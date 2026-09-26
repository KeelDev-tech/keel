#!/usr/bin/env python3
"""Exact-attempt ledger reconciliation; fixture-tested candidate, host unqualified.

Every pass scans the complete ledger. A schema-2 checkpoint records a validated
prefix, never suppresses deferred work. Publication reloads all evidence and all
queue owners under the existing queue lock. Queue envelopes are preserved.

A queue transition and its pending publication event are one queue JSON write.
Delivery is at-least-once, outside the lock; consumers MUST deduplicate event_id.
A successful callback is acknowledged under the lock, retaining the receipt.
Any queue writer exception has UNKNOWN commit outcome and stops this run's
remaining delivery. Restart can recover the marker if replacement occurred.

Host qualification is still required: actual queue-lock cooperation, coherent
ledger/intent snapshots, atomic writer durability, preservation of markers by
other queue writers, supported status vocabulary, and the production sink.
No host modules are implemented or substituted here. Intents/leases/verification
fields are never mutated. Dry-run performs no writes and invokes no callback.
"""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPE = os.path.dirname(os.path.dirname(_HERE))
_OUTCOME_TRACKING = os.path.join(_PIPE, "engines", "outcome-tracking")
for _p in (_HERE, _OUTCOME_TRACKING):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import queue_io  # noqa: E402
import submit_intent  # noqa: E402
from ledger_append import LEDGER as _LEDGER_DEFAULT  # noqa: E402
from ledger_append import canon_ledger_status  # noqa: E402

_HOMES = ("standard", "needs_input", "strategic")
_QUEUE_DEFAULTS = {
    "standard": os.path.join(_PIPE, "queue", "standard-queue.json"),
    "needs_input": os.path.join(_PIPE, "queue", "needs_input-queue.json"),
    "strategic": os.path.join(_PIPE, "queue", "strategic-queue.json"),
}
_WATERMARK_DEFAULT = os.path.join(
    _PIPE, "hidden_files", "ledger_queue_reconciler_state.json")
WATERMARK_SCHEMA = 2
PUBLICATION_SCHEMA = 1
PUBLICATION_FIELD = "reconciliation_publication"
_TEST = {"ledger_path": None, "queue_paths": None, "watermark_path": None}
ACCEPTED_ATTEMPT_SOURCES = frozenset({"writer", "role_id"})
EVIDENCE_REASON = ("ledger outcome reconciliation; not receipt-confirmed; "
                   "not identity-clean")
# Conservative fixture contract. Additional host statuses require qualification.
_ELIGIBLE_STATUSES = frozenset({
    "READY", "PARKED", "PARKED-NEEDS-INPUT", "PARKED-PENDING-VERIFICATION",
    "IN-FLIGHT",
})
_WRITABLE_FIELDS = frozenset({
    "status", "status_reason", "status_updated", "attempt_id", "transport",
    "date_submitted", "date_submitted_provenance", "reconciliation_note",
    "reconciled_ts", "queue_notes", PUBLICATION_FIELD,
})


class ReconciliationError(ValueError):
    """Invalid or inconsistent evidence; callers return structured failures."""


class CommitOutcomeUnknown(RuntimeError):
    """A writer raised; replacement may already have occurred."""


def set_test_paths(ledger_path=None, queue_paths=None, watermark_path=None):
    """Redirect stores; intent and shared-lock paths belong to host modules."""
    if ledger_path is not None:
        _TEST["ledger_path"] = os.fspath(ledger_path)
    if queue_paths is not None:
        _TEST["queue_paths"] = dict(queue_paths)
    if watermark_path is not None:
        _TEST["watermark_path"] = os.fspath(watermark_path)


def reset_test_paths():
    for key in _TEST:
        _TEST[key] = None


def _ledger_path():
    return _TEST["ledger_path"] or _LEDGER_DEFAULT


def _queue_paths():
    paths = dict(_TEST["queue_paths"] or _QUEUE_DEFAULTS)
    if set(paths) != set(_HOMES):
        raise ReconciliationError("exactly the three queue paths are required")
    if len({os.path.realpath(os.fspath(p)) for p in paths.values()}) != 3:
        raise ReconciliationError("queue paths must name three distinct files")
    return paths


def _watermark_path():
    return _TEST["watermark_path"] or _WATERMARK_DEFAULT


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _nonempty_str(value):
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _status(value):
    return _nonempty_str(value).upper()


def _is_protected_status(status):
    return _status(status) not in _ELIGIBLE_STATUSES | {"SUBMITTED"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReconciliationError("duplicate JSON key %r" % key)
        result[key] = value
    return result


def _invalid_constant(value):
    raise ReconciliationError("non-finite JSON number %s" % value)


def _json_value(value, location):
    """Validate host-returned Python values too; do not sanitize/filter data."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReconciliationError("%s: non-finite number" % location)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json_value(item, "%s[%d]" % (location, i))
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for key, item in value.items():
            _json_value(item, "%s.%s" % (location, key))
        return
    raise ReconciliationError("%s: unsupported JSON value" % location)


def _read_json(path, label):
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                           parse_constant=_invalid_constant)
        _json_value(value, label)
        return value, hashlib.sha256(raw).hexdigest()
    except Exception as exc:
        raise ReconciliationError("%s unreadable: %s: %s" %
                                  (label, type(exc).__name__, exc)) from exc


def _validate_rows(rows, label, required_identity=False):
    if not isinstance(rows, list):
        raise ReconciliationError("%s collection must be a JSON array" % label)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReconciliationError("%s[%d] must be an object" % (label, index))
        for field in ("role_id", "attempt_id", "status", "state",
                      "attempt_id_source", "transport"):
            if field in row and row[field] is not None and not isinstance(row[field], str):
                raise ReconciliationError("%s[%d].%s must be a string or null" %
                                          (label, index, field))
        for field in ("role_id", "attempt_id"):
            value = row.get(field)
            if isinstance(value, str) and value != value.strip():
                raise ReconciliationError("%s[%d].%s must not contain surrounding whitespace" %
                                          (label, index, field))
        if required_identity and not _nonempty_str(row.get("attempt_id")):
            raise ReconciliationError("%s[%d] has no attempt_id" % (label, index))
        _json_value(row, "%s[%d]" % (label, index))
    return rows


def _read_ledger():
    root, digest = _read_json(_ledger_path(), "ledger")
    if isinstance(root, list):
        rows = root
    elif isinstance(root, dict) and "rows" in root:
        rows = root["rows"]
    else:
        raise ReconciliationError("ledger root must be an array or rows envelope")
    return _validate_rows(rows, "ledger"), digest


def _load_ledger_rows(path):
    """Compatibility helper: strict rows plus error; source positions preserved."""
    try:
        root, _ = _read_json(path, "ledger")
        if isinstance(root, list):
            rows = root
        elif isinstance(root, dict) and "rows" in root:
            rows = root["rows"]
        else:
            raise ReconciliationError("ledger root must be an array or rows envelope")
        return _validate_rows(rows, "ledger"), None
    except Exception as exc:
        return [], str(exc)


def _load_intents():
    """Validate the host's complete record list; reject duplicate attempt IDs."""
    try:
        records = copy.deepcopy(submit_intent.all_records())
        _validate_rows(records, "intent", required_identity=True)
        result = {}
        for rec in records:
            aid = _nonempty_str(rec["attempt_id"])
            if aid in result:
                raise ReconciliationError("duplicate intent attempt_id %r" % aid)
            result[aid] = rec
        return result
    except Exception as exc:
        raise ReconciliationError("intent store unreadable: %s: %s" %
                                  (type(exc).__name__, exc)) from exc


def _read_queue(path):
    root, digest = _read_json(path, "queue")
    if isinstance(root, list):
        entries = root
    elif isinstance(root, dict):
        keys = [key for key in ("entries", "items") if key in root]
        if len(keys) != 1:
            raise ReconciliationError("queue envelope must contain exactly one of entries/items")
        entries = root[keys[0]]
    else:
        raise ReconciliationError("queue root must be an array or entries/items envelope")
    _validate_rows(entries, "queue")
    for entry in entries:
        if "queue_notes" in entry and not isinstance(entry["queue_notes"], (str, list, type(None))):
            raise ReconciliationError("queue_notes must be a string, list, or null")
        if PUBLICATION_FIELD in entry:
            _validate_marker(entry[PUBLICATION_FIELD], entry)
    return {"root": root, "entries": entries, "sha256": digest, "path": path}


def _load_queue_file(path):
    return _read_queue(path)["entries"]


def _find_entries(role_id, queues_by_home):
    return [(home, entry) for home in _HOMES
            for entry in queues_by_home.get(home, [])
            if entry.get("role_id") == role_id]


def candidate_gate(row):
    if not isinstance(row, dict):
        return False, "invalid_ledger_row"
    if canon_ledger_status(row.get("status")) != "SUBMITTED":
        return False, "row_not_submitted"
    if not _nonempty_str(row.get("attempt_id")):
        return False, "no_attempt_id"
    if _nonempty_str(row.get("attempt_id_source")) not in ACCEPTED_ATTEMPT_SOURCES:
        return False, "untrusted_attempt_id_source"
    return True, ""


def plan(rows, intents_by_attempt, queues_by_home, start_index=0):
    """Pure decisions; full-snapshot ambiguity indices, optional work slice only."""
    _validate_rows(rows, "ledger")
    if type(start_index) is not int or not 0 <= start_index <= len(rows):
        raise ReconciliationError("invalid plan start_index")
    if not isinstance(intents_by_attempt, dict):
        raise ReconciliationError("intent index must be an object")
    _validate_rows(list(intents_by_attempt.values()), "intent", required_identity=True)
    for aid, rec in intents_by_attempt.items():
        if aid != _nonempty_str(rec.get("attempt_id")):
            raise ReconciliationError("intent index key differs from attempt_id")
    if not isinstance(queues_by_home, dict) or set(queues_by_home) != set(_HOMES):
        raise ReconciliationError("all three queue snapshots required")
    for home, entries in queues_by_home.items():
        _validate_rows(entries, "queue.%s" % home)
    counts = {}
    for row in rows:  # Every row, including noncandidates and earlier work.
        aid = _nonempty_str(row.get("attempt_id"))
        if aid:
            counts[aid] = counts.get(aid, 0) + 1
    decisions = []
    for index in range(start_index, len(rows)):
        row = rows[index]
        rid, aid = _nonempty_str(row.get("role_id")), _nonempty_str(row.get("attempt_id"))
        decision = {"kind": "report_only", "ledger_index": index,
                    "role_id": rid, "attempt_id": aid, "queue_home": None,
                    "reasons": [], "detail": "", "_row_sha256": _sha(row)}
        ok, reason = candidate_gate(row)
        rec = intents_by_attempt.get(aid)
        if not ok:
            decision["reasons"] = [reason]
        elif counts.get(aid, 0) != 1:
            decision["reasons"] = ["ambiguous_attempt_id_multiple_ledger_rows"]
            decision["detail"] = "%d full-ledger rows share attempt_id" % counts[aid]
        elif rec is None or rec.get("state") != "SUBMITTED":
            decision["reasons"] = ["no_terminal_submitted_intent"]
        elif not rid or rid != _nonempty_str(rec.get("role_id")):
            decision["reasons"] = ["role_mismatch_ledger_intent"]
        else:
            hits = _find_entries(rid, queues_by_home)
            if len(hits) != 1:
                decision["reasons"] = ["no_queue_entry" if not hits else "ambiguous_queue_ownership"]
            else:
                home, entry = hits[0]
                decision["queue_home"] = home
                current, status = _nonempty_str(entry.get("attempt_id")), _status(entry.get("status"))
                if current and current != aid:
                    decision["reasons"] = ["attempt_id_mismatch"]
                elif status == "SUBMITTED":
                    if current == aid:
                        decision.update(kind="no_op", reasons=["already_published"])
                    else:
                        decision["reasons"] = ["attempt_id_mismatch"]
                elif status == "IN-FLIGHT" and not current:
                    decision["reasons"] = ["active_attempt_id_missing"]
                elif _is_protected_status(status):
                    decision["reasons"] = ["outcome_state_protected"]
                elif PUBLICATION_FIELD in entry:
                    # Never replace a retained receipt or pending obligation.
                    decision["reasons"] = ["publication_marker_already_present"]
                else:
                    decision.update(kind="publish", transport=_nonempty_str(rec.get("transport")))
        decisions.append(decision)
    return decisions


def _validate_watermark(state):
    if not isinstance(state, dict):
        raise ReconciliationError("watermark must be an object")
    schema = state.get("schema")
    if type(schema) is not int or schema not in (1, WATERMARK_SCHEMA):
        raise ReconciliationError("watermark schema unsupported")
    count, index = state.get("ledger_row_count_seen"), state.get("last_processed_ledger_row")
    if type(count) is not int or count < 0 or type(index) is not int:
        raise ReconciliationError("watermark count/cursor must be bounded integers")
    if not -1 <= index < count or (count == 0 and index != -1):
        raise ReconciliationError("watermark cursor/count inconsistent")
    for key in ("published_total", "report_only_total", "no_op_total"):
        if key in state and (type(state[key]) is not int or state[key] < 0):
            raise ReconciliationError("watermark %s must be a nonnegative integer" % key)
    for key in ("last_run_id", "last_run_ts"):
        if key in state and not isinstance(state[key], str):
            raise ReconciliationError("watermark %s must be a string" % key)
    if schema == WATERMARK_SCHEMA:
        digest = state.get("ledger_prefix_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReconciliationError("watermark prefix hash invalid")
    return state


def read_watermark():
    """Absent -> None; malformed state raises and run() fails closed."""
    try:
        os.lstat(_watermark_path())
    except FileNotFoundError:
        return None
    state, _ = _read_json(_watermark_path(), "watermark")
    return _validate_watermark(state)


def _check_prefix(state, rows):
    if state is None:
        return
    count = state["ledger_row_count_seen"]
    if count > len(rows):
        raise ReconciliationError("ledger rewound below watermark row count")
    if state["schema"] == WATERMARK_SCHEMA and _sha(rows[:count]) != state["ledger_prefix_sha256"]:
        raise ReconciliationError("ledger prefix replaced or modified since checkpoint")


def write_watermark(state):
    """Atomic replace with file AND directory fsync; caller holds queue lock."""
    _validate_watermark(state)
    path = _watermark_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lqr-state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=1, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _snapshot():
    rows, ledger_digest = _read_ledger()
    intents = _load_intents()
    queues = {home: _read_queue(path) for home, path in _queue_paths().items()}
    state = read_watermark()
    _check_prefix(state, rows)
    return {"rows": rows, "intents": intents, "queues": queues,
            "watermark": state, "ledger_sha256": ledger_digest,
            "captured_ts": _utcnow_iso()}


def _queue_entries(snapshot):
    return {home: value["entries"] for home, value in snapshot["queues"].items()}


def _public_decision(decision):
    return {key: value for key, value in decision.items() if not key.startswith("_")}


def _event_id(role_id, attempt_id):
    return "lqr-" + _sha(["ledger_queue_published", role_id, attempt_id])


def _utc_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0 and "T" in value
    except (ValueError, AttributeError, OverflowError):
        return False


def _validate_marker(marker, entry):
    if not isinstance(marker, dict) or type(marker.get("schema")) is not int or marker["schema"] != PUBLICATION_SCHEMA:
        raise ReconciliationError("publication marker schema invalid")
    payload = marker.get("payload")
    if not isinstance(payload, dict) or marker.get("state") not in ("pending", "delivered"):
        raise ReconciliationError("publication marker payload/state invalid")
    rid, aid = _nonempty_str(payload.get("role_id")), _nonempty_str(payload.get("attempt_id"))
    if not rid or not aid or payload.get("type") != "ledger_queue_published":
        raise ReconciliationError("publication marker identity/type invalid")
    if payload["role_id"] != rid or payload["attempt_id"] != aid:
        raise ReconciliationError("publication marker identity must not contain surrounding whitespace")
    expected = _event_id(rid, aid)
    if marker.get("event_id") != expected or payload.get("event_id") != expected:
        raise ReconciliationError("publication marker event_id invalid")
    if _nonempty_str(entry.get("role_id")) != rid:
        raise ReconciliationError("publication marker role differs from queue entry")
    if payload.get("queue_home") not in _HOMES or not _utc_timestamp(payload.get("ts")) or payload.get("evidence") != EVIDENCE_REASON:
        raise ReconciliationError("publication marker payload evidence/time/home invalid")
    if payload.get("transport") is not None and not isinstance(payload.get("transport"), str):
        raise ReconciliationError("publication marker transport invalid")
    if type(payload.get("ledger_index")) is not int or payload["ledger_index"] < 0:
        raise ReconciliationError("publication marker ledger_index invalid")
    row_hash = payload.get("ledger_row_sha256")
    if not isinstance(row_hash, str) or re.fullmatch(r"[0-9a-f]{64}", row_hash) is None:
        raise ReconciliationError("publication marker ledger row hash invalid")
    if marker["state"] == "delivered" and not _utc_timestamp(marker.get("delivered_ts")):
        raise ReconciliationError("publication marker delivered_ts invalid")
    if marker["state"] == "pending" and "delivered_ts" in marker:
        raise ReconciliationError("pending marker cannot carry delivered receipt")
    _json_value(marker, "publication marker")
    return marker


def _submission_time(row, intent):
    """Only explicit event fields; never created_ts/updated_ts or our clock.

    Keep source text/precision, including a date-only value. This narrow field
    contract requires host confirmation before live use.
    """
    for source, record in (("ledger", row), ("intent", intent)):
        for field in ("date_submitted", "submitted_ts"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    date.fromisoformat(value)
                    precision = "date"
                else:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None or "T" not in value:
                        continue
                    precision = "source_timestamp"
                return value, {"source": source, "field": field, "precision": precision}
            except (ValueError, OverflowError):
                continue
    return None, None


def _append_queue_note(entry, line):
    notes = entry.get("queue_notes")
    if isinstance(notes, list):
        notes.append(line)
    elif isinstance(notes, str) and notes.strip():
        entry["queue_notes"] = notes.rstrip() + "\n" + line
    else:
        entry["queue_notes"] = line


def _write_queue(queue):
    try:
        queue_io.atomic_write_json(queue["path"], queue["root"])
    except Exception as exc:
        raise CommitOutcomeUnknown("commit_outcome_unknown: %s: %s" %
                                   (type(exc).__name__, exc)) from exc


def _commit_publish(decision):
    """Reload ALL stores inside shared queue lock; never trust planned transport."""
    with queue_io.queue_lock(owner="ledger_queue_reconciler:%s" % decision["role_id"]):
        fresh = _snapshot()
        index = decision["ledger_index"]
        if (type(index) is not int or not 0 <= index < len(fresh["rows"]) or
                _sha(fresh["rows"][index]) != decision.get("_row_sha256")):
            return "report_only", {**_public_decision(decision), "kind": "report_only",
                                    "reasons": ["ledger_row_changed_after_plan"]}
        current = plan(fresh["rows"], fresh["intents"], _queue_entries(fresh))[index]
        if current["kind"] != "publish":
            return current["kind"], _public_decision(current)
        home, rid, aid = current["queue_home"], current["role_id"], current["attempt_id"]
        entry = _find_entries(rid, _queue_entries(fresh))[0][1]
        before = copy.deepcopy(entry)
        now = _utcnow_iso()
        transport = _nonempty_str(fresh["intents"][aid].get("transport"))
        payload = {"event_id": _event_id(rid, aid), "type": "ledger_queue_published",
                   "role_id": rid, "attempt_id": aid, "queue_home": home,
                   "transport": transport or None, "ts": now, "evidence": EVIDENCE_REASON,
                   "ledger_index": index, "ledger_row_sha256": _sha(fresh["rows"][index])}
        entry.update(status="SUBMITTED", status_reason=EVIDENCE_REASON,
                     status_updated=now, attempt_id=aid,
                     reconciliation_note=EVIDENCE_REASON, reconciled_ts=now)
        if transport:
            entry["transport"] = transport
        # Existing nonempty date is preserved exactly, including original precision.
        if entry.get("date_submitted") in (None, ""):
            value, provenance = _submission_time(fresh["rows"][index], fresh["intents"][aid])
            if value is not None:
                entry["date_submitted"] = value
                entry["date_submitted_provenance"] = provenance
        _append_queue_note(entry, "[ledger-queue-reconciler] SUBMITTED from exact ledger/terminal intent "
                           "(attempt %s); %s" % (aid, EVIDENCE_REASON))
        entry[PUBLICATION_FIELD] = {"schema": PUBLICATION_SCHEMA,
                                    "event_id": payload["event_id"],
                                    "payload": payload, "state": "pending"}
        if {k: v for k, v in before.items() if k not in _WRITABLE_FIELDS} != {
                k: v for k, v in entry.items() if k not in _WRITABLE_FIELDS}:
            raise ReconciliationError("non-writable queue field changed")
        _write_queue(fresh["queues"][home])
        return "published", copy.deepcopy(payload)


def _sync_watermark(report):
    """Fresh validation and checkpoint replacement use the SAME queue lock."""
    with queue_io.queue_lock(owner="ledger_queue_reconciler:checkpoint"):
        fresh = _snapshot()
        rows = fresh["rows"]
        # A concurrent append can be observed here without having been planned.
        # Hash the whole observed prefix but do not claim new rows were handled.
        processed = max(min(len(rows), len(report["planned"])) - 1,
                        (fresh["watermark"] or {}).get("last_processed_ledger_row", -1))
        state = {"schema": WATERMARK_SCHEMA, "last_processed_ledger_row": processed,
                 "ledger_row_count_seen": len(rows), "ledger_prefix_sha256": _sha(rows),
                 "last_run_id": report["run_id"], "last_run_ts": report["ts"],
                 "published_total": len(report["published"]),
                 "report_only_total": len(report["report_only"]),
                 "no_op_total": len(report["no_ops"])}
        # Informational scan marker only: never used to skip evidence next pass.
        write_watermark(state)
        return state


def _pending_markers(snapshot):
    result = []
    for home, queue in snapshot["queues"].items():
        for index, entry in enumerate(queue["entries"]):
            marker = entry.get(PUBLICATION_FIELD)
            if marker is not None and marker["state"] == "pending":
                result.append({"queue_home": home, "queue_index": index,
                               "role_id": _nonempty_str(entry.get("role_id")),
                               "marker": copy.deepcopy(marker)})
    return result


def _owned_marker(snapshot, expected):
    payload = expected["marker"]["payload"]
    hits = _find_entries(payload["role_id"], _queue_entries(snapshot))
    if len(hits) != 1:
        raise ReconciliationError("pending event ambiguous/missing queue ownership")
    home, entry = hits[0]
    marker = entry.get(PUBLICATION_FIELD)
    if not isinstance(marker, dict) or marker.get("event_id") != expected["marker"]["event_id"]:
        raise ReconciliationError("pending event marker changed or disappeared")
    _validate_marker(marker, entry)
    if marker["payload"] != payload:
        raise ReconciliationError("pending event payload changed")
    if _nonempty_str(entry.get("attempt_id")) != payload["attempt_id"]:
        raise ReconciliationError("pending event attempt ownership changed")
    return home, entry, marker


def _deliver_pending(report, emit):
    # Discovery and pre-delivery check are locked; callback itself never is.
    with queue_io.queue_lock(owner="ledger_queue_reconciler:event-scan"):
        pending = _pending_markers(_snapshot())
    report["pending_events"] = [item["marker"]["event_id"] for item in pending]
    report["pending_events_complete"] = True
    report["pending_events_status"] = "delivery_snapshot"
    for item in pending:
        event_id = item["marker"]["event_id"]
        try:
            with queue_io.queue_lock(owner="ledger_queue_reconciler:event-validate"):
                _, _, marker = _owned_marker(_snapshot(), item)
                if marker["state"] == "delivered":
                    report["pending_events"].remove(event_id)
                    continue
            payload = copy.deepcopy(item["marker"]["payload"])
            report["events"].append(copy.deepcopy(payload))
            if emit is None:
                report["errors"].append({"kind": "event_sink_unbound", "event_id": event_id,
                                         "error": "pending publication retained: no emit callback"})
                continue
            report["delivery_attempts"].append(event_id)
            try:
                emit(copy.deepcopy(payload))
            except Exception as exc:
                report["errors"].append({"kind": "event_delivery_failed", "event_id": event_id,
                                         "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            with queue_io.queue_lock(owner="ledger_queue_reconciler:event-ack"):
                fresh = _snapshot()
                home, entry, marker = _owned_marker(fresh, item)
                if marker["state"] != "delivered":
                    marker["state"] = "delivered"
                    marker["delivered_ts"] = _utcnow_iso()
                    _write_queue(fresh["queues"][home])
            report["delivered_events"].append(event_id)
            report["pending_events"].remove(event_id)
        except Exception as exc:
            report["errors"].append({"kind": "commit_outcome_unknown" if isinstance(exc, CommitOutcomeUnknown)
                                     else "event_validation_or_ack_failed",
                                     "event_id": event_id,
                                     "error": "%s: %s" % (type(exc).__name__, exc)})
            if isinstance(exc, CommitOutcomeUnknown):
                report["aborted"] = True
                report["pending_events_complete"] = False
                report["pending_events_status"] = "unknown_after_commit_error"
                report["abort_reason"] = "event acknowledgement commit outcome unknown; delivery stopped"
                break


def _evidence_age(record, now):
    value = next((record.get(k) for k in ("date_submitted", "submitted_ts", "updated_ts", "created_ts")
                  if record.get(k)), None)
    result = {"evidence_timestamp": value, "evidence_age_seconds": None}
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and "T" in value:
                result["evidence_age_seconds"] = (now - parsed).total_seconds()
        except (ValueError, OverflowError):
            pass
    return result


def _inventories(snapshot, decisions):
    rows, intents, queues = snapshot["rows"], snapshot["intents"], _queue_entries(snapshot)
    now = datetime.now(timezone.utc)
    ledger_pairs = {(_nonempty_str(r.get("role_id")), _nonempty_str(r.get("attempt_id"))) for r in rows}
    ledger_only, queue_only, open_intents = [], [], []
    for index, row in enumerate(rows):
        rid, aid = _nonempty_str(row.get("role_id")), _nonempty_str(row.get("attempt_id"))
        if not _find_entries(rid, queues):
            ledger_only.append({"ledger_index": index, "role_id": rid, "attempt_id": aid,
                                "reasons": ["no_queue_role_owner"], **_evidence_age(row, now)})
    for home, entries in queues.items():
        for index, entry in enumerate(entries):
            rid, aid = _nonempty_str(entry.get("role_id")), _nonempty_str(entry.get("attempt_id"))
            if _status(entry.get("status")) == "SUBMITTED" and (not aid or (rid, aid) not in ledger_pairs):
                queue_only.append({"queue_home": home, "queue_index": index, "role_id": rid,
                                   "attempt_id": aid, "reasons": ["no_exact_ledger_role_attempt"],
                                   **_evidence_age(entry, now)})
    for aid, record in intents.items():
        if record.get("state") not in {"SUBMITTED", "FAILED", "CANCELLED", "CANCELED"}:
            open_intents.append({"role_id": _nonempty_str(record.get("role_id")), "attempt_id": aid,
                                 "state": record.get("state"), "reasons": ["nonterminal_or_unknown_intent"],
                                 **_evidence_age(record, now)})
    deferred = [_public_decision(d) for d in decisions if d["kind"] == "report_only"]
    reasons = {}
    for decision in deferred:
        for reason in decision["reasons"]:
            reasons.setdefault(reason, []).append(decision["ledger_index"])
    return {"set_relationship": "independent overlapping sets; counts are not additive",
            "definitions": {"ledger_only": "ledger rows with no queue owner of exact role",
                            "queue_only": "SUBMITTED queue entries without exact ledger role/attempt pair",
                            "open_intents": "intent records outside explicit terminal-state allowlist",
                            "deferred": "all ledger report-only decisions from planning snapshot"},
            "ledger_only": ledger_only, "queue_only": queue_only, "open_intents": open_intents,
            "deferred": deferred, "deferred_reason_sets": reasons}


def _metadata(snapshot):
    return {"captured_ts": snapshot["captured_ts"], "ledger_rows": len(snapshot["rows"]),
            "ledger_sha256": snapshot["ledger_sha256"], "ledger_rows_sha256": _sha(snapshot["rows"]),
            "intents_count": len(snapshot["intents"]), "intents_sha256": _sha(snapshot["intents"]),
            "queues": {h: {"rows": len(q["entries"]), "sha256": q["sha256"]}
                       for h, q in snapshot["queues"].items()},
            "scope": "planning snapshot; commit evidence is freshly revalidated under queue lock"}


def run(dry_run=True, rescan=False, emit=None):
    """One full scan. All failures are structured; callback delivery at-least-once."""
    report = {"run_id": "lqr-" + uuid.uuid4().hex, "ts": _utcnow_iso(),
              "dry_run": bool(dry_run), "rescan": bool(rescan), "scan_mode": "full",
              "planned": [], "published": [], "report_only": [], "no_ops": [],
              "errors": [], "events": [], "publication_events": [], "pending_events": [],
              "pending_events_complete": False, "pending_events_status": "not_read",
              "delivered_events": [], "delivery_attempts": [], "warnings": [],
              "aborted": False, "fatal": None, "watermark_from": None, "watermark_to": None,
              "delivery_semantics": "at-least-once; sink deduplicates stable event_id"}
    try:
        snapshot = _snapshot()
        state = snapshot["watermark"]
        report["watermark_from"] = (state or {}).get("last_processed_ledger_row")
        report["watermark_to"] = report["watermark_from"]
        if state and state["schema"] == 1:
            report["warnings"].append("legacy checkpoint has no prefix proof; validated full scan establishes schema-2 baseline")
        report["ledger_rows"] = len(snapshot["rows"])
        report["snapshot"] = _metadata(snapshot)
        decisions = plan(snapshot["rows"], snapshot["intents"], _queue_entries(snapshot))
        report["planned"] = [_public_decision(d) for d in decisions]
        report["inventories"] = _inventories(snapshot, decisions)
        report["pending_events"] = [item["marker"]["event_id"] for item in _pending_markers(snapshot)]
        report["pending_events_complete"] = True
        report["pending_events_status"] = "planning_snapshot"
        for decision in decisions:
            index = decision["ledger_index"]
            if decision["kind"] == "publish" and not dry_run:
                try:
                    kind, payload = _commit_publish(decision)
                except Exception as exc:
                    report["errors"].append({"kind": "commit_outcome_unknown" if isinstance(exc, CommitOutcomeUnknown)
                                             else "commit_revalidation_failed", "ledger_index": index,
                                             "role_id": decision["role_id"], "attempt_id": decision["attempt_id"],
                                             "error": "%s: %s" % (type(exc).__name__, exc)})
                    report["aborted"] = True
                    report["pending_events_complete"] = False
                    report["pending_events_status"] = "unknown_after_abort"
                    report["abort_reason"] = "publication failed; no event delivery this run"
                    break
                if kind == "published":
                    report["published"].append(copy.deepcopy(payload))
                    report["publication_events"].append(copy.deepcopy(payload))
                    report["pending_events"].append(payload["event_id"])
                elif kind == "no_op":
                    report["no_ops"].append(index)
                else:
                    report["report_only"].append(payload)
            elif decision["kind"] == "publish":
                report["published"].append({**_public_decision(decision), "would_publish": True})
            elif decision["kind"] == "no_op":
                report["no_ops"].append(index)
            else:
                report["report_only"].append(_public_decision(decision))
        if not dry_run and not report["aborted"]:
            try:
                state = _sync_watermark(report)
                report["watermark_to"] = state["last_processed_ledger_row"]
            except Exception as exc:
                report["errors"].append({"kind": "watermark_write_or_validation_failed",
                                         "error": "%s: %s" % (type(exc).__name__, exc)})
                report["aborted"] = True
                report["pending_events_complete"] = False
                report["pending_events_status"] = "unknown_after_abort"
                report["abort_reason"] = "checkpoint failed; no event delivery this run"
        if not dry_run and not report["aborted"]:
            _deliver_pending(report, emit)
    except Exception as exc:
        report["fatal"] = "%s: %s" % (type(exc).__name__, exc)
        report["pending_events_complete"] = False
        report["pending_events_status"] = "unknown_after_error"
        if report["published"] and not dry_run:
            report["aborted"] = True
    return report


def main(argv):
    parser = argparse.ArgumentParser(description="Exact-attempt ledger reconciliation; default dry-run")
    parser.add_argument("--live", action="store_true", help="perform queue publications")
    parser.add_argument("--rescan", action="store_true", help="compatibility flag; every run scans all rows")
    parser.add_argument("--report", default=None, help="write JSON report to PATH")
    args = parser.parse_args(argv)
    report_fd = None
    report = None
    if args.report:
        # Reserve a NEW file before any reconciliation. Existing files, hardlinks
        # and symlinks must never become a report target (including intent stores
        # whose path is owned by the host and unavailable to this module).
        try:
            protected = [_ledger_path(), _watermark_path(), *_queue_paths().values()]
            if os.path.realpath(args.report) in {os.path.realpath(p) for p in protected}:
                raise ReconciliationError("report path names a reconciliation store")
            report_fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                getattr(os, "O_NOFOLLOW", 0), 0o600)
        except Exception as exc:
            report = {"dry_run": not args.live, "published": [], "report_only": [],
                      "no_ops": [], "events": [], "aborted": False,
                      "fatal": "report destination rejected before reconciliation",
                      "errors": [{"kind": "report_destination_invalid",
                                  "error": "%s: %s" % (type(exc).__name__, exc)}]}
    if report is None:
        report = run(dry_run=not args.live, rescan=args.rescan)
    if report_fd is not None:
        try:
            with os.fdopen(report_fd, "w", encoding="utf-8") as stream:
                json.dump(report, stream, indent=1, allow_nan=False)
                stream.write("\n")
        except Exception as exc:
            report["errors"].append({"kind": "report_write_failed",
                                     "error": "%s: %s" % (type(exc).__name__, exc)})
    print(json.dumps(report, indent=1, allow_nan=False))
    print("%s: %d published, %d report-only, %d no-ops, %d errors%s" %
          ("LIVE" if args.live else "DRY RUN", len(report["published"]),
           len(report["report_only"]), len(report["no_ops"]), len(report["errors"]),
           " ABORTED" if report["aborted"] else ""), file=sys.stderr)
    return 2 if report["fatal"] or report["aborted"] or report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
