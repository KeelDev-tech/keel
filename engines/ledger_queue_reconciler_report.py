#!/usr/bin/env python3
"""Report-only ledger/queue reconciliation runner.

Implements the vendor's durable transactional-outbox reconciler as a
REPORT-ONLY module in the live tree (Trent 2026-09-20 standing order: the
reconciler STAYS REPORT-ONLY while defects R1-R11 and journal recovery
remain unresolved).

Provenance
----------
- Vendored decision/outbox logic: ``ledger_queue_reconciler.py`` in this
  directory, byte-identical to the adjudicated vendor candidate
  (sha256 7ce8a9bd43b09a63ade4f395ba661a80734252d7d199e31de35c582579d44f23,
  855 lines; quarantine original untouched at
  ``~/workspace/recon-work/reconciler-repair-2026-09-21/quarantine/extracted/``).
  Adjudication: ``~/workspace/recon-work/adjudication-2026-09-21/ADJUDICATION.md``.
- Governing corrections (verified in the vendored source, enforced here):
  attempt evidence only from ``attempt_id_source in {writer, role_id}``;
  intent join requires ``ledger.attempt_id == intent.attempt_id`` AND
  ``intent.state == SUBMITTED`` plus exact ``role_id``; conflict blocking
  (R1: any nonempty differing queue ``attempt_id`` blocks publication
  regardless of queue status; ID-less active attempt blocked fail-closed);
  exactly-one queue owner (R2); the 43 historical divergences stay
  report-only (never backfilled from ``status``/``status_reasons``);
  unknown timestamps never invented; protected outcome states never
  regressed; no intents closed; no launches.

What this runner does
---------------------
- Forces ``dry_run=True``. There is no ``--live`` flag and no code path
  that can reach one.
- Structurally blocks every write-capable path of the vendored module
  (``_commit_publish``, ``_write_queue``, ``_deliver_pending``,
  ``_sync_watermark``, ``write_watermark``) and shadows the module's
  ``queue_io`` / ``submit_intent`` references with read-only guards that
  fail closed on any write attempt. Queues and the ledger are opened
  read-only (``rb``) by the vendored reader; the intent store is read via
  the host's read-only ``all_records()`` API (returns deep copies).
- Hashes every source store before and after the run and fails closed if
  anything changed (mtime + sha256).
- Computes the outbox publication markers a live writer WOULD persist
  (stable ``event_id``, ``evidence``, ``schema``) and attaches them to the
  report as ``computed_outbox_markers`` with
  ``persistence: "computed_not_persisted"``. The durability MODEL is
  implemented for the report; nothing is persisted to live queues.
- Reports ``pending_events_complete`` / ``pending_events_status``
  exactly as the candidate computes them: ``true``/``planning_snapshot``
  when the pending-marker scan completed (verified zero vs. uncertain),
  ``false``/``unknown_*`` on any error path.

Deliberately excluded (and why)
-------------------------------
- Vendor lane-retirement policy: ABSENT from the vendored source (zero
  occurrences of "retire"); adopting any such policy would deny API-direct
  dispatch and hold browser submissions. Excluded.
- Downstream timestamp-format migration (full-ISO UTC ``status_updated``):
  a host-qualification item per the adjudication, documented not adopted.
  This runner never writes a queue entry, so no live timestamp format is
  changed. Full-ISO UTC timestamps appear only inside the emitted report
  file, never in live stores.
- Any write authority: queue/ledger/intent writes, intent closes, launch
  actions, watermark writes, and event delivery are structurally
  impossible here (blocked callables raise before touching stores).
"""

import argparse
import copy
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Use the shipped read-only schema compatibility adapter, never import an
# arbitrary private writer merely to obtain a path and status normalizer.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import ledger_queue_reconciler as _lqr  # noqa: E402  (vendored, sha-pinned below)

VENDORED_SHA256 = ("7ce8a9bd43b09a63ade4f395ba661a80734252d7d199e31de35c582579d44f23")
REPORT_ONLY_VERSION = "0.1.0"

# Explicit private-host configuration retains its established layout. A
# normal installation reads the same workspace initialized by keel.py.
from keel_paths import HOME as _LOCAL_HOME, DATA as _LOCAL_DATA

_JOB_PIPE = os.environ.get("KEEL_JOB_PIPELINE_DIR")
if _JOB_PIPE is not None and (not _JOB_PIPE.strip() or not os.path.isabs(_JOB_PIPE)):
    raise ValueError("KEEL_JOB_PIPELINE_DIR must be a nonblank absolute path")
_QUEUE_DIR = (os.path.join(_JOB_PIPE, "queue") if _JOB_PIPE is not None
              else os.path.join(_LOCAL_DATA, "queues"))
LIVE_LEDGER_PATH = (os.path.join(_JOB_PIPE, "ledger", "application-ledger.json")
                    if _JOB_PIPE is not None else os.path.join(_LOCAL_DATA, "application-ledger.json"))
LIVE_QUEUE_PATHS = {
    "standard": os.path.join(_QUEUE_DIR, "standard-queue.json"),
    "needs_input": os.path.join(_QUEUE_DIR, "needs_input-queue.json"),
    "strategic": os.path.join(_QUEUE_DIR, "strategic-queue.json"),
}
LIVE_WATERMARK_PATH = os.path.join(
    _JOB_PIPE if _JOB_PIPE is not None else _LOCAL_HOME,
    "hidden_files", "ledger_queue_reconciler_state.json")

# Real host modules captured BEFORE any guard is installed.
_REAL_QUEUE_IO = _lqr.queue_io
_REAL_SUBMIT_INTENT = _lqr.submit_intent


class ReportOnlyViolation(RuntimeError):
    """A write was attempted, a guard was bypassed, or a source changed."""


def _pin_vendored_sha():
    path = os.path.abspath(_lqr.__file__)
    with open(path, "rb") as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    if digest != VENDORED_SHA256:
        raise ReportOnlyViolation(
            "vendored ledger_queue_reconciler.py drifted from the adjudicated "
            "artifact: got %s, expected %s" % (digest, VENDORED_SHA256))
    return path


def _blocked(name):
    def _raise(*args, **kwargs):
        raise ReportOnlyViolation(
            "report-only: %s is write authority and is blocked" % name)
    _raise._report_only_blocked = True  # noqa: SLF001
    _raise.__name__ = "blocked_" + name
    return _raise


class _ReadOnlyQueueIO:
    """Shadow for the vendored module's queue_io reference.

    The dry-run planning path never needs the queue lock or the atomic
    writer; both fail closed here so no write can slip through.
    """

    def queue_lock(self, *args, **kwargs):
        raise ReportOnlyViolation(
            "report-only: queue_lock blocked (no lock may be taken)")

    def atomic_write_json(self, *args, **kwargs):
        raise ReportOnlyViolation(
            "report-only: atomic_write_json blocked")


class _ReadOnlySubmitIntent:
    """Shadow for the vendored module's submit_intent reference.

    Only the read-only ``all_records()`` host API is exposed (it returns
    deep copies and takes no lock for writes). Every other attribute —
    including any writer — fails closed. ``records_provider`` is a TEST
    SEAM: when set, intent records come from the provider callable instead
    of the host store. Production callers never set it.
    """

    def __init__(self, real, records_provider=None):
        self._real = real
        self._records_provider = records_provider

    def all_records(self):
        if self._records_provider is not None:
            return copy.deepcopy(self._records_provider())
        return self._real.all_records()

    def __getattr__(self, name):
        raise ReportOnlyViolation(
            "report-only: submit_intent.%s is blocked" % name)


_WRITE_PATHS = ("_commit_publish", "_write_queue", "_deliver_pending",
                "_sync_watermark", "write_watermark")


def _install_report_only_guards(records_provider=None):
    """Replace every write-capable path with fail-closed raisers.

    Re-installing is idempotent. Raises ReportOnlyViolation if the vendored
    module no longer exposes the expected write surface (shape change =
    fail closed, never silently unguarded).
    """
    for name in _WRITE_PATHS:
        if not hasattr(_lqr, name):
            raise ReportOnlyViolation(
                "vendored module lost write path %r; refusing to run "
                "unguarded" % name)
        setattr(_lqr, name, _blocked(name))
    _lqr.queue_io = _ReadOnlyQueueIO()
    _lqr.submit_intent = _ReadOnlySubmitIntent(_REAL_SUBMIT_INTENT,
                                               records_provider)


def _guards_in_place():
    return all(
        getattr(getattr(_lqr, name), "_report_only_blocked", False)
        for name in _WRITE_PATHS
    ) and isinstance(_lqr.queue_io, _ReadOnlyQueueIO) \
        and isinstance(_lqr.submit_intent, _ReadOnlySubmitIntent)


def _fingerprint(path):
    if path is None:
        return {"path": None, "present": False, "note": "no store path"}
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
    except FileNotFoundError:
        return {"path": path, "present": False}
    st = os.stat(path)
    return {"path": path, "present": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "mtime_ns": st.st_mtime_ns}


def _snapshot_sources(ledger_path, queue_paths, watermark_path, intent_store_path):
    sources = {"ledger": _fingerprint(ledger_path)}
    for home, path in queue_paths.items():
        sources["queue:" + home] = _fingerprint(path)
    sources["watermark"] = _fingerprint(watermark_path)
    sources["intent_store"] = _fingerprint(intent_store_path)
    return sources


def _computed_outbox_markers(run_ts):
    """Recompute the outbox markers a live writer WOULD persist.

    Pure: re-reads the stores read-only, re-runs the vendored pure ``plan``,
    and builds each marker payload exactly as ``_commit_publish`` would —
    then validates it with the vendored ``_validate_marker``. Markers are
    attached to the report only; nothing is persisted.
    """
    snapshot = _lqr._snapshot()  # noqa: SLF001 (read-only by construction)
    entries = {home: value["entries"]
               for home, value in snapshot["queues"].items()}
    full = _lqr.plan(snapshot["rows"], snapshot["intents"], entries)
    if not _lqr._utc_timestamp(run_ts):  # noqa: SLF001
        raise ReportOnlyViolation("run ts is not full-ISO UTC: %r" % run_ts)
    markers = []
    for decision in full:
        if decision["kind"] != "publish":
            continue
        rid, aid = decision["role_id"], decision["attempt_id"]
        event_id = _lqr._event_id(rid, aid)  # noqa: SLF001 (stable, deterministic)
        transport = _lqr._nonempty_str(decision.get("transport")) or None  # noqa: SLF001
        payload = {"event_id": event_id, "type": "ledger_queue_published",
                   "role_id": rid, "attempt_id": aid,
                   "queue_home": decision["queue_home"], "transport": transport,
                   "ts": run_ts, "evidence": _lqr.EVIDENCE_REASON,
                   "ledger_index": decision["ledger_index"],
                   "ledger_row_sha256": decision["_row_sha256"]}
        marker = {"schema": _lqr.PUBLICATION_SCHEMA, "event_id": event_id,
                  "payload": payload, "state": "pending",
                  "persistence": "computed_not_persisted", "report_only": True}
        # The marker core must satisfy the vendored validation contract.
        core = {k: marker[k] for k in ("schema", "event_id", "payload", "state")}
        _lqr._validate_marker(core, {"role_id": rid})  # noqa: SLF001
        public = {k: v for k, v in decision.items() if not k.startswith("_")}
        markers.append({"marker": marker, "decision": public,
                        "note": "computed from the report-only plan; a live "
                                "writer would persist this marker in the same "
                                "atomic queue write as the status transition"})
    return markers


def run_report(*, ledger_path, queue_paths, watermark_path,
               intent_records_provider=None, report_path=None, rescan=False):
    """Run one report-only reconciliation pass.

    Never writes the ledger, queues, intent store, or watermark. Returns
    the report dict. Raises ReportOnlyViolation if any source store changed
    during the run or any guard is bypassed.
    """
    _pin_vendored_sha()
    _install_report_only_guards(intent_records_provider)
    if set(queue_paths) != {"standard", "needs_input", "strategic"}:
        raise ReportOnlyViolation("exactly the three queue paths are required")
    intent_store_path = getattr(_REAL_SUBMIT_INTENT, "STORE_PATH", None)
    _lqr.set_test_paths(ledger_path=ledger_path, queue_paths=dict(queue_paths),
                         watermark_path=watermark_path)
    try:
        if not _guards_in_place():
            raise ReportOnlyViolation("report-only guards failed to install")
        before = _snapshot_sources(ledger_path, queue_paths, watermark_path,
                                   intent_store_path)
        report = _lqr.run(dry_run=True, rescan=rescan)
        if report.get("dry_run") is not True:
            raise ReportOnlyViolation("vendored run did not stay in dry-run")
        after = _snapshot_sources(ledger_path, queue_paths, watermark_path,
                                  intent_store_path)
        changed = [k for k in before if before[k] != after[k]]
        if changed:
            raise ReportOnlyViolation(
                "report-only violation: source stores changed during the run: %s"
                % ", ".join(changed))
        markers = _computed_outbox_markers(report["ts"])
        published = report.get("published", [])
        if len(markers) != len(published):
            raise ReportOnlyViolation(
                "marker/decision count mismatch: %d markers vs %d would-publish"
                % (len(markers), len(published)))
        report["computed_outbox_markers"] = markers
        report["report_only_runner"] = {
            "version": REPORT_ONLY_VERSION,
            "vendored_sha256": VENDORED_SHA256,
            "mode": "report_only",
            "excluded": {
                "lane_retirement_policy": "absent from vendored source; would "
                    "deny API-direct dispatch / hold browser submissions",
                "timestamp_format_migration": "full-ISO UTC status_updated is a "
                    "host-qualification item; no live timestamp format changed",
                "write_authority": "queue/ledger/intent/watermark writes, "
                    "intent closes, launches, event delivery all blocked",
            },
            "enforcement": "write paths shadowed with fail-closed raisers; "
                "queue_io/submit_intent references shadowed read-only; "
                "source stores fingerprinted (sha256) before/after and "
                "verified unchanged",
            "source_fingerprints": before,
        }
        if report_path is not None:
            _write_report_new_file(report_path, report, ledger_path,
                                   watermark_path, queue_paths)
        return report
    finally:
        _lqr.reset_test_paths()


def _write_report_new_file(report_path, report, ledger_path, watermark_path,
                           queue_paths):
    """Write the report to a NEW file only; never to a reconciliation store."""
    protected = {os.path.realpath(p) for p in
                 [ledger_path, watermark_path, *queue_paths.values()]
                 if p is not None}
    if os.path.realpath(report_path) in protected:
        raise ReportOnlyViolation("report path names a reconciliation store")
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=1, allow_nan=False)
            stream.write("\n")
    except Exception:
        try:
            os.unlink(report_path)
        except OSError:
            pass
        raise


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Report-only ledger/queue reconciliation "
                    "(no --live flag exists by design)")
    parser.add_argument("--report", default=None,
                        help="write JSON report to a NEW file at PATH")
    parser.add_argument("--rescan", action="store_true",
                        help="compatibility flag; every run scans all rows")
    parser.add_argument("--ledger", default=LIVE_LEDGER_PATH)
    parser.add_argument("--queue-dir", default=_QUEUE_DIR)
    parser.add_argument("--watermark", default=LIVE_WATERMARK_PATH)
    return parser


def main(argv):
    parser = _build_parser()
    args = parser.parse_args(argv)
    queue_paths = {
        "standard": os.path.join(args.queue_dir, "standard-queue.json"),
        "needs_input": os.path.join(args.queue_dir, "needs_input-queue.json"),
        "strategic": os.path.join(args.queue_dir, "strategic-queue.json"),
    }
    try:
        report = run_report(ledger_path=args.ledger, queue_paths=queue_paths,
                            watermark_path=args.watermark,
                            report_path=args.report, rescan=args.rescan)
    except Exception as exc:  # fail closed, structured
        fatal = {"dry_run": True, "published": [], "report_only": [],
                 "no_ops": [], "events": [], "computed_outbox_markers": [],
                 "aborted": True,
                 "fatal": "%s: %s" % (type(exc).__name__, exc),
                 "errors": [{"kind": "report_only_runner_failed",
                             "error": "%s: %s" % (type(exc).__name__, exc)}]}
        print(json.dumps(fatal, indent=1, allow_nan=False))
        print("REPORT-ONLY RUNNER FAILED: %s" % fatal["fatal"], file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1, allow_nan=False))
    print("REPORT-ONLY: %d would-publish, %d report-only, %d no-ops, "
          "%d errors, %d computed outbox markers%s" %
          (len(report["published"]), len(report["report_only"]),
           len(report["no_ops"]), len(report["errors"]),
           len(report["computed_outbox_markers"]),
           " ABORTED" if report["aborted"] else ""), file=sys.stderr)
    return 2 if report["fatal"] or report["aborted"] or report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
