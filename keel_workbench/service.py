"""Shared application service for browser, HTTP, CLI and JSON-line clients."""
from collections import OrderedDict
from threading import RLock
from keel_trust.common import clone, digest, timestamp, require
from .model import validate, project, utcnow
from .workflows import CATALOG, request, execute


class Conflict(ValueError):
    pass


class Workbench:
    def __init__(self, document, *, workspace_id, synthetic=False, attachment_root=None, host_clock=None):
        self.workspace_id = workspace_id; self.synthetic = synthetic
        self.attachment_root = attachment_root; self.clock = host_clock or utcnow
        self.lock = RLock(); self.runs = OrderedDict()
        self.document = validate(document, workspace_id=workspace_id, synthetic=synthetic)
        project(self.document, attachment_root=attachment_root, now=self.clock())

    def snapshot(self):
        with self.lock: return clone(self.document)

    def overview(self):
        return project(self.snapshot(), attachment_root=self.attachment_root, now=self.clock())

    def catalog(self):
        return {"workflows": clone(CATALOG), "execution_authorized": False}

    def replace(self, document, *, previous_sha256):
        candidate = validate(document, workspace_id=self.workspace_id, synthetic=self.synthetic)
        # Validate all reducers before modifying even this session's derived state.
        result = project(candidate, attachment_root=self.attachment_root, now=self.clock())
        with self.lock:
            if digest(self.document) != previous_sha256: raise Conflict("SNAPSHOT_CHANGED: refresh before importing")
            self.document = candidate
        return {"status": "IMPORTED_FOR_THIS_SESSION", "snapshot_sha256": result["snapshot_sha256"],
                "current": result["current"], "canonical_writes": 0, "execution_authorized": False}

    def run(self, command):
        command = clone(command)
        # Serialize short local reducers so identical request IDs cannot race.
        with self.lock:
            doc = clone(self.document); now = self.clock()
            request(command, [r["role_id"] for r in doc["flow"]["leads"]])
            if command["snapshot_sha256"] != digest(doc): raise Conflict("SNAPSHOT_CHANGED: refresh the workspace")
            previous = self.runs.get(command["request_id"])
            command_hash = digest(command)
            if previous:
                if previous[0] != command_hash: raise Conflict("REQUEST_ID_CONFLICT: use a new request ID")
                if not 0 <= (now-timestamp(previous[1]["evaluated_at"])).total_seconds() <= 90:
                    raise Conflict("REPORT_EXPIRED: evaluate again with a new request ID")
            view = project(doc, attachment_root=self.attachment_root, now=now)
            context_hash = digest({k: view[k] for k in ("current", "counts", "roles")})
            if previous:
                if previous[2] != context_hash:
                    raise Conflict("REPORT_CONTEXT_CHANGED: evaluate again with a new request ID")
                return clone(previous[1])
            result = execute(command, doc, view, now=now)
            self.runs[command["request_id"]] = (command_hash, clone(result), context_hash)
            while len(self.runs) > 100: self.runs.popitem(last=False)
            return result

    def history(self):
        with self.lock:
            rows = [value[1] for value in reversed(self.runs.values())]
            return {"persistence": "CURRENT_PROCESS_ONLY", "runs": [{k: r[k] for k in
                    ("request_id", "workflow_id", "evaluated_at", "snapshot_sha256", "current_export")} for r in rows]}
