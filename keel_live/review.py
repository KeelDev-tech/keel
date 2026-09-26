"""Host-authorized, scope-bound review over Keel's existing source decisions.

``Principal`` is an immutable *in-process authorization claim*. It does not
authenticate a person. The embedding host must establish that claim through its
own operator authentication and must never construct it from an HTTP/model
payload. The JSON adapter intentionally has no actor or authority parameters.

No method authorizes submission, authenticates source facts, resolves existing
holds, or creates a request implicitly. Read methods do not update the store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import json
from types import MappingProxyType
from collections.abc import Mapping

from keel_agent.revisions import SourceError, _canonical, _token, export_revisions
from keel_sources.capture import validate_semantics
from keel_sources import decisions


SCOPE_FIELDS = ("workspace_id", "role_id", "application_id", "action")
REVIEW_ACTIONS = frozenset({"review:read", "review:request", "review:decide", "review:revoke"})
_TABLES = frozenset({"source_approval_requests", "source_approval_decisions", "source_approval_revocations"})
_FLAGS = {"execution_authorized": False, "source_authenticity_verified": False,
          "factual_support_verified": False, "identity_authentication": "host_responsibility"}


class ReviewError(ValueError):
    """A fixed diagnostic that does not expose private material."""


def _scope(value):
    if not isinstance(value, Mapping) or set(value) != set(SCOPE_FIELDS):
        raise ReviewError("review_scope_invalid")
    try:
        return {key: _token(value[key]) for key in SCOPE_FIELDS}
    except SourceError:
        raise ReviewError("review_scope_invalid") from None


def _scope_key(value):
    return tuple(value[key] for key in SCOPE_FIELDS)


@dataclass(frozen=True)
class Principal:
    """Trusted host input; scopes and operation permissions have no wildcards.

    A scope's ``action`` (for example ``submit``) identifies reviewed material;
    ``allowed_actions`` contains review operations such as ``review:decide``.
    Mutable constructor collections are copied so later payload/config mutation
    cannot broaden the principal's authority.
    """

    actor_id: str
    authority_record_ref: str
    workspace_id: str
    allowed_actions: frozenset[str]
    allowed_scopes: tuple[Mapping, ...]
    _scope_keys: frozenset = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        try:
            for value in (self.actor_id, self.authority_record_ref, self.workspace_id):
                _token(value)
        except SourceError:
            raise ReviewError("review_principal_invalid") from None
        if not isinstance(self.allowed_actions, (set, frozenset, tuple, list)):
            raise ReviewError("review_permissions_invalid")
        if any(not isinstance(value, str) or value not in REVIEW_ACTIONS for value in self.allowed_actions):
            raise ReviewError("review_permissions_invalid")
        if not isinstance(self.allowed_scopes, (tuple, list)):
            raise ReviewError("review_principal_scopes_invalid")
        scopes = tuple(_scope(value) for value in self.allowed_scopes)
        if any(value["workspace_id"] != self.workspace_id for value in scopes):
            raise ReviewError("review_principal_workspace_mismatch")
        keys = frozenset(_scope_key(value) for value in scopes)
        if len(keys) != len(scopes):
            raise ReviewError("review_principal_duplicate_scope")
        object.__setattr__(self, "allowed_actions", frozenset(self.allowed_actions))
        object.__setattr__(self, "allowed_scopes", tuple(MappingProxyType(value) for value in scopes))
        object.__setattr__(self, "_scope_keys", keys)


def authorize(principal, action, scope):
    """Check a host grant without reading data or registering a scope."""
    if type(principal) is not Principal:
        raise ReviewError("review_principal_required")
    scope = _scope(scope)
    if (scope["workspace_id"] != principal.workspace_id
            or action not in principal.allowed_actions
            or _scope_key(scope) not in principal._scope_keys):
        raise ReviewError("review_access_denied")
    return scope


class _GuardedStore:
    """Apply a trusted host condition inside unchanged source transactions."""

    def __init__(self, store, guard, source_guard):
        self._store = store
        self._guard = guard
        self._source_guard = source_guard

    def _check(self, conn, now):
        if self._guard is not None:
            self._guard(now)
        if self._source_guard is not None:
            self._source_guard(conn, now)

    def __getattr__(self, name):
        return getattr(self._store, name)

    @contextmanager
    def transaction(self, *args, **kwargs):
        with self._store.transaction(*args, **kwargs) as conn:
            # The source store samples keel_now after acquiring its write lock.
            # Recheck again immediately before control returns to its commit.
            self._check(conn, conn.keel_now)
            yield conn
            self._check(conn, self._store.clock())


class ReviewService:
    """Compose existing immutable source approvals with host authorization.

    ``inspect`` returns private source descriptors and diagnostics. ``get``
    returns the exact recorded review packet plus current validity. Neither
    creates an approval request. Only an explicit ``request`` call does so.

    ``transaction_guard(now)`` is a host-only callback. The live surface supplies
    canonical flow freshness and scope checks through it; an exception aborts
    the transaction. It runs after acquiring the write lock and immediately
    before commit. Standalone users that omit it retain the source decision
    contract, but have not established freshness of any external canonical flow.
    ``transaction_source_guard(conn, now)`` additionally receives the held source
    transaction so host checks can bind target/route records to that same flow
    without opening a second connection or introducing a race between checks.
    """

    def __init__(self, store, *, transaction_guard=None, transaction_source_guard=None):
        if transaction_guard is not None and not callable(transaction_guard):
            raise ReviewError("review_transaction_guard_invalid")
        if transaction_source_guard is not None and not callable(transaction_source_guard):
            raise ReviewError("review_transaction_source_guard_invalid")
        self.store = store
        self._decision_store = (_GuardedStore(store, transaction_guard, transaction_source_guard)
                                if transaction_guard is not None or transaction_source_guard is not None else store)

    def _authorize(self, principal, scope, action):
        # A dict carrying actor strings is not an in-process Principal.
        scope = authorize(principal, action, scope)
        if principal.workspace_id != self.store.workspace_id:
            raise ReviewError("review_access_denied")
        return scope

    @staticmethod
    def _has_requests(conn):
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & _TABLES and not _TABLES <= tables:
            raise ReviewError("approval_schema_incomplete")
        return _TABLES <= tables

    def _load_scoped(self, conn, scope, request_id):
        if not self._has_requests(conn):
            raise ReviewError("approval_request_not_found")
        request = decisions._load(conn, request_id)
        if request["scope"] != scope:
            # Never return or inspect material from a different scope.
            raise ReviewError("review_request_scope_mismatch")
        return request

    def _current(self, conn, scope):
        snapshot = self.store.snapshot_in_transaction(conn, scopes=[scope])
        sources = snapshot["roles"][0]["sources"]
        revisions = export_revisions(snapshot, attachment_root=self.store.attachment_root,
                                     now=conn.keel_now)["roles"][0]
        semantic = validate_semantics(sources, scope=scope, attachment_root=self.store.attachment_root,
                                      now=conn.keel_now)
        return sources, revisions, semantic

    def _view(self, conn, request, current=None):
        result = decisions._view(self.store, conn, request)
        _, revisions, semantic = current or self._current(conn, request["scope"])
        recorded_state = result["state"]
        valid = bool(recorded_state == "APPROVED" and result["is_current"]
                     and revisions["envelope_inputs_ready"] and semantic["ready_for_approval"])
        if recorded_state == "APPROVED" and not valid:
            reason = revisions["components"]["approval"].get("reason")
            result["state"] = "STALE" if reason == "approval_dependency_mismatch" else "BLOCKED"
            result["reason"] = reason or "approval_current_inputs_invalid"
        result.update({"recorded_state": recorded_state, "approval_currently_valid": valid,
                       "revision_diagnostics": revisions, "semantic_validation": semantic,
                       "validated_at": conn.keel_now.isoformat(), **_FLAGS})
        return result

    def inspect(self, principal, scope):
        """Read current inputs and the latest request, if one actually exists."""
        scope = self._authorize(principal, scope, "review:read")
        with self.store.read_transaction() as conn:
            current = self._current(conn, scope)
            pending = None
            if self._has_requests(conn):
                row = conn.execute(
                    "SELECT request_id FROM source_approval_requests WHERE scope_json=? ORDER BY rowid DESC LIMIT 1",
                    (_canonical(scope).decode("utf-8"),)).fetchone()
                if row:
                    pending = self._view(conn, self._load_scoped(conn, scope, row[0]), current)
            return {"schema": "keel.live_review_scope.v1", "scope": scope, "sources": current[0],
                    "revision_diagnostics": current[1], "semantic_validation": current[2],
                    "current_request": pending, "validated_at": conn.keel_now.isoformat(), **_FLAGS}

    def get(self, principal, scope, request_id):
        """Read exactly one scoped private packet; material is never refreshed."""
        scope = self._authorize(principal, scope, "review:read")
        with self.store.read_transaction() as conn:
            return self._view(conn, self._load_scoped(conn, scope, request_id))

    def request(self, principal, scope, *, expires_at):
        scope = self._authorize(principal, scope, "review:request")
        result = decisions.prepare_request(self._decision_store, scope, expires_at=expires_at)
        # A writer need not hold read permission; return only this operation's
        # result after the same current-state checks used by the review surface.
        with self.store.read_transaction() as conn:
            return self._view(conn, self._load_scoped(conn, scope, result["request_id"]))

    def decide(self, principal, scope, request_id, *, decision, reviewed_sha256, expires_at):
        scope = self._authorize(principal, scope, "review:decide")
        with self.store.read_transaction() as conn:
            self._load_scoped(conn, scope, request_id)
        # The existing writer rechecks exact digest, attachments, expiry and
        # source material inside its serialized transaction. A read precheck is
        # not used as authorization to skip any of those checks.
        result = decisions.decide_request(
            self._decision_store, request_id, decision=decision, actor_id=principal.actor_id,
            authority_record_ref=principal.authority_record_ref,
            reviewed_sha256=reviewed_sha256, expires_at=expires_at)
        with self.store.read_transaction() as conn:
            return self._view(conn, self._load_scoped(conn, scope, result["request_id"]))

    def revoke(self, principal, scope, request_id, *, reason):
        scope = self._authorize(principal, scope, "review:revoke")
        with self.store.read_transaction() as conn:
            self._load_scoped(conn, scope, request_id)
        result = decisions.revoke_request(self._decision_store, request_id, actor_id=principal.actor_id, reason=reason)
        with self.store.read_transaction() as conn:
            return self._view(conn, self._load_scoped(conn, scope, result["request_id"]))

    def dispatch(self, principal, action, payload):
        """Strict JSON-facing adapter. Authentication remains the host's job.

        The host passes its established principal separately; actor identifiers,
        alternate authority claims and execution flags in payloads are errors.
        """
        fields = {
            "inspect": {"scope"}, "get": {"scope", "request_id"},
            "request": {"scope", "expires_at"},
            "decide": {"scope", "request_id", "decision", "reviewed_sha256", "expires_at"},
            "revoke": {"scope", "request_id", "reason"},
        }
        if not isinstance(action, str) or action not in fields:
            raise ReviewError("review_action_invalid")
        if type(payload) is not dict or set(payload) != fields[action]:
            raise ReviewError("review_payload_invalid")
        # Freeze JSON data before authorization and use, avoiding caller-owned
        # nested scope mutation between those operations.
        try:
            arguments = json.loads(_canonical(payload))
        except (SourceError, ValueError):
            raise ReviewError("review_payload_invalid") from None
        return getattr(self, action)(principal, **arguments)
