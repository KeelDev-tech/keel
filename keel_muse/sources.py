"""Pinned private file producers for the existing seven-family source contract.

Files are data. Producer grants, scope, root and reviewer principals are selected
by trusted embedding code, never accepted from a model/event payload. This is
not an identity service, factual verifier, or canonical application executor.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import stat

from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS, _token, _time
from keel_live.connector import HostSourceConnector, _binding_issues
from keel_live.review import Principal, ReviewService, authorize
from keel_grounding.evidence import _pointer as select_json_pointer
from keel_loki.common import (LokiError, canonical, clone, decode_json, digest,
                              require_dict, require_hash, require_int)
from keel_sources.capture import ingest_attachments
from keel_sources.service import _flow
from keel_sources.store import SourceStore
from tools.bench_inventory import directory_fd, safe_name


FLAGS = {"execution_authorized": False, "canonical_writes": 0,
         "source_authenticity_verified": False, "producer_identity_authenticated": False,
         "factual_support_verified": False, "model_calls": 0, "http_calls": 0}
MAX_FILE_BYTES = 8 * 1024 * 1024


def _require(condition, code):
    if not condition:
        raise LokiError(code)


class PrivateFileRoot:
    """One held directory descriptor; bounded no-symlink, single-link file reads."""

    def __init__(self, root):
        self.path = Path(root).absolute()
        self.fd = directory_fd(self.path)
        try:
            info = os.fstat(self.fd)
            _require(stat.S_IMODE(info.st_mode) == 0o700, "source_root_not_private")
            _require(not hasattr(os, "getuid") or info.st_uid == os.getuid(), "source_root_owner_mismatch")
            self.identity = (info.st_dev, info.st_ino)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def check_current(self):
        _require(self.fd is not None, "source_root_closed")
        current = directory_fd(self.path)
        try:
            info = os.fstat(current)
            _require((info.st_dev, info.st_ino) == self.identity and stat.S_IMODE(info.st_mode) == 0o700,
                     "source_root_changed")
        finally:
            os.close(current)

    def read(self, relative, *, expected_sha256=None):
        _require(self.fd is not None, "source_root_closed")
        parts = safe_name(relative).parts
        parent = os.dup(self.fd)
        fd = None
        try:
            for name in parts[:-1]:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(fd)
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                     0 <= before.st_size <= MAX_FILE_BYTES, "bounded_single_link_source_required")
            _require(stat.S_IMODE(before.st_mode) == 0o600 and
                     (not hasattr(os, "getuid") or before.st_uid == os.getuid()), "source_file_not_private")
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                _require(total <= MAX_FILE_BYTES, "source_file_too_large")
            after = os.fstat(fd)
            keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
            _require(total == before.st_size and all(getattr(before, key) == getattr(after, key) for key in keys),
                     "source_changed_during_read")
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            _require((current.st_dev, current.st_ino) == (after.st_dev, after.st_ino), "source_path_changed")
            raw = b"".join(chunks)
            actual = hashlib.sha256(raw).hexdigest()
            if expected_sha256 is not None:
                _require(actual == require_hash(expected_sha256), "source_file_pin_changed")
            return raw
        except OSError:
            raise LokiError("source_file_unavailable_or_unsafe") from None
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _head(store, connection, scope):
    current = store.read_scope(connection, scope)
    revisions = {}
    for family, descriptor in current["sources"].items():
        revisions[family] = (store._revision(scope, family, descriptor, store._material(family, descriptor, scope))
                             if current["generations"][family] else None)
    present = {family: "record" in descriptor for family, descriptor in current["sources"].items()}
    material = [descriptor for descriptor in current["sources"].values() if "source_ref" in descriptor]
    approval = current["sources"]["approval"].get("record", {})
    return {"schema": "keel.muse.source_head.v1", "store_id": store.store_id,
            "scope": clone(scope), "generations": current["generations"], "revisions": revisions,
            "descriptor_sha256": {key: digest(value) for key, value in current["sources"].items()},
            "record_presence": present,
            "valid_from": math.ceil(max(_time(row["observed_at"]).timestamp() for row in material)) if material else None,
            "valid_until": math.floor(min(_time(row["expires_at"]).timestamp() for row in material)) if material else None,
            "revoked_families": [family for family, descriptor in current["sources"].items()
                                 if descriptor.get("revoked") is True or descriptor.get("record", {}).get("revoked") is True],
            "approval": {"decision": approval.get("decision"), "revoked": approval.get("revoked"),
                         "component_revisions": approval.get("component_revisions")}}


def source_head(store, scope):
    """Actual current material; excludes export timestamps so read pins are stable."""
    _require(isinstance(store, SourceStore), "source_store_required")
    with store.read_transaction() as connection:
        return _head(store, connection, scope)


def head_pin(store, scope):
    return digest(source_head(store, scope))


def _binding(binding):
    required = {"producer_id", "path", "sha256", "expected_generation"}
    _require(type(binding) is dict and required <= set(binding) and set(binding) <= required | {"pointer"},
             "source_binding_fields_invalid")
    _token(binding["producer_id"])
    safe_name(binding["path"])
    require_hash(binding["sha256"])
    require_int(binding["expected_generation"])
    if "pointer" in binding:
        _require(type(binding["pointer"]) is str and len(binding["pointer"]) <= 4096,
                 "source_binding_pointer_invalid")


def _read_bound_json(files, binding):
    document = decode_json(files.read(binding["path"], expected_sha256=binding["sha256"]))
    return select_json_pointer(document, binding.get("pointer", ""))


class FileSourceProducer:
    """Atomic six-source capture plus separately explicit human decision import.

    Scope must already be registered through the existing host connector. Root,
    grants and bindings are trusted host configuration. Even a permitted name is
    not producer authentication. Capture preserves source observation timestamps.
    """

    def __init__(self, store, *, root, scope, producer_components, approval_producer_id=None):
        self.connector = HostSourceConnector(store, producer_components=producer_components,
                                              action="PREPARE", attachment_source_root=Path(root).absolute())
        self.store = store
        self.scope = clone(scope)
        require_dict(self.scope, {"workspace_id", "role_id", "application_id", "action"}, "scope")
        for value in self.scope.values():
            _token(value)
        _require(self.scope["workspace_id"] == store.workspace_id and self.scope["action"] == "PREPARE", "producer_scope_mismatch")
        if approval_producer_id is not None:
            _token(approval_producer_id)
        self.approval_producer_id = approval_producer_id
        self.root = Path(root).absolute()

    def capture(self, bindings, *, expected_bindings_sha256, flow, expected_flow_sha256, expected_head_sha256):
        bindings, flow = clone(bindings), clone(flow)
        _require(digest(bindings) == require_hash(expected_bindings_sha256), "producer_bindings_pin_changed")
        _require(type(bindings) is dict and set(bindings) <= set(PREAPPROVAL_COMPONENTS), "preapproval_families_only")
        _require(digest(flow) == require_hash(expected_flow_sha256), "flow_pin_changed")
        require_hash(expected_head_sha256)
        for family, binding in bindings.items():
            _binding(binding)
            _require(binding["producer_id"] in self.connector.producer_components and
                     family in self.connector.producer_components[binding["producer_id"]], "producer_component_not_allowed")
        captured = []
        with PrivateFileRoot(self.root) as files:
            # All configured files must exist and match before a store mutation.
            payloads = {family: _read_bound_json(files, binding)
                        for family, binding in bindings.items()}
            with self.store.transaction() as connection:
                _flow(flow, connection.keel_now)
                lead = self.connector._scope(self.scope, flow)
                _require(digest(_head(self.store, connection, self.scope)) == expected_head_sha256, "source_head_pin_changed")
                for family in PREAPPROVAL_COMPONENTS:
                    if family not in bindings:
                        continue
                    binding, descriptor = bindings[family], payloads[family]
                    issues = _binding_issues(family, descriptor, lead)
                    _require(not issues, issues[0]["code"] if issues else "canonical_binding_mismatch")
                    _require(self.store.current_generation(connection, self.scope, family) == binding["expected_generation"],
                             "generation_conflict")
                    if family == "attachments":
                        # Require supplied byte hashes, not generated manifest confidence.
                        record = descriptor.get("record") if type(descriptor) is dict else None
                        members = record.get("files") if type(record) is dict else None
                        _require(type(members) is list, "attachment_manifest_required")
                        for member in members:
                            require_hash(member.get("sha256"))
                            original = member.get("source_path", member.get("path"))
                            raw = files.read(original, expected_sha256=member["sha256"])
                            _require(type(member.get("size_bytes")) is int and member["size_bytes"] == len(raw), "attachment_size_mismatch")
                        descriptor = ingest_attachments(descriptor, source_root=self.root,
                            attachment_root=self.store.attachment_root, scope=self.scope, now=connection.keel_now)
                    written = self.store.write_source(connection, self.scope, family, descriptor,
                                                       binding["expected_generation"])
                    self.store.audit_event(connection, "MUSE_FILE_SOURCE_CAPTURED", self.scope, family,
                        written["generation"], {"actor_id": binding["producer_id"],
                        "descriptor_sha256": digest(descriptor), "revision": written["revision"],
                        "source_ref": descriptor["source_ref"], "source_version": descriptor["source_version"]})
                    captured.append({"family": family, "file_sha256": binding["sha256"], **written})
                for family, binding in bindings.items():
                    files.read(binding["path"], expected_sha256=binding["sha256"])
                files.check_current()
                _flow(flow, self.store.clock())
                after = _head(self.store, connection, self.scope)
                final_sources = self.store.read_scope(connection, self.scope)["sources"]
                coverage = {family: int("record" in final_sources[family]) for family in COMPONENTS}
        return {"schema": "keel.muse.file_capture.v1", "status": "CAPTURED" if captured else "NO_FILES_SELECTED",
                "captured": captured, "bindings_sha256": digest(bindings), "flow_sha256": digest(flow),
                "before_head_sha256": expected_head_sha256, "after_head_sha256": digest(after), "source_head": after,
                "source_record_coverage": coverage,
                "missing_families": [family for family in COMPONENTS if not coverage[family]],
                "approval_requests_created": 0, "approval_decisions_recorded": 0,
                "atomic_source_transaction": True, "unreferenced_attachment_objects_possible": True, **FLAGS}

    def observe_approval(self, binding, *, expected_binding_sha256, principal, flow,
                         expected_flow_sha256, expected_head_sha256):
        """Import one actual decision against an existing reviewed request.

        ``principal`` is supplied by the authenticated embedding host, never a
        JSON dictionary. This method cannot authenticate that host. No request
        is created, no decision is inferred, and retries retain existing refusal.
        """
        binding, flow = clone(binding), clone(flow)
        _binding(binding)
        _require(digest(binding) == require_hash(expected_binding_sha256), "producer_bindings_pin_changed")
        _require(self.approval_producer_id is not None and binding["producer_id"] == self.approval_producer_id,
                 "approval_producer_not_allowed")
        _require(digest(flow) == require_hash(expected_flow_sha256), "flow_pin_changed")
        require_hash(expected_head_sha256)
        authorize(principal, "review:decide", self.scope)
        with PrivateFileRoot(self.root) as files:
            decision = _read_bound_json(files, binding)
            require_dict(decision, {"schema", "request_id", "decision", "reviewed_sha256", "expires_at"}, "decision observation")
            _require(decision["schema"] == "keel.muse.human_decision.v1", "decision_schema_invalid")
            _token(decision["request_id"])
            require_hash(decision["reviewed_sha256"])
            _require(decision["decision"] in ("APPROVE", "REJECT"), "decision_invalid")
            def flow_guard(now):
                _flow(flow, now)
                self.connector._scope(self.scope, flow)
                files.check_current()
                files.read(binding["path"], expected_sha256=binding["sha256"])
            initial = [True]
            def source_guard(connection, now):
                current = _head(self.store, connection, self.scope)
                # ReviewService runs this before and after its single mutation.
                if initial[0]:
                    _require(digest(current) == expected_head_sha256, "source_head_pin_changed")
                    _require(current["generations"]["approval"] == binding["expected_generation"], "generation_conflict")
                    initial[0] = False
                else:
                    _require(current["generations"]["approval"] == binding["expected_generation"] + 1, "unexpected_approval_generation")
                lead = self.connector._scope(self.scope, flow)
                source_rows = self.store.read_scope(connection, self.scope)["sources"]
                _require(not any(_binding_issues(family, source_rows[family], lead) for family in ("target", "route")),
                         "canonical_binding_mismatch")
            service = ReviewService(self.store, transaction_guard=flow_guard, transaction_source_guard=source_guard)
            result = service.decide(principal, self.scope, decision["request_id"], decision=decision["decision"],
                                    reviewed_sha256=decision["reviewed_sha256"], expires_at=decision["expires_at"])
            after = source_head(self.store, self.scope)
        return {"schema": "keel.muse.approval_observation.v1", "status": "RECORDED",
                "request_id": result["request_id"], "recorded_state": result["recorded_state"],
                "approval_currently_valid": result["approval_currently_valid"],
                "file_sha256": binding["sha256"], "after_head_sha256": digest(after), "source_head": after,
                "approval_requests_created": 0, "approval_decisions_recorded": 1,
                "identity_authentication": "host_responsibility", **FLAGS}


def make_demo_inputs(home):
    """Create actual synthetic source files and a fresh registered source store."""
    from tools.make_flow_demo import make_snapshot
    from tools.make_source_producer_demo import ATTACHMENT, SyntheticClock, fixture_scope, fixture_sources
    home = Path(home).absolute()
    _require(home.parent.resolve(strict=True) == home.parent, "canonical_parent_required")
    home.mkdir(mode=0o700, exist_ok=False)
    root = home / "files"
    root.mkdir(mode=0o700)
    def write(name, raw):
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
    clock, scope = SyntheticClock(), fixture_scope()
    store = SourceStore(home / "store", scope["workspace_id"], clock=clock)
    sources = fixture_sources(scope)
    sources["attachments"]["record"]["files"][0].update(sha256=hashlib.sha256(ATTACHMENT).hexdigest(), size_bytes=len(ATTACHMENT))
    write("synthetic-resume.txt", ATTACHMENT)
    bindings = {}
    for family in PREAPPROVAL_COMPONENTS:
        raw = canonical(sources[family])
        write(family + ".json", raw)
        bindings[family] = {"producer_id": "synthetic-file-producer", "path": family + ".json",
                            "sha256": hashlib.sha256(raw).hexdigest(), "expected_generation": 0}
    flow = make_snapshot()
    flow["leads"][0]["posting_url"] = sources["target"]["record"]["canonical_posting_url"]
    producer = FileSourceProducer(store, root=root, scope=scope,
        producer_components={"synthetic-file-producer": list(PREAPPROVAL_COMPONENTS)},
        approval_producer_id="synthetic-review-producer")
    producer.connector.register_flow(flow)
    return {"producer": producer, "store": store, "scope": scope, "root": root,
            "clock": clock, "flow": flow, "bindings": bindings, "descriptors": sources,
            "expected_head_sha256": head_pin(store, scope), "synthetic": True}


def demo(home):
    fixture = make_demo_inputs(home)
    result = fixture["producer"].capture(fixture["bindings"], expected_bindings_sha256=digest(fixture["bindings"]),
        flow=fixture["flow"], expected_flow_sha256=digest(fixture["flow"]), expected_head_sha256=fixture["expected_head_sha256"])
    return {"schema": "keel.muse.sources_demo.v1", "status": "PASS", "synthetic": True,
            "capture": result, "actual_source_files_read": 6, "actual_attachment_files_read": 1,
            "approval_genuinely_absent": result["missing_families"] == ["approval"],
            "actual_human_decisions": 0, **FLAGS}
