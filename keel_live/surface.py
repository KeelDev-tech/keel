"""Host-bound review and Workbench projection. HTTP is never a source producer.

The principal, source store, canonical-body provider and attachment directory are
trusted startup inputs. A private session bearer token delegates that principal's
limited review authority; it does not independently prove a person's identity.
"""
from copy import deepcopy
from threading import RLock

from keel_trust.common import digest, keys, require, timestamp
from keel_workbench.api import APIError, dispatch as workbench_dispatch
from keel_workbench.model import adapt_body, project, validate
from keel_workbench.service import Workbench, Conflict


def qualified_view(document, *, attachment_root, now):
    """Intersect legacy views with canonical bindings and source semantics."""
    from .proof import build_proof
    view = project(document, attachment_root=attachment_root, now=now)
    proof = build_proof(document, workspace_id=document["workspace_id"], synthetic=document["synthetic"],
                        action="PREPARE", attachment_root=attachment_root, now=now)
    proofs = {row["scope"]["role_id"]: row for row in proof["roles"]}
    extra_groups = {}
    for row in view["roles"]:
        checked = proofs[row["role_id"]]
        for reason in set(checked["reasons"]) - set(row["reasons"]):
            component = reason.split(":")[1] if reason.startswith("source_semantics:") else "snapshot"
            key = (component, reason)
            group = extra_groups.setdefault(key, {"component": component, "reason": reason,
                "responsibility": "system", "owner": "host_adapter", "role_ids": []})
            group["role_ids"].append(row["role_id"])
        row["review_checks_passed"] = checked["capture_review_checks_passed"]
        row["status"] = "REVIEW_CHECKS_PASSED" if row["review_checks_passed"] else "REVIEW_REQUIRED"
        row["reasons"] = sorted(set(row["reasons"]) | set(checked["reasons"]))
    view["source_inventory"]["groups"].extend(extra_groups.values())
    view["counts"]["source_groups"] = len(view["source_inventory"]["groups"])
    view["counts"]["source_gaps"] += sum(len(row["role_ids"]) for row in extra_groups.values())
    view["counts"]["review_checks_passed"] = proof["counts"]["capture_review_checks_passed"]
    view["qualification"] = "LIVE_CAPTURE_TO_REVIEW_CHECKS"
    return view


class _LiveWorkbench(Workbench):
    def overview(self):
        return qualified_view(self.snapshot(), attachment_root=self.attachment_root, now=self.clock())

    def run(self, command):
        # Use the original workflow implementations with the stricter live view;
        # no workflow may report a legacy green that the live proof rejects.
        from keel_workbench.workflows import request, execute
        command = deepcopy(command)
        with self.lock:
            document = self.snapshot()
            now = self.clock()
            request(command, [row["role_id"] for row in document["flow"]["leads"]])
            if command["snapshot_sha256"] != digest(document):
                raise Conflict("SNAPSHOT_CHANGED: refresh the workspace")
            prior = self.runs.get(command["request_id"])
            command_hash = digest(command)
            if prior:
                if prior[0] != command_hash:
                    raise Conflict("REQUEST_ID_CONFLICT: use a new request ID")
                if not 0 <= (now - timestamp(prior[1]["evaluated_at"])).total_seconds() <= 90:
                    raise Conflict("REPORT_EXPIRED: evaluate again with a new request ID")
            view = qualified_view(document, attachment_root=self.attachment_root, now=now)
            context_hash = digest({key: view[key] for key in ("current", "counts", "roles")})
            if prior:
                if prior[2] != context_hash:
                    raise Conflict("REPORT_CONTEXT_CHANGED: evaluate again with a new request ID")
                return deepcopy(prior[1])
            result = execute(command, document, view, now=now)
            self.runs[command["request_id"]] = (command_hash, deepcopy(result), context_hash)
            while len(self.runs) > 100:
                self.runs.popitem(last=False)
            return result


class LiveSurface:
    def __init__(self, store, body, *, action, principal, synthetic=False,
                 labels=None, body_provider=None):
        from .review import Principal, ReviewService
        require(action == "PREPARE", "live surface supports PREPARE only")
        require(isinstance(principal, Principal) and principal.workspace_id == store.workspace_id,
                "trusted workspace principal required")
        require("review:read" in principal.allowed_actions, "workspace read grant required")
        require(body_provider is None or callable(body_provider), "trusted body provider must be callable")
        self.store = store
        self.action = action
        self.principal = principal
        self.synthetic = synthetic
        self.labels = deepcopy(labels or [])
        self.body_provider = body_provider
        self.body = deepcopy(body)
        self.lock = RLock()
        self.reviews = ReviewService(store)
        self.workbench = None
        self.source_export = None
        with self.lock:
            self._refresh()

    def _scopes(self, body):
        return [{"workspace_id": self.store.workspace_id, "role_id": row["role_id"],
                 "application_id": row["identity"], "action": self.action}
                for row in body["flow"]["leads"]]

    def _authorize_workspace(self, body):
        # Workbench is an entire canonical projection, so never expose a broader
        # document than the principal's scope grant. Filtering its graph ad hoc
        # would invalidate identity/history checks.
        from .review import authorize
        for scope in self._scopes(body):
            authorize(self.principal, "review:read", scope)

    def _refresh(self):
        from .connector import export_workbench
        if self.body_provider is not None:
            try:
                candidate = deepcopy(self.body_provider())
            except Exception:
                raise APIError(503, "HOST_EXPORT_UNAVAILABLE", "The trusted host export could not be read.") from None
        else:
            candidate = deepcopy(self.body)
        keys(candidate, {"flow", "assurance", "trust"})
        self._authorize_workspace(candidate)
        # The connector may return stale diagnostics, but must not renew any
        # supplied canonical observation time. _current() enforces mutation use.
        result = export_workbench(self.store, candidate, action=self.action,
                                  synthetic=self.synthetic, labels=self.labels, allow_stale_read=True)
        document = result["workbench_snapshot"]
        if self.workbench is not None:
            previous = self.workbench.document
            # Store reads observe an unchanged local snapshot with a new wrapper
            # clock. Preserve the first wrapper observation for stable UI CAS;
            # this is conservative and cannot extend evidence freshness.
            probe = deepcopy(document)
            old_sources = previous.get("revision_sources")
            new_sources = probe.get("revision_sources")
            if old_sources and new_sources:
                for field in ("observed_at", "expires_at"):
                    new_sources["snapshot"][field] = old_sources["snapshot"][field]
            if probe == previous:
                document = deepcopy(previous)
            document = validate(document, workspace_id=self.store.workspace_id, synthetic=self.synthetic)
            project(document, attachment_root=self.store.attachment_root, now=self.store.clock())
            self.workbench.document = document
        else:
            self.workbench = _LiveWorkbench(document, workspace_id=self.store.workspace_id,
                                       synthetic=self.synthetic, attachment_root=self.store.attachment_root,
                                       host_clock=self.store.clock)
        self.body = candidate
        self.source_export = result["source_export"]
        return self.workbench

    def update_body(self, body):
        """Trusted in-process host adapter only; never dispatched over HTTP."""
        with self.lock:
            require(self.body_provider is None, "body provider owns canonical updates")
            previous = self.body
            self.body = deepcopy(body)
            try:
                self._refresh()
            except Exception:
                self.body = previous
                raise

    def _current(self):
        from keel_sources.service import _flow
        try:
            _flow(self.body["flow"], self.store.clock())
        except ValueError:
            raise Conflict("HOST_EXPORT_STALE: the trusted host must supply a fresh complete export") from None

    def _scope(self, scope):
        keys(scope, {"workspace_id", "role_id", "application_id", "action"})
        if scope not in self._scopes(self.body):
            raise Conflict("SCOPE_CHANGED: refresh the current role and application identity")

    def _inspection(self, scope):
        """Expose current material plus the canonical connector's stricter view."""
        from keel_sources.store import SourceStoreError
        from .connector import _binding_issues
        try:
            result = self.reviews.inspect(self.principal, scope)
        except SourceStoreError as error:
            if str(error) != "scope_not_registered":
                raise
            # Absence is a read diagnostic, never an invented registered scope.
            from keel_agent.revisions import COMPONENTS
            return {"scope": deepcopy(scope), "scope_registered": False, "sources": {},
                    "revision_diagnostics": {"components": {key: {"status": "MISSING",
                        "revision": None, "reason": "scope_not_registered"} for key in COMPONENTS}},
                    "semantic_validation": {"ready_for_approval": False,
                        "issues": [{"component": "scope", "code": "scope_not_registered"}]},
                    "current_request": None, "canonical_binding_verified": False,
                    "execution_authorized": False, "source_authenticity_verified": False}
        source_row = next((row for row in self.source_export["rows"] if row["scope"] == scope), None)
        if source_row is None:
            raise Conflict("SOURCE_SCOPE_UNAVAILABLE: the host must register this current role")
        lead = next(row for row in self.body["flow"]["leads"] if row["role_id"] == scope["role_id"])
        issues = [issue for component in ("target", "route")
                  for issue in _binding_issues(component, result["sources"].get(component), lead)]
        result["semantic_validation"]["issues"].extend(issues)
        result["canonical_binding_verified"] = bool(source_row["canonical_binding_verified"] and not issues)
        if not result["canonical_binding_verified"]:
            result["semantic_validation"]["ready_for_approval"] = False
            if result["current_request"]:
                result["current_request"]["request_actionable"] = False
                result["current_request"]["approval_currently_valid"] = False
                result["current_request"]["state"] = "BLOCKED"
                result["current_request"]["reason"] = "canonical_source_binding_unverified"
        return result

    def review_overview(self):
        with self.lock:
            self._refresh()
            view = self.workbench.overview()
            return {"schema": "keel.live_review_overview.v1", "workspace_id": self.store.workspace_id,
                    "synthetic": self.synthetic, "current": view["current"],
                    "observed_at": self.body["flow"]["observed_at"],
                    "evaluated_at": self.store.clock().isoformat(),
                    "flow_sha256": digest(self.body["flow"]),
                    "scopes": self._scopes(self.body), "roles": view["roles"],
                    "operator": {"actor_id": self.principal.actor_id,
                                 "authority_record_ref": self.principal.authority_record_ref},
                    "allowed_actions": sorted(self.principal.allowed_actions),
                    "execution_authorized": False}

    def review_command(self, command, payload):
        fields = {"scope", "flow_sha256"}
        additions = {"show": set(), "request": {"expires_at", "confirmed"},
                     "decide": {"request_id", "decision", "reviewed_sha256", "expires_at", "confirmed"},
                     "revoke": {"request_id", "reason", "confirmed"}}
        if command not in additions:
            raise APIError(404, "NOT_FOUND", "unknown review operation")
        keys(payload, fields | additions[command])
        with self.lock:
            self._refresh()
            self._scope(payload["scope"])
            if payload["flow_sha256"] != digest(self.body["flow"]):
                raise Conflict("HOST_EXPORT_CHANGED: refresh before reviewing")
            scope = payload["scope"]
            if command == "show":
                return self._inspection(scope)
            require(payload["confirmed"] is True, "explicit review confirmation required")
            self._current()
            if command in ("request", "decide"):
                source_row = next((row for row in self.source_export["rows"] if row["scope"] == scope), None)
                if not source_row or not source_row["canonical_binding_verified"]:
                    raise Conflict("CANONICAL_BINDING_UNVERIFIED: repair current target and route evidence")
                if not source_row["semantic_validation"]["ready_for_approval"]:
                    raise Conflict("SOURCE_SEMANTICS_BLOCKED: repair current source evidence")
            from .review import ReviewService
            captured_body = deepcopy(self.body)
            captured_hash = digest(captured_body)
            def transaction_guard(now):
                from keel_sources.service import _flow
                _flow(captured_body["flow"], now)
                # The host file is independently refreshed while SQLite waits.
                # Any context change requires a new displayed review context.
                if self.body_provider is not None:
                    try:
                        latest = self.body_provider()
                    except Exception:
                        raise Conflict("HOST_EXPORT_UNAVAILABLE: refresh before deciding") from None
                    if digest(latest) != captured_hash:
                        raise Conflict("HOST_EXPORT_CHANGED: refresh before deciding")
            def transaction_source_guard(conn, now):
                if command == "revoke":
                    return
                from .connector import _binding_issues
                current = self.store.read_scope(conn, scope)
                lead = next(row for row in captured_body["flow"]["leads"] if row["role_id"] == scope["role_id"])
                if any(_binding_issues(component, current["sources"].get(component), lead)
                       for component in ("target", "route")):
                    raise Conflict("CANONICAL_BINDING_CHANGED: refresh before deciding")
            service = ReviewService(self.store, transaction_guard=transaction_guard,
                                    transaction_source_guard=transaction_source_guard)
            if command == "request":
                result = service.request(self.principal, scope, expires_at=payload["expires_at"])
            elif command == "decide":
                result = service.decide(self.principal, scope, payload["request_id"],
                                             decision=payload["decision"], reviewed_sha256=payload["reviewed_sha256"],
                                             expires_at=payload["expires_at"])
            else:
                result = service.revoke(self.principal, scope, payload["request_id"], reason=payload["reason"])
            # Reproject the committed source store. If projection fails the
            # durable decision is retained and inspect
            # reveals it; clients must refresh before making another decision.
            self._refresh()
            return result

    def dispatch(self, method, path, body=None):
        if method == "GET" and path == "/health":
            return {"status": "ok", "service": "keel-live", "execution_authorized": False}
        if method == "GET" and path == "/api/live/v1/review":
            return self.review_overview()
        if method == "POST" and path.startswith("/api/live/v1/review/"):
            return self.review_command(path.rsplit("/", 1)[-1], body)
        if method == "POST" and path == "/api/v1/snapshot":
            raise APIError(405, "HOST_EXPORT_ONLY", "The trusted host supplies this workspace export.")
        with self.lock:
            self._refresh()
            if method == "GET" and path == "/api/live/v1/proof":
                from .proof import build_proof
                return build_proof(self.workbench.snapshot(), workspace_id=self.store.workspace_id,
                                   synthetic=self.synthetic, action=self.action,
                                   attachment_root=self.store.attachment_root, now=self.store.clock())
            if method == "GET" and path == "/openapi.json":
                from keel_workbench.openapi import specification
                specification = deepcopy(specification())
                specification["paths"]["/api/v1/snapshot"].pop("post", None)
                return specification
            return workbench_dispatch(self.workbench, method, path, body)
