"""Private durable recovery rehearsal journal; never sends or clears host gates.

One trusted controller owns a journal. Reopening fences every old lease and
turns every started attempt UNKNOWN, even if the former process is still alive.
Host approval digests and observations are declarations, not authenticated human
consent. HMAC protects accidental/unkeyed edits, not an operator owning the key.
"""
from contextlib import closing
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from keel_agent.state import LocalState, StateError, _hash, _text, _time, _private_file
from keel_trust.common import canonical, digest
from maintenance_workbench.keel_maint.contracts import strict_json


class RecoveryError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise RecoveryError(code)


def _identifier(value):
    try:
        return _text(value, "identifier", 128)
    except StateError:
        raise RecoveryError("identifier_invalid") from None


def _digest(value):
    try:
        return _hash(value)
    except StateError:
        raise RecoveryError("sha256_invalid") from None


def _now(value):
    try:
        return _time(time.time() if value is None else value)
    except StateError:
        raise RecoveryError("clock_invalid") from None


def _clone(value):
    return strict_json(canonical(value))


class RecoveryJournal:
    """Single-controller isolated SQLite journal using existing private storage.

    All state transitions use BEGIN IMMEDIATE, FULL synchronous commits, a
    monotonically checked host clock and a HMAC-bound event chain. There are no
    APIs to reset UNKNOWN, rate limits or holds. Restore rollback requires an
    independently retained checkpoint to detect; the same OS account/key owner
    controls this trust boundary.
    """
    def __init__(self, home, workspace_id, *, now=None):
        from pathlib import Path
        self.workspace_id = _identifier(workspace_id)
        self._readonly = False
        self._local = LocalState(Path(home) / "recovery.sqlite3", workspace_id)
        now = _now(now)
        with self._local._transaction(now) as (db, stamp):
            db.execute("CREATE TABLE IF NOT EXISTS loki_recovery_state (slot TEXT PRIMARY KEY, payload TEXT NOT NULL, mac TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS loki_recovery_events (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL, mac TEXT NOT NULL)")
            existing = db.execute("SELECT COUNT(*) FROM loki_recovery_state").fetchone()[0]
            if not existing:
                _require(db.execute("SELECT COUNT(*) FROM loki_recovery_events").fetchone()[0] == 0,
                         "state_missing_with_events")
                state = {"schema": "keel.loki.recovery-state.v1", "workspace_id": workspace_id,
                         "boot_generation": 1, "event_count": 0, "last_now": stamp,
                         "rate_limited": False, "jobs": {}}
                self._store(db, state, "initialize", None, stamp)
            else:
                state, _head = self._read(db)
                state["boot_generation"] += 1
                for job in state["jobs"].values():
                    if job["phase"] == "STARTED":
                        job["phase"] = "UNKNOWN"
                        job["unknown_reason"] = "controller_reopened"
                    if job["owner"] is not None:
                        self._fence(job)
                    job["version"] += 1
                self._store(db, state, "controller_reopen", None, stamp)
            # Bind this controller object to its own committed generation. An
            # older process may still read a snapshot, but must not acquire new
            # leases or mutate state after another controller has taken over.
            self._boot_generation = state["boot_generation"]

    @classmethod
    def open_readonly(cls, home, workspace_id):
        """Inspect an existing journal without opening a new controller epoch.

        No LocalState constructor, schema initialization, clock write or crash
        recovery is invoked. Mutation methods reject this view before a DB
        transaction. The key and private files are rechecked on every read.
        """
        from pathlib import Path
        self = cls.__new__(cls)
        self.workspace_id = _identifier(workspace_id)
        self._readonly = True
        local = LocalState.__new__(LocalState)
        local.path = (Path(home) / "recovery.sqlite3").absolute()
        local.key_path = local.path.with_suffix(local.path.suffix + ".key")
        _private_file(local.path)
        _private_file(local.key_path)
        local._key = local.key_path.read_bytes()
        _require(len(local._key) == 32, "host_key_invalid")
        local.key_id = hashlib.sha256(local._key).hexdigest()
        local.workspace_id = self.workspace_id
        self._local = local
        self.snapshot()
        return self

    def _connection(self):
        if not self._readonly:
            return self._local._connect()
        _private_file(self._local.path)
        _private_file(self._local.key_path)
        _require(hmac.compare_digest(hashlib.sha256(self._local.key_path.read_bytes()).hexdigest(),
                                     self._local.key_id), "host_key_changed")
        db = sqlite3.connect(self._local.path.as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def _mac(self, domain, value):
        return hmac.new(self._local._key, domain.encode() + b"\0" + canonical(value), hashlib.sha256).hexdigest()

    def _read(self, db):
        rows = db.execute("SELECT slot,payload,mac FROM loki_recovery_state").fetchall()
        _require(len(rows) == 1 and rows[0]["slot"] == "state", "state_record_invalid")
        try:
            state = strict_json(rows[0]["payload"])
            _require(type(state) is dict and state.get("schema") == "keel.loki.recovery-state.v1"
                     and state.get("workspace_id") == self.workspace_id, "state_scope_invalid")
            _require(hmac.compare_digest(rows[0]["mac"], self._mac("state", state)), "state_authentication_failed")
            events = db.execute("SELECT sequence,payload,sha256,mac FROM loki_recovery_events ORDER BY sequence").fetchall()
            _require(1 <= len(events) <= 4096 and len(events) == state["event_count"], "event_coverage_invalid")
            previous = "0" * 64
            for index, row in enumerate(events, 1):
                payload = strict_json(row["payload"])
                _require(row["sequence"] == index and payload["sequence"] == index
                         and payload["previous_sha256"] == previous
                         and payload["workspace_id"] == self.workspace_id,
                         "event_chain_invalid")
                _require(digest(payload) == row["sha256"]
                         and hmac.compare_digest(row["mac"], self._mac("event", payload)),
                         "event_authentication_failed")
                previous = row["sha256"]
            _require(payload["state_sha256"] == digest(state), "event_state_binding_invalid")
            return state, previous
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError("journal_encoding_invalid") from None

    def _store(self, db, state, action, job_id, now):
        _require(state["event_count"] < 4096, "event_limit_reached")
        _require(now >= state["last_now"], "authenticated_clock_rollback")
        last = db.execute("SELECT sha256 FROM loki_recovery_events ORDER BY sequence DESC LIMIT 1").fetchone()
        state["event_count"] += 1
        state["last_now"] = now
        payload = {"schema": "keel.loki.recovery-event.v1", "workspace_id": self.workspace_id,
                   "sequence": state["event_count"], "previous_sha256": last[0] if last else "0" * 64,
                   "action": action, "job_id": job_id, "occurred_at": now, "state_sha256": digest(state)}
        db.execute("INSERT INTO loki_recovery_events VALUES (?,?,?,?)",
                   (payload["sequence"], canonical(payload).decode(), digest(payload), self._mac("event", payload)))
        db.execute("INSERT INTO loki_recovery_state VALUES ('state',?,?) ON CONFLICT(slot) DO UPDATE SET payload=excluded.payload,mac=excluded.mac",
                   (canonical(state).decode(), self._mac("state", state)))

    @staticmethod
    def _checkpoint(state, head):
        return digest({"state": state, "event_head_sha256": head})

    @staticmethod
    def _fence(job):
        job["fence"] += 1
        job["owner"] = None
        job["lease_until"] = None

    def _change(self, action, job_id, function, now):
        _require(not self._readonly, "read_only_view")
        now = _now(now)
        with self._local._transaction(now) as (db, stamp):
            state, _head = self._read(db)
            _require(state["boot_generation"] == self._boot_generation,
                     "stale_controller_generation")
            job = None
            if job_id is not None:
                _identifier(job_id)
                _require(job_id in state["jobs"], "job_not_found")
                job = state["jobs"][job_id]
            result = function(state, job, stamp)
            if job is not None:
                job["version"] += 1
            self._store(db, state, action, job_id, stamp)
            return _clone(result)

    def register(self, job_id, revision_sha256, *, now=None):
        job_id, revision_sha256 = _identifier(job_id), _digest(revision_sha256)
        def apply(state, _job, stamp):
            _require(job_id not in state["jobs"], "job_already_registered")
            _require(len(state["jobs"]) < 128, "job_limit_reached")
            state["jobs"][job_id] = {"job_id": job_id, "revision_sha256": revision_sha256,
                "approval_sha256": None, "approval_revision_sha256": None, "approval_active": False,
                "phase": "IDLE", "fence": 0, "owner": None, "lease_until": None,
                "held": False, "escalated": False, "attempt_id": None, "start_revision_sha256": None,
                "start_approval_sha256": None, "start_fence": None, "started_at": None,
                "receipt_sha256": None, "completion_fence": None, "unknown_reason": None, "version": 1}
            return state["jobs"][job_id]
        return self._change("register", None, apply, now)

    def approve(self, job_id, revision_sha256, approval_sha256, *, now=None):
        revision_sha256, approval_sha256 = _digest(revision_sha256), _digest(approval_sha256)
        def apply(state, job, stamp):
            _require(job["phase"] == "IDLE" and job["revision_sha256"] == revision_sha256,
                     "approval_revision_or_phase_mismatch")
            job.update(approval_sha256=approval_sha256, approval_revision_sha256=revision_sha256, approval_active=True)
            return {"recorded": True, "human_approval_authenticated": False, "execution_authorized": False}
        return self._change("approval_declaration", job_id, apply, now)

    def revoke(self, job_id, *, now=None):
        def apply(state, job, stamp):
            job["approval_active"] = False
            if job["phase"] == "STARTED":
                job.update(phase="UNKNOWN", unknown_reason="approval_revoked_after_start")
            self._fence(job)
            return {"recorded": True, "execution_authorized": False}
        return self._change("approval_revoked", job_id, apply, now)

    def revise(self, job_id, revision_sha256, *, now=None):
        revision_sha256 = _digest(revision_sha256)
        def apply(state, job, stamp):
            _require(job["revision_sha256"] != revision_sha256, "revision_unchanged")
            job.update(revision_sha256=revision_sha256, approval_active=False)
            if job["phase"] == "STARTED":
                job.update(phase="UNKNOWN", unknown_reason="revision_changed_after_start")
            self._fence(job)
            return {"recorded": True, "execution_authorized": False}
        return self._change("revision_changed", job_id, apply, now)

    def claim(self, job_id, worker_id, *, lease_seconds=30, now=None):
        worker_id = _identifier(worker_id)
        _require(type(lease_seconds) in (int, float) and 0 < lease_seconds <= 300, "lease_duration_invalid")
        def apply(state, job, stamp):
            _require(not state["rate_limited"] and not job["held"], "persistent_gate_blocks_claim")
            _require(job["phase"] == "IDLE", "attempt_not_retryable")
            _require(job["owner"] is None or job["lease_until"] <= stamp, "lease_already_owned")
            job.update(owner=worker_id, fence=job["fence"] + 1, lease_until=stamp + lease_seconds)
            return {"job_id": job_id, "worker_id": worker_id, "fence": job["fence"],
                    "lease_until": job["lease_until"], "execution_authorized": False}
        return self._change("lease_claimed", job_id, apply, now)

    @staticmethod
    def _worker(job, worker_id, fence, now):
        _identifier(worker_id)
        _require(type(fence) is int and fence > 0, "fence_invalid")
        _require(job["owner"] == worker_id and job["fence"] == fence
                 and job["lease_until"] is not None and now < job["lease_until"], "stale_or_expired_worker")

    def start(self, job_id, worker_id, fence, *, now=None):
        def apply(state, job, stamp):
            self._worker(job, worker_id, fence, stamp)
            _require(job["phase"] == "IDLE" and not state["rate_limited"] and not job["held"], "start_blocked")
            _require(job["approval_active"] and job["approval_revision_sha256"] == job["revision_sha256"],
                     "fresh_approval_declaration_required")
            job.update(phase="STARTED", attempt_id="attempt_" + secrets.token_hex(16),
                       start_revision_sha256=job["revision_sha256"], start_approval_sha256=job["approval_sha256"],
                       start_fence=fence, started_at=stamp)
            return {"attempt_id": job["attempt_id"], "phase": "STARTED",
                    "execution_authorized": False, "external_action_performed": False}
        return self._change("intent_started", job_id, apply, now)

    def complete(self, job_id, worker_id, fence, receipt_sha256, *, now=None):
        receipt_sha256 = _digest(receipt_sha256)
        def apply(state, job, stamp):
            self._worker(job, worker_id, fence, stamp)
            _require(job["phase"] == "STARTED" and job["start_fence"] == fence
                     and job["start_revision_sha256"] == job["revision_sha256"]
                     and job["approval_active"] and job["approval_sha256"] == job["start_approval_sha256"]
                     and not job["held"] and not state["rate_limited"], "completion_blocked")
            job.update(phase="RECORDED", receipt_sha256=receipt_sha256, completion_fence=fence,
                       owner=None, lease_until=None)
            return {"phase": "RECORDED", "observation_authenticated": False, "execution_authorized": False}
        return self._change("completion_observation", job_id, apply, now)

    def set_hold(self, job_id, *, now=None):
        def apply(state, job, stamp):
            job["held"] = True
            if job["phase"] == "STARTED":
                job.update(phase="UNKNOWN", unknown_reason="hold_after_start")
            self._fence(job)
            return {"held": True, "canonical_hold_changed": False, "execution_authorized": False}
        return self._change("local_hold", job_id, apply, now)

    def escalate(self, job_id, *, now=None):
        def apply(state, job, stamp):
            job["escalated"] = True
            return {"escalated": True, "human_notified": False, "execution_authorized": False}
        return self._change("escalation_recorded", job_id, apply, now)

    def record_429(self, *, now=None):
        def apply(state, job, stamp):
            state["rate_limited"] = True
            for item in state["jobs"].values():
                if item["phase"] == "STARTED":
                    item.update(phase="UNKNOWN", unknown_reason="rate_limit_after_start")
                if item["owner"] is not None:
                    self._fence(item)
                item["version"] += 1
            return {"rate_limited": True, "execution_authorized": False}
        return self._change("global_rate_429", None, apply, now)

    def recover_expired(self, *, now=None):
        def apply(state, job, stamp):
            recovered = []
            for item in state["jobs"].values():
                if item["owner"] is not None and item["lease_until"] <= stamp:
                    if item["phase"] == "STARTED":
                        item.update(phase="UNKNOWN", unknown_reason="lease_expired_after_start")
                    self._fence(item)
                    item["version"] += 1
                    recovered.append(item["job_id"])
            return {"fenced_jobs": recovered, "retry_authorized": False, "execution_authorized": False}
        return self._change("expired_lease_recovery", None, apply, now)

    def snapshot(self, *, expected_checkpoint_sha256=None):
        if expected_checkpoint_sha256 is not None:
            _digest(expected_checkpoint_sha256)
        with closing(self._connection()) as db:
            db.execute("BEGIN")
            state, head = self._read(db)
            checkpoint = self._checkpoint(state, head)
            _require(expected_checkpoint_sha256 is None or checkpoint == expected_checkpoint_sha256,
                     "checkpoint_mismatch")
            return {"schema": "keel.loki.recovery-snapshot.v1", "state": state,
                    "checkpoint_sha256": checkpoint, "event_head_sha256": head,
                    "human_approval_authenticated": False, "canonical_writes": 0,
                    "execution_authorized": False}

    def reconciliation_proposal(self, job_id, evidence, *, expected_checkpoint_sha256,
                                expected_evidence_sha256, now=None):
        """Read-only exact-attempt proposal; negative observations never permit retry."""
        _identifier(job_id)
        _digest(expected_evidence_sha256)
        evidence = _clone(evidence)
        _require(type(evidence) is dict and set(evidence) == {"schema", "workspace_id", "job_id", "attempt_id",
            "revision_sha256", "attempt_fence", "observation", "observed_at", "evidence_sha256"}, "evidence_schema_invalid")
        _require(digest(evidence) == expected_evidence_sha256, "evidence_pin_mismatch")
        _require(evidence["schema"] == "keel.loki.reconciliation-evidence.v1", "evidence_schema_invalid")
        _digest(evidence["evidence_sha256"])
        snap = self.snapshot(expected_checkpoint_sha256=expected_checkpoint_sha256)
        state = snap["state"]
        _require(job_id in state["jobs"], "job_not_found")
        job = state["jobs"][job_id]
        _require(job["phase"] == "UNKNOWN", "unknown_attempt_required")
        _require(evidence["workspace_id"] == self.workspace_id and evidence["job_id"] == job_id
                 and evidence["attempt_id"] == job["attempt_id"]
                 and evidence["revision_sha256"] == job["start_revision_sha256"]
                 and type(evidence["attempt_fence"]) is int and evidence["attempt_fence"] == job["start_fence"],
                 "evidence_attempt_binding_mismatch")
        _require(type(evidence["observed_at"]) in (int, float), "evidence_time_invalid")
        now, observed = _now(now), _now(evidence["observed_at"])
        _require(now >= state["last_now"] and job["started_at"] <= observed <= now and now - observed <= 90,
                 "evidence_time_invalid")
        _require(evidence["observation"] in ("CONFIRMED", "NOT_OBSERVED", "INCONCLUSIVE"), "evidence_observation_invalid")
        return {"schema": "keel.loki.reconciliation-proposal.v1", "status": "REVIEW_ONLY",
                "proposal": "REVIEW_RECORDED_CONFIRMATION" if evidence["observation"] == "CONFIRMED" else "MANUAL_INVESTIGATION",
                "job_id": job_id, "attempt_id": job["attempt_id"], "checkpoint_sha256": snap["checkpoint_sha256"],
                "evidence_sha256": expected_evidence_sha256, "observation_authenticated": False,
                "reconciliation_authorized": False, "retry_authorized": False,
                "canonical_writes": 0, "journal_writes": 0, "execution_authorized": False}


def _journal_trace_demo(home):
    """Project actual committed fixture snapshots, never model-generated states.

    This narrow happy path does not establish a refinement proof for the whole
    journal. Concrete hashes/worker identities are mapped to the finite model's
    declared IDs. Completion fencing is read from its persisted observation.
    """
    from .modelcheck import machine_sha256, replay_trace
    journal = RecoveryJournal(home, "synthetic-trace", now=200)
    journal.register("job", "a" * 64, now=201)
    def project(snapshot):
        state, job = snapshot["state"], snapshot["state"]["jobs"]["job"]
        revisions = {None: -1, "a" * 64: 0}
        approvals = {None: -1, "b" * 64: 0}
        return {"revision": revisions[job["revision_sha256"]],
                "approval": approvals[job["approval_sha256"]] if job["approval_active"] else -1,
                "fence": job["fence"], "owner": {None: "", "worker-a": "a"}[job["owner"]],
                "phase": {"IDLE": "IDLE", "STARTED": "ACTIVE", "UNKNOWN": "UNKNOWN", "RECORDED": "DONE"}[job["phase"]],
                "start_revision": revisions[job["start_revision_sha256"]],
                "start_approval": approvals[job["start_approval_sha256"]],
                "start_fence": job["start_fence"] if job["start_fence"] is not None else 0,
                "finish_fence": job["completion_fence"] if job["completion_fence"] is not None else 0,
                "held": job["held"], "ever_held": job["held"],
                "rate_limited": state["rate_limited"], "ever_rate_limited": state["rate_limited"],
                "ever_unknown": job["phase"] == "UNKNOWN", "escalated": job["escalated"]}
    first = journal.snapshot()
    trace = {"schema": "keel.loki.trace.v1", "machine_sha256": machine_sha256(),
             "initial": project(first), "laboratory_mutant": None, "steps": []}
    checkpoints = [first["checkpoint_sha256"]]
    def observe(action):
        snapshot = journal.snapshot()
        trace["steps"].append({"action": action, "state": project(snapshot)})
        checkpoints.append(snapshot["checkpoint_sha256"])
    journal.approve("job", "a" * 64, "b" * 64, now=202)
    observe("approve")
    lease = journal.claim("job", "worker-a", now=203)
    observe("claim_a")
    journal.start("job", "worker-a", lease["fence"], now=204)
    observe("start")
    journal.complete("job", "worker-a", lease["fence"], "c" * 64, now=205)
    observe("finish")
    journal.record_429(now=206)
    observe("rate_429")
    return {"schema": "keel.loki.journal-conformance.v1", "synthetic": True,
            "observation_source": "COMMITTED_SQLITE_SNAPSHOTS", "trace": trace,
            "snapshot_checkpoints": checkpoints,
            "trace_check": replay_trace(trace, expected_machine_sha256=trace["machine_sha256"]),
            "complete_implementation_refinement_proven": False,
            "projection_scope": "One fixed-revision worker happy path and a post-completion 429 stop",
            "execution_authorized": False}


def demo(home):
    """Measured synthetic restart rehearsal; requires a new caller-owned home."""
    from pathlib import Path
    _require(not Path(home).exists(), "new_demo_home_required")
    journal = RecoveryJournal(home, "synthetic-loki", now=100)
    journal.register("fixture", "a" * 64, now=101)
    journal.approve("fixture", "a" * 64, "b" * 64, now=102)
    lease = journal.claim("fixture", "worker-a", now=103)
    journal.start("fixture", "worker-a", lease["fence"], now=104)
    reopened = RecoveryJournal(home, "synthetic-loki", now=105)
    snap = reopened.snapshot()
    job = snap["state"]["jobs"]["fixture"]
    stale_blocked = False
    try:
        journal.complete("fixture", "worker-a", lease["fence"], "c" * 64, now=106)
    except RecoveryError:
        stale_blocked = True
    evidence = {"schema": "keel.loki.reconciliation-evidence.v1", "workspace_id": "synthetic-loki",
                "job_id": "fixture", "attempt_id": job["attempt_id"], "revision_sha256": "a" * 64,
                "attempt_fence": job["start_fence"], "observation": "NOT_OBSERVED", "observed_at": 106,
                "evidence_sha256": "d" * 64}
    proposal = reopened.reconciliation_proposal("fixture", evidence,
        expected_checkpoint_sha256=snap["checkpoint_sha256"], expected_evidence_sha256=digest(evidence), now=106)
    unchanged = reopened.snapshot()["checkpoint_sha256"] == snap["checkpoint_sha256"]
    reopened.record_429(now=107)
    again = RecoveryJournal(home, "synthetic-loki", now=108)
    final = again.snapshot()
    conformance = _journal_trace_demo(Path(home) / "trace-conformance")
    checks = {"restart_is_unknown": job["phase"] == "UNKNOWN", "stale_worker_blocked": stale_blocked,
              "proposal_read_only": unchanged, "negative_observation_never_retries": not proposal["retry_authorized"],
              "rate_429_survives_reopen": final["state"]["rate_limited"],
              "unknown_survives_reopen": final["state"]["jobs"]["fixture"]["phase"] == "UNKNOWN",
              "committed_trace_conforms": conformance["trace_check"]["status"] == "CONFORMING"}
    return {"schema": "keel.loki.recovery-demo.v1", "synthetic": True,
            "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "proposal": proposal, "checkpoint_sha256": final["checkpoint_sha256"], "journal_conformance": conformance,
            "network_calls": 0, "external_actions": 0, "canonical_writes": 0,
            "human_approval_authenticated": False, "execution_authorized": False}
