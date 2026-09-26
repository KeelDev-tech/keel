"""Durable trusted-host coordination; no reasoning engine or send capability.

Callbacks are installed by trusted embedding code, never supplied in task JSON.
A recorded callback result is an observation, not approval, factual truth or an
exactly-once external effect. Restart/uncertain effects preserve resource holds.
"""
from contextlib import closing
import hashlib
import hmac
import math
from pathlib import Path
import secrets
import sqlite3
import time

from keel_agent.state import LocalState, StateError, _private_file, _time
from keel_loki.common import clone, digest
from maintenance_workbench.keel_maint.contracts import canonical, strict_json


class CoordinatorError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise CoordinatorError(code)


def ident(value):
    require(type(value) is str and 0 < len(value) <= 128 and value == value.strip()
            and all(c.isalnum() or c in "_.:/@+-" for c in value), "identifier_invalid")
    return value


def sha(value):
    require(type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value), "sha256_invalid")
    return value


def stamp(value):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12, "clock_invalid")
    return float(value)


def exact(value, fields):
    require(type(value) is dict and set(value) == set(fields), "object_schema_invalid")


def dependencies(value):
    require(type(value) is dict and 1 <= len(value) <= 64, "dependencies_invalid")
    for key, revision in value.items():
        ident(key); sha(revision)
    return value


def task_document(value):
    value = clone(value)
    exact(value, {"schema", "task_id", "workspace_id", "scope_id", "handler_id", "account_id", "resource_id", "dependencies", "payload", "priority"})
    require(value["schema"] == "keel.muse.task.v1", "task_schema_invalid")
    for name in ("task_id", "workspace_id", "scope_id", "handler_id", "account_id", "resource_id"):
        ident(value[name])
    dependencies(value["dependencies"])
    require(type(value["payload"]) is dict and len(canonical(value["payload"])) <= 8192, "task_payload_invalid")
    require(type(value["priority"]) is int and 0 <= value["priority"] <= 10, "priority_invalid")
    return value


def event_document(value):
    value = clone(value)
    exact(value, {"schema", "event_id", "workspace_id", "scope_id", "kind", "payload"})
    require(value["schema"] == "keel.muse.event.v1", "event_schema_invalid")
    for name in ("event_id", "workspace_id", "scope_id"):
        ident(value[name])
    require(value["kind"] in ("SOURCE", "WAKE", "HOLD", "RATE_429"), "event_kind_invalid")
    payload = value["payload"]
    if value["kind"] == "SOURCE":
        exact(payload, {"source_id", "revision_sha256", "expected_previous_sha256"})
        ident(payload["source_id"]); sha(payload["revision_sha256"])
        if payload["expected_previous_sha256"] is not None:
            sha(payload["expected_previous_sha256"])
    elif value["kind"] == "HOLD":
        exact(payload, {"reason"}); ident(payload["reason"])
    else:
        exact(payload, set())
    return value


def host_snapshot(value, task, now, *, require_clear=True):
    value = clone(value)
    exact(value, {"schema", "workspace_id", "scope_id", "account_id", "resource_id", "dependencies",
                  "consent", "no_ai", "approval_current", "holds", "unknown_attempt", "rate_limited", "issued_at", "expires_at"})
    require(value["schema"] == "keel.muse.host-snapshot.v1", "host_schema_invalid")
    for key in ("workspace_id", "scope_id", "account_id", "resource_id"):
        require(value[key] == task[key], "host_scope_mismatch")
    require(dependencies(value["dependencies"]) == task["dependencies"], "host_revision_mismatch")
    for key in ("consent", "no_ai", "approval_current", "unknown_attempt", "rate_limited"):
        require(type(value[key]) is bool, "host_boolean_invalid")
    require(type(value["holds"]) is list and len(value["holds"]) <= 128, "host_holds_invalid")
    for item in value["holds"]:
        ident(item)
    issued, expiry = stamp(value["issued_at"]), stamp(value["expires_at"])
    require(issued <= now < expiry and expiry - issued <= 90, "host_snapshot_not_fresh")
    if require_clear:
        require(value["consent"] and value["approval_current"] and not value["no_ai"]
                and not value["holds"] and not value["unknown_attempt"] and not value["rate_limited"], "host_gate_blocked")
    return value


class Coordinator:
    """One long-lived controller with concurrent workers and passive clients.

    Writable construction is an explicit controller restart. A new instance
    fences old leases; STARTED work becomes UNKNOWN with locked resources.
    Use open_readonly for dashboards, never writable construction per CLI call.
    """
    def __init__(self, home, workspace_id, handlers, snapshot_provider, *, clock=time.time,
                 max_running=2, max_pending=128, max_calls=100):
        self.workspace_id = ident(workspace_id)
        require(type(handlers) is dict and 1 <= len(handlers) <= 32 and all(callable(v) for v in handlers.values()), "handlers_invalid")
        for key in handlers:
            ident(key)
        require(callable(snapshot_provider) and callable(clock), "trusted_callbacks_required")
        for value, bound in ((max_running, 16), (max_pending, 128), (max_calls, 10000)):
            require(type(value) is int and 1 <= value <= bound, "limits_invalid")
        self._handlers, self._provider, self._clock, self._readonly = dict(handlers), snapshot_provider, clock, False
        self._local = LocalState(Path(home) / "coordinator.sqlite3", workspace_id)
        limits = {"max_running": max_running, "max_pending": max_pending, "max_calls": max_calls, "handlers": sorted(handlers)}
        with self._local._transaction(stamp(clock())) as (db, now):
            db.execute("CREATE TABLE IF NOT EXISTS muse_state (slot TEXT PRIMARY KEY,payload TEXT NOT NULL,mac TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS muse_journal (sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL,mac TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS muse_inbox (event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,payload_sha256 TEXT NOT NULL,receipt TEXT NOT NULL,mac TEXT NOT NULL)")
            if not db.execute("SELECT COUNT(*) FROM muse_state").fetchone()[0]:
                require(db.execute("SELECT COUNT(*) FROM muse_journal").fetchone()[0] == 0
                        and db.execute("SELECT COUNT(*) FROM muse_inbox").fetchone()[0] == 0, "orphan_journal")
                state = {"schema": "keel.muse.coordinator-state.v1", "workspace_id": workspace_id,
                         "limits": limits, "generation": 1, "sequence": 0, "last_now": now,
                         "rate_limited": False, "calls_reserved": 0, "tasks": {}, "sources": {}, "holds": {},
                         "resources": {}, "account_served": {}, "enqueue_sequence": 0,
                         "events_received": 0, "wakeups_coalesced": 0}
                self._store(db, state, "initialize", None, now)
            else:
                state, _ = self._read(db)
                require(state["limits"] == limits, "persistent_configuration_mismatch")
                state["generation"] += 1
                for task in state["tasks"].values():
                    if task["status"] == "STARTED":
                        self._unknown(state, task, "controller_restarted")
                    elif task["status"] == "LEASED":
                        self._release(state, task)
                        task.update(status="READY", reason="controller_restarted_before_intent")
                self._store(db, state, "controller_restart", None, now)
            self._generation = state["generation"]

    @classmethod
    def open_readonly(cls, home, workspace_id):
        obj = cls.__new__(cls)
        obj.workspace_id, obj._readonly = ident(workspace_id), True
        local = LocalState.__new__(LocalState)
        local.path = (Path(home) / "coordinator.sqlite3").absolute()
        local.key_path = local.path.with_suffix(local.path.suffix + ".key")
        _private_file(local.path); _private_file(local.key_path)
        local._key = local.key_path.read_bytes()
        require(len(local._key) == 32, "host_key_invalid")
        local.key_id = hashlib.sha256(local._key).hexdigest()
        local.workspace_id = obj.workspace_id
        obj._local = local
        obj.snapshot()
        return obj

    def _connection(self):
        if not self._readonly:
            return self._local._connect()
        _private_file(self._local.path); _private_file(self._local.key_path)
        require(hashlib.sha256(self._local.key_path.read_bytes()).hexdigest() == self._local.key_id, "host_key_changed")
        db = sqlite3.connect(self._local.path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def _mac(self, domain, value):
        return hmac.new(self._local._key, domain.encode() + b"\0" + canonical(value), hashlib.sha256).hexdigest()

    def _read(self, db, *, history=False):
        rows = db.execute("SELECT * FROM muse_state").fetchall()
        require(len(rows) == 1 and rows[0]["slot"] == "state", "state_record_invalid")
        state = strict_json(rows[0]["payload"])
        require(state["workspace_id"] == self.workspace_id and state["schema"] == "keel.muse.coordinator-state.v1", "state_scope_invalid")
        require(hmac.compare_digest(rows[0]["mac"], self._mac("state", state)), "state_authentication_failed")
        require(1 <= state["sequence"] <= 10000, "journal_limit_invalid")
        require(db.execute("SELECT COUNT(*) FROM muse_journal").fetchone()[0] == state["sequence"], "journal_coverage_invalid")
        entries = db.execute("SELECT * FROM muse_journal ORDER BY sequence" if history else
                             "SELECT * FROM muse_journal ORDER BY sequence DESC LIMIT 1").fetchall()
        previous = "0" * 64
        for row in entries:
            payload = strict_json(row["payload"])
            require(payload["workspace_id"] == self.workspace_id and payload["sequence"] == row["sequence"]
                    and digest(payload) == row["sha256"] and hmac.compare_digest(row["mac"], self._mac("journal", payload)), "journal_authentication_failed")
            if history:
                require(payload["previous_sha256"] == previous, "journal_chain_invalid")
            previous = row["sha256"]
        require(payload["state_sha256"] == digest(state), "journal_state_binding_invalid")
        return state, previous

    def _store(self, db, state, action, task_id, now):
        require(state["sequence"] < 10000 and now >= state["last_now"], "journal_limit_or_clock_rollback")
        prior = db.execute("SELECT sha256 FROM muse_journal ORDER BY sequence DESC LIMIT 1").fetchone()
        state["sequence"] += 1
        state["last_now"] = now
        payload = {"schema": "keel.muse.coordinator-journal.v1", "workspace_id": self.workspace_id,
                   "sequence": state["sequence"], "previous_sha256": prior[0] if prior else "0" * 64,
                   "action": action, "task_id": task_id, "occurred_at": now, "state_sha256": digest(state)}
        db.execute("INSERT INTO muse_journal VALUES (?,?,?,?)", (state["sequence"], canonical(payload).decode(), digest(payload), self._mac("journal", payload)))
        db.execute("INSERT INTO muse_state VALUES ('state',?,?) ON CONFLICT(slot) DO UPDATE SET payload=excluded.payload,mac=excluded.mac",
                   (canonical(state).decode(), self._mac("state", state)))

    def _mutate(self, action, task_id, function):
        require(not self._readonly, "read_only_view")
        with self._local._transaction(stamp(self._clock())) as (db, transaction_started):
            # Acquiring the write transaction may block. Its input timestamp is
            # not sufficient for admission after that wait.
            now = stamp(self._clock())
            state, _ = self._read(db)
            require(now >= transaction_started and now >= state["last_now"], "host_clock_regressed")
            require(state["generation"] == self._generation or action == "callback_observation", "controller_fenced")
            result, changed = function(db, state, now)
            if changed:
                self._store(db, state, action, task_id, now)
            return clone(result)

    @staticmethod
    def _resources(task):
        document = task["document"]
        return ("account:" + document["account_id"], "browser:" + document["resource_id"])

    @classmethod
    def _release(cls, state, task):
        for key in cls._resources(task):
            resource = state["resources"].get(key)
            if resource and resource["task_id"] == task["document"]["task_id"] and not resource["uncertain"]:
                resource.update(task_id=None, worker_id=None, lease_until=None, fence=resource["fence"] + 1)
        task.update(owner=None, lease_until=None, fence=task["fence"] + 1)

    @classmethod
    def _unknown(cls, state, task, reason):
        task.update(status="UNKNOWN", reason=reason, owner=None, lease_until=None, fence=task["fence"] + 1)
        for key in cls._resources(task):
            resource = state["resources"].get(key)
            if resource and resource["task_id"] == task["document"]["task_id"]:
                resource.update(uncertain=True, worker_id=None, lease_until=None, fence=resource["fence"] + 1)

    @staticmethod
    def _reason(state, task):
        doc = task["document"]
        if state["rate_limited"]:
            return "global_rate_429"
        if state["holds"].get(doc["scope_id"]):
            return "persistent_scope_hold"
        known = state["sources"].get(doc["scope_id"], {})
        if any(known.get(key) != revision for key, revision in doc["dependencies"].items()):
            return "source_revision_missing_or_changed"
        return None

    def enqueue(self, task):
        task = task_document(task)
        require(task["workspace_id"] == self.workspace_id, "workspace_mismatch")
        require(not self._readonly, "read_only_view")
        require(task["handler_id"] in self._handlers, "handler_not_allowlisted")
        def apply(db, state, now):
            old = state["tasks"].get(task["task_id"])
            if old:
                require(digest(old["document"]) == digest(task), "task_id_payload_conflict")
                return {"task_id": task["task_id"], "status": old["status"], "duplicate": True, "execution_authorized": False}, False
            pending = sum(item["status"] in ("READY", "BLOCKED", "LEASED", "STARTED") for item in state["tasks"].values())
            require(pending < state["limits"]["max_pending"] and len(state["tasks"]) < 512, "queue_backpressure")
            state["enqueue_sequence"] += 1
            row = {"document": task, "enqueue_sequence": state["enqueue_sequence"], "enqueued_at": now,
                   "status": "READY", "reason": None, "owner": None, "fence": 0, "lease_until": None,
                   "intent_id": None, "snapshot_sha256": None, "outcome": None, "wake_pending": True}
            reason = self._reason(state, row)
            if reason:
                row.update(status="BLOCKED", reason=reason)
            state["tasks"][task["task_id"]] = row
            return {"task_id": task["task_id"], "status": row["status"], "duplicate": False, "execution_authorized": False}, True
        return self._mutate("enqueue", task["task_id"], apply)

    def ingest(self, event):
        event = event_document(event)
        require(event["workspace_id"] == self.workspace_id, "workspace_mismatch")
        event_hash = digest(event)
        def apply(db, state, now):
            prior = db.execute("SELECT * FROM muse_inbox WHERE event_id=?", (event["event_id"],)).fetchone()
            if prior:
                self._verify_inbox(prior)
                require(prior["payload_sha256"] == event_hash, "event_id_payload_conflict")
                return strict_json(prior["receipt"]), False
            require(state["events_received"] < 4096, "event_backpressure")
            scope, kind, payload = event["scope_id"], event["kind"], event["payload"]
            if kind == "SOURCE":
                require(scope in state["sources"] or len(state["sources"]) < 512, "source_scope_limit")
                known = state["sources"].setdefault(scope, {})
                require(payload["source_id"] in known or len(known) < 64, "source_limit")
                require(known.get(payload["source_id"]) == payload["expected_previous_sha256"], "source_cursor_conflict")
                known[payload["source_id"]] = payload["revision_sha256"]
            elif kind == "HOLD":
                reasons = state["holds"].setdefault(scope, [])
                if payload["reason"] not in reasons:
                    require(len(reasons) < 128, "hold_limit")
                    reasons.append(payload["reason"])
            elif kind == "RATE_429":
                state["rate_limited"] = True
            affected, coalesced = 0, 0
            for task in state["tasks"].values():
                if kind != "RATE_429" and task["document"]["scope_id"] != scope:
                    continue
                if task["status"] in ("RECORDED", "UNKNOWN"):
                    continue
                reason = self._reason(state, task)
                if task["status"] == "STARTED":
                    if reason:
                        self._unknown(state, task, reason)
                    continue
                if task["status"] == "LEASED":
                    if reason:
                        self._release(state, task)
                        task.update(status="BLOCKED", reason=reason)
                    continue
                affected += 1
                if task["wake_pending"]:
                    coalesced += 1
                task.update(status="BLOCKED" if reason else "READY", reason=reason, wake_pending=True)
            state["events_received"] += 1
            state["wakeups_coalesced"] += coalesced
            receipt = {"schema": "keel.muse.event-receipt.v1", "event_id": event["event_id"],
                       "payload_sha256": event_hash, "affected_tasks": affected, "coalesced_wakeups": coalesced,
                       "execution_authorized": False}
            envelope = {"event": event, "payload_sha256": event_hash, "receipt": receipt}
            db.execute("INSERT INTO muse_inbox VALUES (?,?,?,?,?)", (event["event_id"], canonical(event).decode(), event_hash,
                       canonical(receipt).decode(), self._mac("inbox", envelope)))
            return receipt, True
        return self._mutate("event_ingested", None, apply)

    def _verify_inbox(self, row):
        event, receipt = strict_json(row["payload"]), strict_json(row["receipt"])
        require(event["event_id"] == row["event_id"] and digest(event) == row["payload_sha256"]
                and hmac.compare_digest(row["mac"], self._mac("inbox", {"event": event,
                    "payload_sha256": row["payload_sha256"], "receipt": receipt})), "inbox_authentication_failed")
        return {"event": event, "receipt": receipt}

    def _claim(self, worker_id, lease_seconds):
        def apply(db, state, now):
            changed = False
            for task in state["tasks"].values():
                if task["status"] in ("STARTED", "LEASED") and task["lease_until"] <= now:
                    if task["status"] == "STARTED":
                        self._unknown(state, task, "lease_expired_after_intent")
                    else:
                        self._release(state, task)
                        task.update(status="READY", reason="lease_expired_before_intent")
                    changed = True
            if state["rate_limited"] or state["calls_reserved"] >= state["limits"]["max_calls"]:
                return {"status": "IDLE", "reason": "persistent_rate_or_budget_gate", "handler_calls_attempted": 0, "execution_authorized": False}, changed
            active = sum(task["status"] in ("LEASED", "STARTED") for task in state["tasks"].values())
            if active >= state["limits"]["max_running"]:
                return {"status": "IDLE", "reason": "concurrency_limit", "handler_calls_attempted": 0, "execution_authorized": False}, changed
            ready = []
            for task in state["tasks"].values():
                if task["status"] != "READY":
                    continue
                reason = self._reason(state, task)
                if reason:
                    task.update(status="BLOCKED", reason=reason, wake_pending=False)
                    changed = True
                    continue
                if any(state["resources"].get(key, {}).get("task_id") is not None
                       or state["resources"].get(key, {}).get("uncertain", False) for key in self._resources(task)):
                    continue
                ready.append(task)
            if not ready:
                return {"status": "IDLE", "reason": "no_ready_unlocked_task", "handler_calls_attempted": 0, "execution_authorized": False}, changed
            def rank(task):
                doc = task["document"]
                effective = min(10, doc["priority"] + int((now - task["enqueued_at"]) // 30))
                return (state["account_served"].get(doc["account_id"], 0), -effective, task["enqueue_sequence"])
            task = min(ready, key=rank)
            task.update(status="LEASED", reason=None, owner=worker_id, fence=task["fence"] + 1,
                        lease_until=now + lease_seconds, wake_pending=False)
            for key in self._resources(task):
                prior = state["resources"].get(key, {"fence": 0})
                state["resources"][key] = {"fence": prior["fence"] + 1, "task_id": task["document"]["task_id"],
                    "worker_id": worker_id, "lease_until": now + lease_seconds, "uncertain": False}
            account = task["document"]["account_id"]
            state["account_served"][account] = state["account_served"].get(account, 0) + 1
            return {"status": "LEASED", "task": task, "generation": state["generation"]}, True
        return self._mutate("lease_claimed", None, apply)

    @staticmethod
    def _owned(state, task, worker_id, fence, generation, now):
        require(state["generation"] == generation and task["owner"] == worker_id and task["fence"] == fence
                and task["lease_until"] is not None and now < task["lease_until"], "stale_or_expired_worker")

    def _block_before_effect(self, task_id, worker_id, fence, generation, reason, *, uncertain_after_intent=False):
        def apply(db, state, now):
            task = state["tasks"][task_id]
            if state["generation"] != generation or task["fence"] != fence or task["owner"] != worker_id:
                return {"status": task["status"], "reason": "worker_fenced", "handler_calls_attempted": 0, "execution_authorized": False}, False
            if task["status"] == "STARTED" and (uncertain_after_intent or now >= task["lease_until"]):
                self._unknown(state, task, reason)
                return {"status": "UNKNOWN", "reason": reason, "handler_calls_attempted": 0, "execution_authorized": False}, True
            self._release(state, task)
            task.update(status="BLOCKED", reason=reason, wake_pending=False)
            return {"status": "BLOCKED", "reason": reason, "handler_calls_attempted": 0, "execution_authorized": False}, True
        return self._mutate("pre_effect_block", task_id, apply)

    def worker_once(self, worker_id, *, lease_seconds=60):
        require(not self._readonly, "read_only_view")
        ident(worker_id)
        require(type(lease_seconds) in (int, float) and 1 <= lease_seconds <= 3600, "lease_duration_invalid")
        claimed = self._claim(worker_id, lease_seconds)
        if claimed["status"] == "IDLE":
            return claimed
        task = claimed["task"]; doc = task["document"]
        task_id, fence, generation = doc["task_id"], task["fence"], claimed["generation"]
        try:
            first = self._observe_host(doc)
        except Exception:
            return self._block_before_effect(task_id, worker_id, fence, generation, "host_snapshot_blocked")
        def start(db, state, now):
            current = state["tasks"][task_id]
            self._owned(state, current, worker_id, fence, generation, now)
            require(current["status"] == "LEASED" and not self._reason(state, current), "admission_changed")
            require(state["calls_reserved"] < state["limits"]["max_calls"], "call_budget_exhausted")
            host_snapshot(first, doc, now)
            state["calls_reserved"] += 1
            current.update(status="STARTED", intent_id="intent_" + secrets.token_hex(16), snapshot_sha256=digest(first))
            return {"intent_id": current["intent_id"]}, True
        try:
            intent = self._mutate("intent_started", task_id, start)
            # Re-read canonical host observations after the durable intent and
            # immediately before callback admission. The effect adapter must
            # itself recheck its authoritative gateway before actual effects.
            fresh = self._observe_host(doc)
            def admit(db, state, now):
                current = state["tasks"][task_id]
                self._owned(state, current, worker_id, fence, generation, now)
                require(current["status"] == "STARTED" and not self._reason(state, current), "admission_changed")
                host_snapshot(fresh, doc, now)
                current["snapshot_sha256"] = digest(fresh)
                return None, True
            self._mutate("callback_admitted", task_id, admit)
        except Exception:
            return self._block_before_effect(task_id, worker_id, fence, generation, "fresh_admission_blocked")
        context = {"schema": "keel.muse.callback-context.v1", "task": clone(doc), "intent_id": intent["intent_id"],
                   "worker_id": worker_id, "fence": fence, "generation": generation,
                   "snapshot": fresh, "snapshot_sha256": digest(fresh), "execution_authorized": False}
        try:
            # The admission commit itself can be slow. Check the committed
            # current lease/fence and snapshot against a clock read AFTER all
            # potentially blocking state reads, immediately before the callback.
            boundary = self.snapshot()["state"]
            boundary_now = stamp(self._clock())
            require(boundary_now >= boundary["last_now"], "host_clock_regressed")
            current = boundary["tasks"][task_id]
            self._owned(boundary, current, worker_id, fence, generation, boundary_now)
            require(current["status"] == "STARTED" and not self._reason(boundary, current), "admission_changed")
            host_snapshot(fresh, doc, boundary_now)
        except Exception:
            return self._block_before_effect(task_id, worker_id, fence, generation,
                "callback_boundary_expired_or_changed", uncertain_after_intent=True)
        try:
            outcome = clone(self._handlers[doc["handler_id"]](context))
            exact(outcome, {"status", "receipt_sha256"})
            require(outcome["status"] in ("RECORDED", "BLOCKED", "UNKNOWN", "RATE_429"), "handler_outcome_invalid")
            if outcome["status"] == "RECORDED":
                sha(outcome["receipt_sha256"])
            else:
                require(outcome["receipt_sha256"] is None, "handler_receipt_invalid")
        except Exception:
            outcome = {"status": "UNKNOWN", "receipt_sha256": None}
        def finish(db, state, now):
            current = state["tasks"][task_id]
            stopped = outcome["status"] == "RATE_429"
            if stopped:
                state["rate_limited"] = True
                for other in state["tasks"].values():
                    if other["status"] == "STARTED":
                        self._unknown(state, other, "global_rate_429")
            if (current["status"] != "STARTED" or current["fence"] != fence
                    or current["owner"] != worker_id or state["generation"] != generation):
                return {"status": current["status"], "reason": "stale_result_ignored", "handler_calls_attempted": 1, "execution_authorized": False}, stopped
            current["outcome"] = outcome
            if now >= current["lease_until"] or self._reason(state, current) or outcome["status"] in ("UNKNOWN", "RATE_429"):
                self._unknown(state, current, "uncertain_callback_outcome")
            else:
                self._release(state, current)
                current.update(status="RECORDED", reason="callback_observation_" + outcome["status"].lower())
            return {"status": current["status"], "task_id": task_id, "outcome": outcome,
                    "handler_calls_attempted": 1, "observation_authenticated": False, "execution_authorized": False}, True
        return self._mutate("callback_observation", task_id, finish)

    def _observe_host(self, document):
        value = host_snapshot(self._provider(clone(document)), document, stamp(self._clock()), require_clear=False)
        if value["rate_limited"]:
            self.ingest({"schema": "keel.muse.event.v1", "event_id": "host429:" + digest(value),
                         "workspace_id": self.workspace_id, "scope_id": document["scope_id"],
                         "kind": "RATE_429", "payload": {}})
        return host_snapshot(value, document, stamp(self._clock()))

    def snapshot(self, *, expected_checkpoint_sha256=None):
        if expected_checkpoint_sha256 is not None:
            sha(expected_checkpoint_sha256)
        with closing(self._connection()) as db:
            db.execute("BEGIN")
            state, head = self._read(db, history=True)
            rows = db.execute("SELECT * FROM muse_inbox ORDER BY event_id").fetchall()
            require(len(rows) == state["events_received"], "inbox_coverage_invalid")
            inbox_hash = digest([self._verify_inbox(row) for row in rows])
            checkpoint = digest({"state": state, "event_head_sha256": head, "inbox_sha256": inbox_hash})
            require(expected_checkpoint_sha256 is None or checkpoint == expected_checkpoint_sha256, "checkpoint_mismatch")
            return {"schema": "keel.muse.coordinator-snapshot.v1", "workspace_id": self.workspace_id,
                    "state": state, "checkpoint_sha256": checkpoint, "event_head_sha256": head,
                    "inbox_sha256": inbox_hash, "execution_authorized": False}


def inspect_home(home, workspace_id):
    return Coordinator.open_readonly(home, workspace_id).snapshot()


def local_agent_handler(agent, worker_id, *, transport=None):
    """Trusted adapter delegates reasoning to existing LocalAgent.worker_once.

    The LocalAgent retains its own queue, review prerequisites and durable state.
    A broker task is a wakeup for that queue, not an alternate approval path.
    """
    from keel_agent.runtime import LocalAgent
    require(isinstance(agent, LocalAgent), "existing_local_agent_required")
    ident(worker_id)
    def invoke(context):
        doc = context["task"]
        require(doc["workspace_id"] == agent.state.workspace_id
                and doc["scope_id"] == "local-agent:" + agent.state.workspace_id
                and doc["account_id"] == "local-agent:" + agent.state.workspace_id
                and doc["resource_id"] == "local-agent:" + digest(str(agent.home)),
                "local_agent_workspace_queue_scope_required")
        result = agent.worker_once(worker_id, transport=transport)
        # Generic failures remain uncertain; completed processing does not mean
        # the underlying review passed or any application was submitted.
        if result.get("state") in ("IDLE", "COMPLETED"):
            return {"status": "RECORDED", "receipt_sha256": digest(result)}
        return {"status": "UNKNOWN", "receipt_sha256": None}
    return invoke


def recovery_guarded_provider(base_provider, journal, job_bindings, *, revision_source_id):
    """Intersect canonical host gates with an existing recovery journal read.

    The mapping from broker scope to journal job and the dependency key come
    from trusted configuration. No journal is constructed, approved, restarted
    or reconciled by this adapter; only the existing verified snapshot is read.
    """
    from keel_loki.recovery import RecoveryJournal
    require(callable(base_provider) and isinstance(journal, RecoveryJournal), "trusted_recovery_provider_required")
    require(type(job_bindings) is dict and 1 <= len(job_bindings) <= 128, "job_bindings_invalid")
    bindings = {ident(scope): ident(job_id) for scope, job_id in job_bindings.items()}
    ident(revision_source_id)
    def observe(task):
        require(task["scope_id"] in bindings and task["workspace_id"] == journal.workspace_id, "recovery_scope_unbound")
        snapshot = journal.snapshot()
        job = snapshot["state"]["jobs"].get(bindings[task["scope_id"]])
        require(job is not None and revision_source_id in task["dependencies"]
                and job["revision_sha256"] == task["dependencies"][revision_source_id], "recovery_revision_mismatch")
        result = clone(base_provider(clone(task)))
        for name in ("approval_current", "unknown_attempt", "rate_limited"):
            require(type(result.get(name)) is bool, "host_boolean_invalid")
        require(type(result.get("holds")) is list, "host_holds_invalid")
        result["approval_current"] = result["approval_current"] and job["approval_active"]
        result["unknown_attempt"] = result["unknown_attempt"] or job["phase"] == "UNKNOWN"
        result["rate_limited"] = result["rate_limited"] or snapshot["state"]["rate_limited"]
        additions = (["recovery_local_hold"] if job["held"] else [])
        if job["phase"] in ("STARTED", "RECORDED"):
            additions.append("recovery_attempt_already_started")
        result["holds"] = sorted(set(result["holds"] + additions))
        return result
    return observe


def demo(home):
    """Real SQLite orchestration with injected callbacks and no network/model."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    require(not Path(home).exists(), "new_demo_home_required")
    clock = [100.0]; calls = []
    entered, released = threading.Event(), threading.Event()
    def observe(task):
        return {"schema": "keel.muse.host-snapshot.v1", **{key: task[key] for key in
                ("workspace_id", "scope_id", "account_id", "resource_id", "dependencies")},
                "consent": True, "no_ai": False, "approval_current": True, "holds": [],
                "unknown_attempt": False, "rate_limited": False, "issued_at": clock[0], "expires_at": clock[0] + 60}
    def handler(context):
        calls.append(context["task"]["task_id"])
        entered.set()
        require(released.wait(5), "demo_worker_wait_expired")
        return {"status": "RECORDED", "receipt_sha256": digest(context)}
    coordinator = Coordinator(home, "fixture", {"review": handler}, observe, clock=lambda: clock[0])
    def task(identifier, scope="scope-a", account="account-a"):
        return {"schema": "keel.muse.task.v1", "task_id": identifier, "workspace_id": "fixture", "scope_id": scope,
                "handler_id": "review", "account_id": account, "resource_id": account,
                "dependencies": {"answers": "a" * 64}, "payload": {}, "priority": 0}
    coordinator.enqueue(task("first"))
    idle = coordinator.worker_once("worker-a")
    event = {"schema": "keel.muse.event.v1", "event_id": "source-1", "workspace_id": "fixture", "scope_id": "scope-a",
             "kind": "SOURCE", "payload": {"source_id": "answers", "revision_sha256": "a" * 64, "expected_previous_sha256": None}}
    receipt = coordinator.ingest(event)
    repeated = coordinator.ingest(event)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(coordinator.worker_once, "worker-a")
        require(entered.wait(5), "demo_worker_start_expired")
        try:
            concurrent = coordinator.worker_once("worker-duplicate")
        finally:
            released.set()
        recorded = active.result(timeout=5)
    coordinator.enqueue(task("interrupted", account="account-interrupted"))
    def interrupted(context):
        raise SystemExit("injected controller interruption; no external action")
    coordinator._handlers["review"] = interrupted
    interruption_observed = False
    try:
        coordinator.worker_once("worker-interrupted")
    except SystemExit:
        interruption_observed = True
    started_persisted = Coordinator.open_readonly(home, "fixture").snapshot()["state"]["tasks"]["interrupted"]["status"] == "STARTED"
    coordinator = Coordinator(home, "fixture", {"review": handler}, observe, clock=lambda: clock[0])
    restarted_unknown = coordinator.snapshot()["state"]["tasks"]["interrupted"]["status"] == "UNKNOWN"
    coordinator.enqueue(task("second", account="account-b"))
    coordinator._handlers["review"] = lambda context: {"status": "RATE_429", "receipt_sha256": None}
    limited = coordinator.worker_once("worker-b")
    reopened = Coordinator(home, "fixture", {"review": handler}, observe, clock=lambda: clock[0])
    snap = reopened.snapshot()
    reader = Coordinator.open_readonly(home, "fixture")
    checks = {"missing_evidence_idle_without_handler": idle["handler_calls_attempted"] == 0,
              "duplicate_event_same_receipt": receipt == repeated,
              "source_wakes_real_callback": recorded["status"] == "RECORDED" and calls == ["first"],
              "concurrent_duplicate_never_calls": concurrent["handler_calls_attempted"] == 0,
              "durable_started_before_interruption": interruption_observed and started_persisted,
              "restart_converts_uncertain_to_unknown": restarted_unknown,
              "uncertain_429_retained": limited["status"] == "UNKNOWN" and snap["state"]["tasks"]["second"]["status"] == "UNKNOWN",
              "global_429_persistent": snap["state"]["rate_limited"] and reopened.worker_once("worker-c")["handler_calls_attempted"] == 0,
              "readonly_no_epoch_change": reader.snapshot()["checkpoint_sha256"] == snap["checkpoint_sha256"]}
    return {"schema": "keel.muse.coordinator-demo.v1", "synthetic": True,
            "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "callback_provenance": "INJECTED", "model_calls": 0, "network_calls": 0,
            "interruption_provenance": "INJECTED_BASEEXCEPTION; separate tests exercise real process exit",
            "external_actions": 0, "checkpoint_sha256": snap["checkpoint_sha256"], "execution_authorized": False}
