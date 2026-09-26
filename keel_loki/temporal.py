"""Local, scoped, bitemporal evidence history and dependency projections.

This module records observations; it cannot authenticate a reviewer, confer
approval, or write to Keel's canonical application pipeline. Hashes expose
accidental changes, not a database owner's coordinated rewrite of history.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


class MemoryError(ValueError):
    """Fixed diagnostic without private claim values."""


USES = frozenset({"application_fact", "planning", "review"})
ZERO = "0" * 64
FLAGS = {"execution_authorized": False, "canonical_writes": 0,
         "reviewer_identity_authenticated": False, "source_truth_verified": False}


def _check(condition, code):
    if not condition:
        raise MemoryError(code)


def _raw(value):
    def check_json(item, depth=0):
        _check(depth <= 32, "json_too_deep")
        if type(item) is dict:
            _check(all(type(key) is str for key in item), "invalid_json_key")
            for child in item.values():
                check_json(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check_json(child, depth + 1)
        else:
            _check(item is None or type(item) in (str, int, float, bool), "invalid_json_type")
    check_json(value)
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
        _check(len(text.encode()) <= 65536, "record_too_large")
        return text
    except (TypeError, ValueError, RecursionError):
        raise MemoryError("invalid_json") from None


def _clone(value):
    return json.loads(_raw(value))


def _hash(value):
    return hashlib.sha256(_raw(value).encode()).hexdigest()


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names.split()), "invalid_fields")


def _id(value):
    _check(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value),
           "invalid_id")
    return value


def _sha(value):
    _check(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_sha256")
    return value


def _stamp(value):
    _check(type(value) is int and 0 <= value <= 253402300799, "invalid_timestamp")
    return value


def _ids(value, *, nonempty=False):
    _check(type(value) is list and len(value) <= 256 and
           (not nonempty or value), "invalid_id_list")
    for item in value:
        _id(item)
    _check(len(value) == len(set(value)), "duplicate_id")
    return value


def _uses(value):
    _check(type(value) is list and bool(value)
           and all(type(item) is str and item in USES for item in value)
           and len(value) == len(set(value)), "invalid_permitted_use")


def _claim(value):
    _fields(value, "revision_id subject_id key value source scope permitted_uses valid_from valid_until supersedes")
    for field in ("revision_id", "subject_id", "key", "scope"):
        _id(value[field])
    _uses(value["permitted_uses"])
    _stamp(value["valid_from"])
    if value["valid_until"] is not None:
        _stamp(value["valid_until"])
        _check(value["valid_until"] > value["valid_from"], "invalid_valid_interval")
    _ids(value["supersedes"])
    _check(value["revision_id"] not in value["supersedes"], "self_supersession")
    source = value["source"]
    _fields(source, "source_id sha256 classification allowed_scopes permitted_uses")
    _id(source["source_id"])
    _sha(source["sha256"])
    _check(type(source["classification"]) is str and
           source["classification"] in {"public", "personal", "sensitive"}, "invalid_classification")
    _ids(source["allowed_scopes"], nonempty=True)
    _uses(source["permitted_uses"])
    _check(value["scope"] in source["allowed_scopes"] and
           set(value["permitted_uses"]) <= set(source["permitted_uses"]), "source_use_forbidden")
    # Values may be structured facts, but may not embed executable capabilities.
    _raw(value["value"])


def _observation(value):
    _fields(value, "observation_id revision_id reviewer_id verdict source_sha256 observed_at")
    for field in ("observation_id", "revision_id", "reviewer_id"):
        _id(value[field])
    _check(type(value["verdict"]) is str and value["verdict"] in {"verified", "rejected"}, "invalid_verdict")
    _sha(value["source_sha256"])
    _stamp(value["observed_at"])


def _artifact(value):
    _fields(value, "artifact_id subject_id scope purpose sha256 dependencies")
    for field in ("artifact_id", "subject_id", "scope"):
        _id(value[field])
    _check(type(value["purpose"]) is str and value["purpose"] in USES, "invalid_permitted_use")
    _sha(value["sha256"])
    _ids(value["dependencies"], nonempty=True)


def _approval(value):
    _fields(value, "approval_id artifact_id artifact_sha256 reviewer_id source_record_sha256 observed_at decision scope")
    for field in ("approval_id", "artifact_id", "reviewer_id", "scope"):
        _id(value[field])
    _sha(value["artifact_sha256"])
    _sha(value["source_record_sha256"])
    _stamp(value["observed_at"])
    _check(type(value["decision"]) is str and value["decision"] in {"approved", "rejected"}, "invalid_decision")


VALIDATORS = {"claim": _claim, "observation": _observation,
              "artifact": _artifact, "approval_observation": _approval}
IDENTITIES = {"claim": "revision_id", "observation": "observation_id",
              "artifact": "artifact_id", "approval_observation": "approval_id"}


class TemporalMemory:
    """SQLite history with valid time and monotonic host-recorded time.

    The configured home must be private (0700), and its parent must exist.
    ``clock`` supplies trusted integer epoch seconds. Regressing clocks reject
    new events; idempotent repeats retain the original timestamp. The caller is
    responsible for authenticating observation producers outside this store.
    """

    def __init__(self, home, clock=None):
        self.home = Path(home).absolute()
        self.clock = clock or (lambda: int(time.time()))
        _check(self.home.parent.exists(), "parent_missing")
        _check(self.home.parent.resolve(strict=True) == self.home.parent, "symlink_forbidden")
        try:
            self.home.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise MemoryError("home_unavailable") from None
        self._directory_identity = self._private(self.home, directory=True)
        self.db_path = self.home / "temporal.sqlite3"
        created = False
        if not self.db_path.exists() and not self.db_path.is_symlink():
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
                created = True
            except OSError:
                raise MemoryError("database_create_failed") from None
        self._file_identity = self._private(self.db_path)
        self._closed = False
        with self._connection(write=True) as connection:
            if created:
                connection.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, recorded_at INTEGER NOT NULL, payload TEXT NOT NULL, previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL, UNIQUE(kind,identity))")
                connection.execute("CREATE TABLE head (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL, event_count INTEGER NOT NULL, sha256 TEXT NOT NULL)")
                connection.execute("INSERT INTO head VALUES (1, 'keel.loki.temporal.v1', 0, ?)", (ZERO,))
                connection.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'immutable_event'); END")
                connection.execute("CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'immutable_event'); END")
            self._history(connection)

    @staticmethod
    def _private(path, directory=False):
        try:
            _check(path.resolve(strict=True) == path, "symlink_forbidden")
            info = path.stat()
        except OSError:
            raise MemoryError("storage_unavailable") from None
        expected = 0o700 if directory else 0o600
        _check((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
               and stat.S_IMODE(info.st_mode) == expected, "storage_not_private")
        _check(not hasattr(os, "getuid") or info.st_uid == os.getuid(), "storage_owner_mismatch")
        _check(directory or info.st_nlink == 1, "hardlink_forbidden")
        return (info.st_dev, info.st_ino)

    @contextmanager
    def _connection(self, write=False):
        _check(not self._closed, "store_closed")
        _check(self._private(self.home, directory=True) == self._directory_identity and
               self._private(self.db_path) == self._file_identity, "storage_replaced")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.db_path.with_name(self.db_path.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                try:
                    self._private(sidecar)
                except MemoryError as error:
                    # Another transaction may have just committed and removed
                    # its private journal. Existing unsafe paths still fail.
                    if str(error) != "storage_unavailable" or sidecar.exists() or sidecar.is_symlink():
                        raise
        connection = None
        try:
            connection = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            raise MemoryError("database_integrity_error") from None
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _relations(kind, payload, history, recorded_at):
        indexed = {name: {} for name in VALIDATORS}
        for event in history:
            indexed[event["kind"]][event["identity"]] = event["payload"]
        _check(payload[IDENTITIES[kind]] not in indexed[kind], "duplicate_record_id")
        if kind == "claim":
            for revision in payload["supersedes"]:
                old = indexed["claim"].get(revision)
                _check(old is not None, "unknown_superseded_revision")
                _check(all(old[key] == payload[key] for key in ("subject_id", "key", "scope")),
                       "cross_scope_supersession")
        elif kind == "observation":
            claim = indexed["claim"].get(payload["revision_id"])
            _check(claim is not None, "unknown_revision")
            _check(claim["source"]["sha256"] == payload["source_sha256"], "source_hash_mismatch")
            _check(payload["observed_at"] <= recorded_at, "future_observation")
        elif kind == "artifact":
            for revision in payload["dependencies"]:
                claim = indexed["claim"].get(revision)
                _check(claim is not None, "unknown_dependency")
                _check(claim["subject_id"] == payload["subject_id"] and
                       claim["scope"] == payload["scope"] and
                       payload["purpose"] in claim["permitted_uses"], "cross_scope_dependency")
        else:
            artifact = indexed["artifact"].get(payload["artifact_id"])
            _check(artifact is not None, "unknown_artifact")
            _check(artifact["sha256"] == payload["artifact_sha256"] and
                   artifact["scope"] == payload["scope"], "approval_binding_mismatch")
            _check(payload["observed_at"] <= recorded_at, "future_observation")

    def _history(self, connection):
        head = connection.execute("SELECT * FROM head").fetchall()
        _check(len(head) == 1 and head[0]["singleton"] == 1 and
               head[0]["schema"] == "keel.loki.temporal.v1", "invalid_head")
        history = []
        previous = ZERO
        last_time = 0
        idem = set()
        for row in connection.execute("SELECT * FROM events ORDER BY sequence"):
            _check(row["sequence"] == len(history) + 1, "sequence_gap")
            _check(row["kind"] in VALIDATORS, "invalid_event_kind")
            _id(row["idempotency_key"])
            _check(row["idempotency_key"] not in idem, "duplicate_idempotency_key")
            idem.add(row["idempotency_key"])
            _stamp(row["recorded_at"])
            _check(row["recorded_at"] >= last_time, "clock_regression")
            _check(type(row["payload"]) is str and len(row["payload"]) <= 65536, "record_too_large")
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                raise MemoryError("invalid_stored_json") from None
            _check(_raw(payload) == row["payload"], "noncanonical_payload")
            VALIDATORS[row["kind"]](payload)
            _check(row["identity"] == payload[IDENTITIES[row["kind"]]], "identity_mismatch")
            event = dict(row)
            event["payload"] = payload
            material = {key: value for key, value in event.items() if key != "event_sha256"}
            _check(row["previous_sha256"] == previous and _hash(material) == row["event_sha256"],
                   "history_hash_mismatch")
            self._relations(row["kind"], payload, history, row["recorded_at"])
            history.append(event)
            previous = row["event_sha256"]
            last_time = row["recorded_at"]
        _check(head[0]["event_count"] == len(history) and head[0]["sha256"] == previous,
               "head_mismatch")
        return history

    def _append(self, kind, payload, idempotency_key):
        payload = _clone(payload)
        VALIDATORS[kind](payload)
        _id(idempotency_key)
        with self._connection(write=True) as connection:
            history = self._history(connection)
            for old in history:
                if old["idempotency_key"] == idempotency_key:
                    _check(old["kind"] == kind and old["payload"] == payload, "idempotency_conflict")
                    return {"created": False, "event": _clone(old), **FLAGS}
            recorded_at = _stamp(self.clock())
            _check(not history or recorded_at >= history[-1]["recorded_at"], "clock_regression")
            self._relations(kind, payload, history, recorded_at)
            event = {"sequence": len(history) + 1, "kind": kind,
                     "identity": payload[IDENTITIES[kind]], "idempotency_key": idempotency_key,
                     "recorded_at": recorded_at, "payload": payload,
                     "previous_sha256": history[-1]["event_sha256"] if history else ZERO}
            event["event_sha256"] = _hash(event)
            connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                event["sequence"], kind, event["identity"], idempotency_key, recorded_at,
                _raw(payload), event["previous_sha256"], event["event_sha256"]))
            connection.execute("UPDATE head SET event_count=?, sha256=? WHERE singleton=1",
                               (event["sequence"], event["event_sha256"]))
        return {"created": True, "event": _clone(event), **FLAGS}

    def record_claim(self, claim, idempotency_key):
        return self._append("claim", claim, idempotency_key)

    def record_observation(self, observation, idempotency_key):
        return self._append("observation", observation, idempotency_key)

    def register_artifact(self, artifact, idempotency_key):
        return self._append("artifact", artifact, idempotency_key)

    def register_approval(self, approval_observation, idempotency_key):
        """Record supplied reviewer evidence; never create or authenticate approval."""
        return self._append("approval_observation", approval_observation, idempotency_key)

    def verify(self, expected_head_sha256=None):
        with self._connection() as connection:
            history = self._history(connection)
        head = history[-1]["event_sha256"] if history else ZERO
        if expected_head_sha256 is not None:
            _sha(expected_head_sha256)
            _check(head == expected_head_sha256, "trusted_checkpoint_mismatch")
        return {"schema": "keel.loki.memory_integrity.v1", "status": "PASS",
                "event_count": len(history), "head_sha256": head,
                "external_checkpoint_matched": expected_head_sha256 is not None,
                "tamper_proof": False, **FLAGS}

    def history(self):
        """Privileged local audit API: contains private values; do not log publicly."""
        with self._connection() as connection:
            return _clone(self._history(connection))

    @staticmethod
    def _known(history, known_at):
        if known_at is None:
            return history
        _stamp(known_at)
        return [event for event in history if event["recorded_at"] <= known_at]

    @staticmethod
    def _resolve(history, subject_id, key, scope, purpose, valid_at):
        claims = [event["payload"] for event in history if event["kind"] == "claim"
                  and event["payload"]["subject_id"] == subject_id
                  and event["payload"]["key"] == key and event["payload"]["scope"] == scope
                  and event["payload"]["valid_from"] <= valid_at
                  and (event["payload"]["valid_until"] is None or valid_at < event["payload"]["valid_until"])]
        # Supersession applies before permitted-use filtering: a restrictive
        # correction must not expose the old revision through a different use.
        all_claims = {event["identity"]: event["payload"] for event in history if event["kind"] == "claim"}
        superseded = {revision for claim in claims for revision in claim["supersedes"]}
        pending = list(superseded)
        while pending:
            for ancestor in all_claims[pending.pop()]["supersedes"]:
                if ancestor not in superseded:
                    superseded.add(ancestor)
                    pending.append(ancestor)
        active = [claim for claim in claims if claim["revision_id"] not in superseded]
        allowed = [claim for claim in active if purpose in claim["permitted_uses"]]
        result = {"schema": "keel.loki.memory_resolution.v1", "status": "NOT_FOUND",
                  "value": None, "revision_ids": [], "usable_observation": False, **FLAGS}
        if not allowed:
            return result
        if len(active) != len(allowed):
            result["status"] = "RESTRICTED_CONFLICT"
            return result
        result["revision_ids"] = sorted(claim["revision_id"] for claim in allowed)
        if len(allowed) > 1:
            result["status"] = ("CONFLICT" if len({_hash(c["value"]) for c in allowed}) > 1
                                else "AMBIGUOUS_REVISIONS")
            return result
        claim = allowed[0]
        latest = {}
        for event in history:
            value = event["payload"]
            if event["kind"] == "observation" and value["revision_id"] == claim["revision_id"]:
                old = latest.get(value["reviewer_id"])
                # A late-arriving old observation does not override a newer
                # observation made by the same reviewer.
                if old is None or (value["observed_at"], event["sequence"]) > old[:2]:
                    latest[value["reviewer_id"]] = (value["observed_at"], event["sequence"], value["verdict"])
        verdicts = {item[2] for item in latest.values()}
        result["status"] = ("UNVERIFIED" if not verdicts else "DISPUTED" if len(verdicts) > 1
                            else "REJECTED" if "rejected" in verdicts else "VERIFIED_OBSERVATION")
        if result["status"] == "VERIFIED_OBSERVATION":
            result["value"] = _clone(claim["value"])
            result["usable_observation"] = True
        return result

    def resolve(self, subject_id, key, scope, purpose, valid_at, known_at=None):
        for value in (subject_id, key, scope):
            _id(value)
        _check(type(purpose) is str and purpose in USES, "invalid_permitted_use")
        _stamp(valid_at)
        with self._connection() as connection:
            history = self._known(self._history(connection), known_at)
        return self._resolve(history, subject_id, key, scope, purpose, valid_at)

    def invalidation_projection(self, valid_at, known_at=None):
        """Read-only dependency impacts; callers must implement their own gate."""
        _stamp(valid_at)
        with self._connection() as connection:
            history = self._known(self._history(connection), known_at)
        claims = {event["identity"]: event["payload"] for event in history if event["kind"] == "claim"}
        artifacts = []
        for event in history:
            if event["kind"] != "artifact":
                continue
            artifact = event["payload"]
            reasons = []
            for revision_id in artifact["dependencies"]:
                claim = claims[revision_id]
                resolution = self._resolve(history, claim["subject_id"], claim["key"],
                                           artifact["scope"], artifact["purpose"], valid_at)
                if resolution["revision_ids"] != [revision_id] or not resolution["usable_observation"]:
                    reasons.append({"revision_id": revision_id, "reason": resolution["status"],
                                    "current_revision_ids": resolution["revision_ids"]})
            artifacts.append({"artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"],
                              "status": "STALE" if reasons else "DEPENDENCIES_CURRENT",
                              "reasons": reasons, "artifact_content_verified": False})
        stale = {item["artifact_id"] for item in artifacts if item["status"] == "STALE"}
        approvals = []
        latest = {}
        for event in history:
            if event["kind"] == "approval_observation":
                observation = event["payload"]
                key = (observation["artifact_id"], observation["reviewer_id"])
                previous = latest.get(key)
                if previous is None or (observation["observed_at"], event["sequence"]) > (
                        previous["payload"]["observed_at"], previous["sequence"]):
                    latest[key] = event
        for event in history:
            if event["kind"] == "approval_observation":
                observation = event["payload"]
                current_decisions = {item["payload"]["decision"] for key, item in latest.items()
                                     if key[0] == observation["artifact_id"]}
                replaced = latest[(observation["artifact_id"], observation["reviewer_id"])]["sequence"] != event["sequence"]
                approvals.append({"approval_id": observation["approval_id"],
                                  "artifact_id": observation["artifact_id"],
                                  "status": "REAPPROVAL_REQUIRED" if observation["artifact_id"] in stale
                                  else "SUPERSEDED_OBSERVATION" if replaced
                                  else "DISPUTED_OBSERVATION" if len(current_decisions) > 1
                                  else "REJECTED_OBSERVATION" if observation["decision"] == "rejected"
                                  else "RECORDED_CURRENT", "execution_authorized": False})
        return {"schema": "keel.loki.memory_invalidation.v1", "artifacts": artifacts,
                "approval_observations": approvals,
                "stale_artifact_count": len(stale), "projection_only": True, **FLAGS}


def demo(home):
    """Synthetic late correction; no human approval observation is invented."""
    clock = [100]
    with TemporalMemory(home, clock=lambda: clock[0]) as memory:
        claim = {"revision_id": "synthetic-rev-1", "subject_id": "synthetic-person",
                 "key": "employment-start", "value": "2020-01",
                 "source": {"source_id": "synthetic-document", "sha256": "a" * 64,
                            "classification": "personal", "allowed_scopes": ["synthetic-role"],
                            "permitted_uses": ["application_fact"]},
                 "scope": "synthetic-role", "permitted_uses": ["application_fact"],
                 "valid_from": 1, "valid_until": None, "supersedes": []}
        memory.record_claim(claim, "demo-claim-1")
        # Explicitly synthetic evidence-verification fixture, never an approval.
        memory.record_observation({"observation_id": "synthetic-review-1",
                                   "revision_id": claim["revision_id"], "reviewer_id": "synthetic-reviewer",
                                   "verdict": "verified", "source_sha256": "a" * 64,
                                   "observed_at": 100}, "demo-observation-1")
        memory.register_artifact({"artifact_id": "synthetic-packet", "subject_id": "synthetic-person",
                                  "scope": "synthetic-role", "purpose": "application_fact", "sha256": "b" * 64,
                                  "dependencies": [claim["revision_id"]]}, "demo-artifact")
        before = memory.resolve("synthetic-person", "employment-start", "synthetic-role", "application_fact", 50)
        clock[0] = 200
        correction = _clone(claim)
        correction.update(revision_id="synthetic-rev-2", value="2020-02", supersedes=[claim["revision_id"]])
        correction["source"]["sha256"] = "c" * 64
        memory.record_claim(correction, "demo-claim-2")
        old_view = memory.resolve("synthetic-person", "employment-start", "synthetic-role", "application_fact", 50, known_at=100)
        current = memory.resolve("synthetic-person", "employment-start", "synthetic-role", "application_fact", 50)
        projection = memory.invalidation_projection(50)
        integrity = memory.verify()
    return {"schema": "keel.loki.memory_demo.v1", "synthetic": True,
            "historical_view_preserved": before == old_view,
            "corrected_revision_requires_verification": current["status"] == "UNVERIFIED",
            "projection": projection, "integrity": integrity,
            "actual_human_decisions": 0, **FLAGS}
