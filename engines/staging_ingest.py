#!/usr/bin/env python3
"""staging_ingest.py — persistent staging -> queue ingestion worker.

Contract (incident response 2026-09-15, dev-support deep sweep):
    read staging -> validate -> dedupe -> backup -> write -> telemetry
    -> archive-on-success

A discovery burst writes JSON lead files to hidden_files/discovery-staging/.
Nothing else auto-ingests them; this worker is the ONLY sanctioned path from
staging into the queues. Dry-run by default; --live applies.

Settlement rule: a staging file is only ingested when its mtime is older than
SETTLE_SECONDS (default 300, one pulse interval) — files belonging to an
active sweep (still being written) are left alone and reported as deferred.

One-lead-one-queue: dedupe by role_id AND normalized employer+title against
every *-queue.json and the application ledger (Shadeform lesson).

STAGING DIRECTORY SEPARATION (2026-09-15 cleanup-4, ARM 113 shape fallback):
ingest reads files matching *-leads.json PLUS any other *.json file whose
records are lead-shaped (is_lead_shaped(): a JSON list where at least half
the entries carry the lead schema core — the same required fields
validate_entry() demands; mirror of the >50% fail-fast boundary). Ramp-style
sweep batches (e.g. ramp1a-...-20260915.json — 34 legitimately ingested leads
at the 11:48Z batch, which do NOT match *-leads.json) are recovered through
this record-shape fallback instead of being silently dropped; they go
through the IDENTICAL validated path (fail-fast, validate_entry, dedupe,
backup, telemetry, archive). Non-lead artifacts (token files, title-only
hits, verified records missing role IDs) never hit ingest —
sweep_nonlead_artifacts() moves them to hidden_files/discovery-artifacts/
(never overwrite, never delete) so the staging dir holds only candidate lead
files. Worker tooling (.py probes, .md proposals) stays put.

Every ingestion: queue backup to data/queues/_backup-<date>-staged-ingest-<batch>/
(sha256-verified) BEFORE the write; atomic write (tmp + os.replace);
per-lead staged_ingested / staged_rejected telemetry; post-write role_id
spot-check — on any miss the queue is rolled back from the backup and an
error event is emitted. Staging files are archived (never deleted) only on
full success.

Per-file fail-fast (J-20260915-1227-meth-84): a staging file is rejected as
a WHOLE when more than MALFORMED_FILE_THRESHOLD of its entries fail
validation (missing required fields) — one `staged_rejected` telemetry event
per rejected file (entry count + reason + file name) instead of one event
per malformed entry, and the file is moved to hidden_files/discovery-staging/
_quarantine/ instead of being triaged entry-by-entry. Rejected files appear
in the audit under `file_rejections` with their rejection count, and their
entries count toward the audit's `rejected` total — audit and emitted events
always reconcile. (2026-09-15 10:53 incident: a second same-minute run
overwrote the batch audit while 1794 per-entry events stood, so the audit
recorded 0/0/[].) Batch ids now carry seconds so two runs in the same minute
cannot collide and overwrite each other's audit.

Pulse alert helper: check_stale_staging() returns settled-but-never-ingested
staging files (older than one pulse interval with zero staged_ingested
telemetry) — the Tier-1 pulse calls this to surface stuck batches.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from queue_intake import validate_entry  # noqa: E402
from log_event import log  # noqa: E402
import queue_io  # noqa: E402 — unified atomic queue lock, 2026-09-15 fix
try:
    from dedupe_index import get_index, stage_verdict  # noqa: E402
except Exception:  # index unavailable -> triage falls back to legacy checks
    get_index = None

    def stage_verdict(entry, index, *a):  # noqa: E402
        return "fresh", {}, [], ("", "")

try:
    # J-20260916-0022-feed-427: canonical emission dedupe lives on
    # dedupe_gate.filter_batch (never hand-rolled greps).
    from dedupe_gate import filter_batch as _canonical_filter_batch  # noqa: E402
except Exception:
    _canonical_filter_batch = None

try:
    # P2-4 (2026-09-16): staging-side title triage — the same pure
    # title/location filter already applied at build time by
    # clean_board_watch.build_entry and
    # greenhouse_json_enumerate.build_json_entry. Fail-soft: if the module
    # is unavailable the ingest path simply skips the tag.
    import title_triage as _title_triage  # noqa: E402
except Exception:
    _title_triage = None

try:
    # Territorial work-auth prefilter + canonical gate taxonomy.
    # Fail-soft: if the module is unavailable the ingest path skips the
    # screen (prescreen's work-auth gates still apply downstream).
    import work_auth_gates as _work_auth_gates  # noqa: E402
except Exception:
    _work_auth_gates = None

from keel_paths import HOME as PIPE, DATA, TELEMETRY  # noqa: E402 — repo root; never the private pipeline path
STAGED_DIR = os.path.join(PIPE, "hidden_files/discovery-staging")
# Directory separation: non-lead artifacts live here, never in staging.
ARTIFACTS_DIR = os.path.join(PIPE, "hidden_files/discovery-artifacts")
# Allowlist: ONLY lead files are ingestible. Discovery workers must name
# lead batches *-leads.json; everything else is an artifact, not a queue
# candidate (2026-09-15 10:53 incident: token files, title-only hits and
# verified records missing role IDs hit ingest as if they were leads).
LEAD_FILE_GLOB = "*-leads.json"
QUEUE_DIR = os.path.join(DATA, "queues")
STANDARD_QUEUE = os.path.join(QUEUE_DIR, "standard-queue.json")
LEDGER = os.path.join(DATA, "application-ledger.json")
# Operator-owned employer blocklist (setup.sh writes a starter file at this
# path). The real production blocklist is personal data and is never
# committed — populate it with your own never-apply employers, one per
# line, plain text. staging_ingest's blocklist signal is BLOCKED_NAMES
# below (the file itself is read by feeder_watchdog's workable check).
BLOCKLIST = os.path.join(DATA, "employer-blocklist.md")

SETTLE_SECONDS = 300          # one pulse interval
LOCK_TIMEOUT = 600            # stale lock older than this may be broken
# Fail-fast: a staging file is rejected whole when MORE than this share of
# its entries fail validation (missing required fields). Fixed by
# J-20260915-1227-meth-84; not a tunable policy knob.
MALFORMED_FILE_THRESHOLD = 0.5

# Flood gate (J-20260916-0022-feed-427): a batch whose triage reject share
# hits this level is junk — one `staging_flood_gate` event + quarantine,
# never per-entry `staged_rejected` noise. Calibrated on the 2026-09-15
# census incident (0/1794 ingested, 92.6% rejected, ~1661 noise events).
# Fixed constants, not policy knobs: they bound event spam, they do not
# change what counts as a lead or any discovery threshold.
FLOOD_GATE_MIN_BATCH = 100
FLOOD_GATE_REJECT_SHARE = 0.90

BLOCKED_NAMES = {"super.com"}  # employer-blocklist.md is authoritative


def _now_pdt():
    return datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M PDT")


def batch_id(when=None):
    """Unique batch id, second-granular so two runs in the same minute
    cannot share a name (2026-09-15 10:53: the second run overwrote the
    first run's audit file because both were named ...-1053)."""
    when = when or datetime.now(timezone.utc)
    return "staged-ingest-" + when.strftime("%Y-%m-%d-%H%M%S")


def malformed_share(entries):
    """Fraction of entries failing validation (missing required fields).

    Duplicate/blocklist outcomes do NOT count — fail-fast triggers only on
    structurally malformed entries, per J-20260915-1227-meth-84.
    """
    if not entries:
        return 0.0
    bad = sum(1 for e in entries if validate_entry(e)[0])
    return bad / len(entries)


def has_lead_core(e):
    """True when a record carries the lead schema core: none of its
    validate_entry errors are missing-required-field errors.

    This is the shape gate's single source of truth — it asks the same
    validator the ingest path uses, so "lead-shaped" always means "the same
    record schema the *-leads.json path accepts". Cross-field rules (fit
    bar, URL presence, LOW-FIT stamp) are the triage validator's job, not
    the shape gate's: a record can be lead-shaped yet still be rejected by
    the current validator.
    """
    if not isinstance(e, dict):
        return False
    errs, _ = validate_entry(e)
    return not any(x.startswith("missing required field:") for x in errs)


def core_share(entries):
    """Fraction of entries carrying the lead schema core."""
    if not entries:
        return 0.0
    return sum(1 for e in entries if has_lead_core(e)) / len(entries)


def is_lead_shaped(path):
    """Record-shape fallback (ARM 113; residual risk from ARM 99 audit).

    A staged file whose name does NOT match LEAD_FILE_GLOB is still
    lead-shaped — and therefore ingestible — when it is a JSON list in
    which at least half the entries carry the lead schema core (mirror of
    the >50% fail-fast boundary: a file this malformed would be quarantined
    whole even under the allowlist). Ramp-style sweep batches
    (e.g. ramp1a-...-20260915.json) land here. Anything else — token
    scratch, title-only hits, verified records without role_ids, non-list
    JSON, unparseable files — is not lead-shaped and keeps the
    sweep-to-artifacts behavior.
    """
    try:
        data = json.load(open(path))
    except Exception:
        return False
    if not isinstance(data, list) or not data:
        return False
    return core_share(data) >= 0.5


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _waived_set(staged_dir, waived):
    """Normalize waived paths to realpaths inside the staged dir.

    A waiver only ever applies to an existing file under staged_dir —
    missing paths and paths outside the dir are silently ignored.
    """
    base = os.path.realpath(staged_dir)
    out = set()
    for w in (waived or ()):
        rp = os.path.realpath(os.path.abspath(w))
        if os.path.exists(rp) and (rp == base or rp.startswith(base + os.sep)):
            out.add(rp)
    return out


def settled_files(staged_dir=STAGED_DIR, settle_seconds=SETTLE_SECONDS, waived=()):
    """Return settled ingestible staging files; skip active/probe/aux files.

    ALLOWLIST + RECORD-SHAPE FALLBACK (2026-09-15 cleanup-4, ARM 113):
    files matching LEAD_FILE_GLOB (*-leads.json) are ingestible by name;
    any other *.json file that is lead-shaped (is_lead_shaped()) is
    ingestible by record shape — ramp-style sweep batches that would
    otherwise be swept to discovery-artifacts/ un-ingested. Non-lead
    artifacts (token files, title-only hits, verified records missing role
    IDs) are ignored by ingest and swept to
    hidden_files/discovery-artifacts/ instead.

    Excludes: the _archived-* dirs, probe scripts, .md proposals, and any
    file touched within settle_seconds (active sweep still writing).

    WAIVED (2026-09-21 settle-bypass fix): paths in `waived` skip the age
    check — a producer passes its OWN just-written file (atomic write, so
    complete at spawn time) and waives the settle wait for exactly that
    file. Waived paths must exist inside the staged dir; the ingestibility
    filter (allowlist/shape) is NEVER waived, so a waived token file or
    missing path is still excluded.
    """
    now = time.time()
    out = []
    waived_set = _waived_set(staged_dir, waived)
    # glob is non-recursive: _archived-* subdirs are never matched
    for path in sorted(glob.glob(os.path.join(staged_dir, "*.json"))):
        name = os.path.basename(path)
        if not name.endswith("-leads.json") and not is_lead_shaped(path):
            continue
        if os.path.realpath(path) in waived_set:
            out.append(path)  # producer's own complete file: no settle wait
            continue
        age = now - os.path.getmtime(path)
        if age < settle_seconds:
            continue
        out.append(path)
    return out


def deferred_files(staged_dir=STAGED_DIR, settle_seconds=SETTLE_SECONDS, waived=()):
    """Staging files skipped this run because they are still being written.

    Allowlist + record-shape fallback apply here too — only ingestible
    files (by name or by shape) can defer ingest. Files in `waived` are
    reported as settled by settled_files(), so they are not deferred here
    either (keeps main()'s settled/deferred counts consistent).
    """
    now = time.time()
    out = []
    waived_set = _waived_set(staged_dir, waived)
    for path in sorted(glob.glob(os.path.join(staged_dir, "*.json"))):
        name = os.path.basename(path)
        if not name.endswith("-leads.json") and not is_lead_shaped(path):
            continue
        if os.path.realpath(path) in waived_set:
            continue
        age = now - os.path.getmtime(path)
        if age < settle_seconds:
            out.append((path, age))
    return out


def sweep_nonlead_artifacts(staged_dir=STAGED_DIR, artifacts_dir=ARTIFACTS_DIR):
    """Move non-lead *.json files out of staging into discovery-artifacts/.

    Covers the 2026-09-15 10:53 incident shapes: token files, title-only
    hits, verified records missing role IDs — JSON artifacts that are NOT
    lead batches (don't match *-leads.json AND are not lead-shaped per
    is_lead_shaped()). Never overwrites an existing artifact (suffixes with
    a timestamp instead), never deletes. Files that ARE lead-shaped stay in
    staging for ingest through the identical validated path (ARM 113
    record-shape fallback — ramp-style sweep batches). Worker tooling
    (.py, .md) is left alone — it isn't JSON.

    Returns list of moved file names. Safe to call on every run (dry or
    live): it touches no queue, ledger, or telemetry.
    """
    os.makedirs(artifacts_dir, exist_ok=True)
    moved = []
    for path in sorted(glob.glob(os.path.join(staged_dir, "*.json"))):
        name = os.path.basename(path)
        if name.endswith("-leads.json"):
            continue
        if is_lead_shaped(path):
            # ARM 113: lead-shaped records under a non-allowlist filename
            # are queue candidates, not artifacts — leave in staging.
            continue
        dst = os.path.join(artifacts_dir, name)
        if os.path.exists(dst):
            stem, ext = os.path.splitext(name)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            dst = os.path.join(artifacts_dir, f"{stem}-{stamp}{ext}")
        os.rename(path, dst)
        moved.append(os.path.basename(dst))
    return moved


def ingest_telemetry_for(staged_file):
    """True if any staged_ingested event names this staging file."""
    events = os.path.join(TELEMETRY, "events.jsonl")
    if not os.path.exists(events):
        return False
    name = os.path.basename(staged_file)
    with open(events) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if (e.get("event_type") == "staged_ingested"
                    and e.get("details", {}).get("staging_file") == name):
                return True
    return False


def check_stale_staging(staged_dir=STAGED_DIR, settle_seconds=SETTLE_SECONDS):
    """Pulse-alert helper: settled staging files with zero ingestion telemetry.

    Returns list of dicts {file, age_seconds}. A settled file with no
    staged_ingested event means a batch is stuck — the pulse should surface it.
    """
    out = []
    for path in settled_files(staged_dir, settle_seconds):
        if not ingest_telemetry_for(path):
            out.append({"file": path,
                        "age_seconds": time.time() - os.path.getmtime(path)})
    return out


def load_queue_keys(queue_dir=QUEUE_DIR):
    """(role_ids, normalized (employer, title) keys) across all queues."""
    ids, keys = set(), set()
    for qf in glob.glob(os.path.join(queue_dir, "*-queue.json")):
        try:
            entries = json.load(open(qf))
        except Exception:
            continue
        for e in entries:
            if e.get("role_id"):
                ids.add(e["role_id"])
            keys.add((norm(e.get("company") or e.get("employer")),
                      norm(e.get("title"))))
    return ids, keys


def load_ledger_keys(ledger_path=LEDGER):
    keys = set()
    if not os.path.exists(ledger_path):
        return keys
    entries = json.load(open(ledger_path))
    rows = entries if isinstance(entries, list) else entries.get("entries", [])
    for e in rows:
        keys.add((norm(e.get("company") or e.get("employer")),
                  norm(e.get("title"))))
    return keys


def blocklisted(company):
    c = norm(company)
    return any(b in c for b in BLOCKED_NAMES)


def _stage_dup_reason(evidence):
    kind = evidence.get("kind") or "duplicate"
    match = evidence.get("match") or "unknown"
    who = (evidence.get("ledger_role_id")
           or evidence.get("queue_role_id") or "").strip()
    return f"{kind}: {match} matches {who}".rstrip().rstrip(":")


# Duplicate-rejection delta (feed-21 / ARM71-2 candidate c, 2026-09-16): a
# re-staged lead rejected as "duplicate role_id in queue" may carry a
# legitimate delta (materials built since, URL recovered). The rejection
# event carries the field diff vs the queued entry so the delta is never
# silently dropped — updates themselves stay on the queue-write path (the
# reviewer decision), the event is evidence only.
_DELTA_FIELDS = ("status", "action_band", "fit_score", "company", "title",
                 "location", "application_url", "ats_url", "posting_url",
                 "job_url", "ats", "resume_lane", "materials")


def _entry_delta(queued, staged):
    """Field diff {field: {queued, staged}} over _DELTA_FIELDS. Pure."""
    delta = {}
    for f in _DELTA_FIELDS:
        qv = (queued or {}).get(f)
        sv = (staged or {}).get(f)
        if qv == sv:
            continue
        if qv in (None, "") and sv in (None, ""):
            continue
        delta[f] = {"queued": qv, "staged": sv}
    return delta


def _find_introduced_dupes(pre_queue, new_queue):
    """role_ids whose multiplicity GREW to 2+ across the merge. Pure.

    A brand-new role_id (0 -> 1) is a legitimate add, not a dupe; a
    historical dupe already in the queue (pre count >= post count) is not
    the merge's fault — only growth into duplication is a merge defect.
    Used as the post-merge invariant tripwire in run_ingest.
    """
    from collections import Counter
    pre = Counter(e.get("role_id") for e in pre_queue)
    post = Counter(e.get("role_id") for e in new_queue)
    return sorted(rid for rid, c in post.items()
                  if c >= 2 and c > pre.get(rid, 0))


def triage(rows, queue_ids, queue_keys, ledger_keys, index=None):
    """Validate + dedupe a batch. Returns (ingested, rejected[(role_id, reason)]).

    When `index` (dedupe_index.DedupeIndex) is passed, the index-backed
    staging dedupe runs FIRST — before validate_entry — so duplicate
    candidates are rejected with an explicit duplicate reason even when
    their fields would also fail validation. The check covers canonical
    posting URLs (both key forms, incl. /confirmation suffix variants),
    employer containment + title equality vs ledger SUBMITTED rows and
    all live queues, intra-batch URL / employer+title collisions, and the
    conservative description-similarity fallback. Holds/suspects are
    never rejected here (reviewer judgment stays in the loop).
    Without an index the legacy inline checks apply unchanged.
    """
    ingested, rejected, seen = [], [], set()
    batch_url_keys, batch_emp_titles = set(), set()
    for e in rows:
        rid = e.get("role_id")
        if index is not None:
            verdict, evidence, _uk, _ek = stage_verdict(
                e, index, batch_url_keys, batch_emp_titles)
            if verdict == "duplicate":
                rejected.append((rid, _stage_dup_reason(evidence)))
                continue
        errs, _ = validate_entry(e)
        reason = None
        if errs:
            reason = "validation: " + "; ".join(errs)
        elif rid in queue_ids:
            reason = "duplicate role_id in queue"
        elif rid in seen:
            reason = "duplicate role_id in batch"
        elif (norm(e.get("company")), norm(e.get("title"))) in queue_keys:
            reason = "duplicate employer+title in queue"
        elif (norm(e.get("company")), norm(e.get("title"))) in ledger_keys:
            reason = "duplicate employer+title in ledger"
        elif blocklisted(e.get("company")):
            reason = "employer blocklist"
        if reason:
            rejected.append((rid, reason))
        # Territorial work-auth prefilter: a screen on the normalized
        # location string at triage time, BEFORE any verify/packet cycles.
        # Parks (reject ledger + rejected list) when the location explicitly
        # names a foreign country and no banked own-words fact establishes
        # authorization for it. Ambiguous location, remote/remote-in-territory
        # roles, and US roles flow through UNCHANGED (never-infer).
        # Configured relocation willingness is never consulted — relocation
        # is not work authorization.
        elif _work_auth_parked(e, rejected):
            pass
        # P-2026-09-16-titlegate-1 (operator-approved 2026-09-16 ~12:55 PDT):
        # upstream pre-staging title gate. Replaces the P2-4 tag-and-ingest
        # (which accumulated 1,092 PARKED-TRIAGE-DEFERRED queue entries):
        # rejects are NOT ingested — they go to the reject ledger
        # (role_id, title, reason, stamp), visible/auditable/recoverable,
        # honoring "deferred is not dropped" without queue clutter or
        # verify-budget burn. The fit>=75 waiver extends to ALL defers.
        elif _title_triage is not None:
            keep, treason = _title_triage.pre_staging_title_gate(
                e.get("title", ""), e.get("location", ""),
                e.get("fit_score"))
            if not keep:
                _write_titlegate_reject(e, treason)
                rejected.append((rid, f"title-gate: {treason}"))
            else:
                ingested.append(e)
                seen.add(rid)
        else:
            ingested.append(e)
            seen.add(rid)
    return ingested, rejected


# Test override for the titlegate reject-ledger path.
# None = production default (hidden_files/titlegate-rejects.jsonl).
_TITLEGATE_REJECT_LEDGER_OVERRIDE = None


def _titlegate_ledger_path():
    """P-2026-09-16-titlegate-1: reject-ledger location. Overridable via
    si._TITLEGATE_REJECT_LEDGER_OVERRIDE for tests so unit tests never
    touch the production ledger."""
    if _TITLEGATE_REJECT_LEDGER_OVERRIDE:
        return _TITLEGATE_REJECT_LEDGER_OVERRIDE
    return os.path.join(PIPE, "hidden_files", "titlegate-rejects.jsonl")


def _write_titlegate_reject(entry, reason):
    """P-2026-09-16-titlegate-1: append a gate reject to the reject ledger.

    Append-only JSONL — the recoverability backing for "deferred is not
    dropped". Best-effort: a ledger write failure must never break the
    ingest flow (the reject is still recorded in the triage rejected list
    + audit).
    """
    try:
        import json as _json
        ledger = _titlegate_ledger_path()
        rec = {
            "ts": _now_pdt(),
            "role_id": entry.get("role_id"),
            "company": entry.get("company"),
            "title": entry.get("title"),
            "location": entry.get("location"),
            "fit_score": entry.get("fit_score"),
            "reason": reason,
        }
        with open(ledger, "a") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as ex:
        print(f"  titlegate: reject-ledger write failed: {ex}",
              file=sys.stderr)


# Territorial work-auth prefilter support. Mirrors the titlegate
# reject-ledger pattern above: parked leads are recoverable/auditable,
# never silently dropped, and cost zero verify/packet cycles (they never
# enter the queue).
_WORKAUTH_REJECT_LEDGER_OVERRIDE = None
_WORKAUTH_ANSWER_BANK_OVERRIDE = None
_workauth_answer_bank_cache = None


def _workauth_ledger_path():
    """Work-auth reject-ledger location. Overridable via
    si._WORKAUTH_REJECT_LEDGER_OVERRIDE for tests so unit tests never
    touch the operator's ledger."""
    if _WORKAUTH_REJECT_LEDGER_OVERRIDE:
        return _WORKAUTH_REJECT_LEDGER_OVERRIDE
    return os.path.join(PIPE, "hidden_files", "workauth-rejects.jsonl")


def _workauth_answer_bank():
    """Read-only load of the answer bank for the work-auth prefilter.

    Loaded once per process; never written by this module (screening ONLY).
    Tests inject a scratch bank via si._WORKAUTH_ANSWER_BANK_OVERRIDE.
    """
    global _workauth_answer_bank_cache
    if _WORKAUTH_ANSWER_BANK_OVERRIDE is not None:
        return _WORKAUTH_ANSWER_BANK_OVERRIDE
    if _workauth_answer_bank_cache is None:
        try:
            with open(os.path.join(PIPE, "answer_bank.json")) as f:
                _workauth_answer_bank_cache = json.load(f)
        except Exception:
            _workauth_answer_bank_cache = {}
    return _workauth_answer_bank_cache


def _write_workauth_reject(entry, note, gate, country, basis):
    """Append a work-auth prefilter park to the reject ledger.

    Append-only JSONL. The note carries the lowercase `needs_input` token
    (verify_retry's park-guard matches only the lowercase token), the
    canonical gate name, and the standing-fact citation. Best-effort: a
    ledger write failure must never break the ingest flow (the park is
    still recorded in the triage rejected list + audit).
    """
    try:
        import json as _json
        ledger = _workauth_ledger_path()
        rec = {
            "ts": _now_pdt(),
            "role_id": entry.get("role_id"),
            "company": entry.get("company"),
            "title": entry.get("title"),
            "location": entry.get("location"),
            "fit_score": entry.get("fit_score"),
            "gate": gate,            # canonical: work_auth_unverified
            "country": country,      # slug, e.g. "india"
            "standing_fact": basis,  # e.g. "india_work_auth=NO (operator's words ...)"
            "note": note,
        }
        with open(ledger, "a") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as ex:
        print(f"  workauth: reject-ledger write failed: {ex}",
              file=sys.stderr)


def _work_auth_parked(e, rejected):
    """Triage-time territorial work-auth park step.

    Returns True when the entry was parked (reject ledger written +
    rejected appended); False when it flows through unchanged. Fail-soft:
    module unavailable -> never parks. Uses the SANCTIONED triage park
    mechanism only (reject ledger + triage rejected list); no new queue
    writer is invented here.
    """
    if _work_auth_gates is None:
        return False
    keep, note, gate, country, basis = \
        _work_auth_gates.pre_staging_workauth_screen(
            e.get("location", ""), bank=_workauth_answer_bank())
    if keep:
        return False
    _write_workauth_reject(e, note, gate, country, basis)
    rejected.append((e.get("role_id"), "work-auth: " + note))
    return True


# Intake materials gate (sour-518, 2026-09-16): a staged lead must never
# enter READY/READY-FOR-BROWSER without truthful, on-disk materials. The
# 2026-09-16 A16ZNT incident wrote 6 leads READY with no materials dict and
# the apply loop demoted all 6/6 (24h durable-gate cooldown each). The gate
# runs pre-merge, in memory, over the triage-fresh entries.
INTAKE_GATE_STATUSES = ("READY", "READY-FOR-BROWSER")
INTAKE_HOLD_STATUS = "PARKED-AWAITING-MATERIALS"


def materials_intake_gate(entries, pipe):
    """Hold READY/READY-FOR-BROWSER leads without materials at intake.

    For each entry that would enter READY/READY-FOR-BROWSER:
      1. if materials_readiness passes -> retain the READY-family status;
      2. else try verify_retry.try_materials_onramp — the existing
         truthful lane-master on-ramp (fail-closed, in-memory only,
         never invents a lane or artifact) — and re-check readiness;
      3. else route to PARKED-AWAITING-MATERIALS with an explicit
         status_reason naming the missing/unbuildable materials, a
         status_updated stamp, and a queue note (accepted by
         queue_intake.validate_entry: the status is known and stamped).

    No transient READY is ever written: the hold happens before the queue
    merge. Returns the held [{role_id, reason, onramp}] list for the audit
    metric. Mutates entries in place (triage-style).

    Portability: this gate needs apply_loop.materials_readiness (and reads
    apply_loop.PIPE while it runs). When the host apply_loop does not
    provide them — e.g. an open-core build whose materials machinery lives
    elsewhere — the gate is skipped fail-open and run_ingest records the
    skip in the audit; the apply loop's own gates still guard READY leads
    downstream.
    """
    import apply_loop
    readiness = getattr(apply_loop, "materials_readiness", None)
    try:
        from verify_retry import try_materials_onramp
    except Exception as exc:
        try_materials_onramp = None
        _onramp_import_error = str(exc)
    else:
        _onramp_import_error = ""
    if readiness is None:
        # Gate unavailable in this build — skip fail-open, record nothing
        # here; run_ingest notes the skip via the returned sentinel.
        return None
    # The on-ramp's inner materials_readiness also reads
    # apply_loop.PIPE — swap once for the whole gate.
    prev_pipe = getattr(apply_loop, "PIPE", None)
    if prev_pipe is not None:
        apply_loop.PIPE = pipe
    held = []
    try:
        for e in entries:
            from_status = e.get("status")
            if from_status not in INTAKE_GATE_STATUSES:
                continue
            ok, reason = readiness(e)
            onramp = ""
            if not ok:
                if try_materials_onramp is None:
                    onramp = f"on-ramp unavailable: {_onramp_import_error}"
                else:
                    ok2, reason2 = try_materials_onramp(e)
                    onramp = f"on-ramp: {reason2}"
                    if ok2:
                        ok, reason = readiness(e)
                        if not ok:
                            # On-ramp claimed success but materials still
                            # fail the gate — fail closed, hold.
                            reason = ("on-ramp succeeded but materials "
                                      f"still not ready: {reason}")
            if ok:
                continue
            rid = e.get("role_id") or ""
            hold_reason = f"materials not ready: {reason}"
            e["status"] = INTAKE_HOLD_STATUS
            e["status_reason"] = hold_reason
            e["status_updated"] = _now_pdt()
            prev_notes = e.get("queue_notes") or ""
            e["queue_notes"] = (
                f"{prev_notes} ".strip() +
                f"[intake materials gate {_now_pdt()}: held from "
                f"{from_status} — {hold_reason}; {onramp}]").strip()
            held.append({"role_id": rid, "from_status": from_status,
                         "reason": hold_reason, "onramp": onramp})
    finally:
        if prev_pipe is not None:
            apply_loop.PIPE = prev_pipe
    return held


def flood_gate_check(rows, rejected):
    """Batch flood gate verdict. Pure function (no disk, no telemetry).

    Returns {"fired": bool, "batch_size": int, "rejected": int,
             "reject_share": float, "reason": str}.

    Fires when a large batch is overwhelmingly rejected — the 2026-09-15
    census incident shape (0/1794 ingested, 92.6% rejected). A fired gate
    means: quarantine the batch files, emit ONE `staging_flood_gate`
    event, and skip per-entry `staged_rejected` telemetry (the noise the
    incident produced). Nothing about discovery thresholds changes —
    this only bounds how a junk batch is *handled*.
    """
    n = len(rows)
    r = len(rejected)
    share = (r / n) if n else 0.0
    fired = n >= FLOOD_GATE_MIN_BATCH and share >= FLOOD_GATE_REJECT_SHARE
    reason = ""
    if fired:
        reason = (f"flood gate: {r}/{n} ({share:.1%}) rejected — batch "
                  f"quarantined, single staging_flood_gate event emitted")
    return {"fired": fired, "batch_size": n, "rejected": r,
            "reject_share": round(share, 4), "reason": reason}


def emission_dedupe(entries):
    """Canonical emission-side dedupe for census/discovery emitters.

    Runs dedupe_gate.filter_batch (the canonical gate — never hand-rolled
    greps) over the batch BEFORE it is written to the staging directory,
    so duplicates never occupy staging at all. Dupes carry their
    `dedupe_evidence`; the caller logs ONE summary line, not per-dupe
    events.

    Returns (fresh, dupes, report). Falls back to (entries, [], ...) when
    the canonical gate is unavailable — emission must never be blocked by
    the dedupe helper itself.
    """
    entries = [e for e in entries if isinstance(e, dict)]
    if _canonical_filter_batch is None:
        return entries, [], {"dedupe": "unavailable",
                             "fresh": len(entries), "dupes": 0}
    fresh, dupes = _canonical_filter_batch(entries)
    report = {"dedupe": "dedupe_gate.filter_batch",
              "fresh": len(fresh), "dupes": len(dupes),
              "dupe_kinds": {}}
    for d in dupes:
        kind = (d.get("dedupe_evidence") or {}).get("kind", "duplicate")
        report["dupe_kinds"][kind] = report["dupe_kinds"].get(kind, 0) + 1
    return fresh, dupes, report


def emission_precheck(entries):
    """Emission-side schema + in-batch dedupe gate (J-20260916-0425-sour-562).

    Runs BEFORE a staging file is written, so malformed stubs and
    intra-batch dupes never occupy staging (and never become
    `staged_rejected` noise or malformed-file quarantines at ingest time —
    measured 2412 staged_rejected/24h on 2026-09-16, ~95% avoidable:
    1698 schema-invalid + 587 in-batch employer+title dupes).

    Checks, in order:
      1. validate_entry: zero ERRORS (required fields role_id/company/
         title/action_band/status, known band/status values, cross-field
         rules). Warnings pass — this is validation moved earlier, not a
         policy change.
      2. in-batch role_id dedupe: first occurrence wins.
      3. in-batch (employer, title) dedupe: first occurrence wins.

    Fail-closed: withheld entries are DROPPED from the emitted batch with
    their reasons carried in the report — never rewritten, never silently
    fixed, and never assigned an invented role_id (minting identity
    collapsed 12 gigs postings into 8 shared role_ids on 2026-09-15; see
    clean_board_watch.make_watch_role_id). A generator emitting invalid
    stubs is a generator bug — the report names it so the worker gets
    fixed at the source.

    Pure function: no disk, no telemetry, no queue reads. The emitter prints
    the one-line summary and keeps the full report in its own yield/metrics
    log; ingest behavior is unchanged (defense in depth stays at ingest).

    Returns (fresh, withheld, report) where withheld is [(role_id, reason)].
    """
    fresh, withheld = [], []
    seen_ids, seen_emp_titles = set(), set()
    for e in entries:
        if not isinstance(e, dict):
            withheld.append((None, "precheck: entry is not a dict"))
            continue
        errs, _ = validate_entry(e)
        if errs:
            withheld.append(
                (e.get("role_id"), "precheck: validation: " + "; ".join(errs)))
            continue
        rid = e.get("role_id")
        key = (norm(e.get("company") or e.get("employer")), norm(e.get("title")))
        if rid in seen_ids:
            withheld.append((rid, "precheck: duplicate role_id in batch"))
            continue
        if key in seen_emp_titles:
            withheld.append(
                (rid, "precheck: duplicate employer+title in batch"))
            continue
        seen_ids.add(rid)
        seen_emp_titles.add(key)
        fresh.append(e)
    report = {"precheck": "staging_ingest.emission_precheck",
              "fresh": len(fresh), "withheld": len(withheld),
              "withheld_role_ids": [rid or "" for rid, _ in withheld],
              "withheld_reason_counts": {}}
    for _rid, reason in withheld:
        report["withheld_reason_counts"][reason] = (
            report["withheld_reason_counts"].get(reason, 0) + 1)
    return fresh, withheld, report


def acquire_lock(queue_dir=QUEUE_DIR, timeout=LOCK_TIMEOUT):
    """RETIRED (P1-1, 2026-09-16 complementarity audit).

    The O_EXCL `.ingest.lock` was a DIFFERENT file from queue_io's
    `queue.lock` — any caller on this path raced every queue_io writer with
    zero mutual exclusion (the 17:44 PDT clobber class). run_ingest uses the
    unified queue_io.queue_lock(); this trap now fails loudly so a future
    caller can never resurrect the race.
    """
    raise AssertionError(
        "staging_ingest.acquire_lock is retired: use queue_io.queue_lock()")


def release_lock(lock):
    """RETIRED — see acquire_lock."""
    raise AssertionError(
        "staging_ingest.release_lock is retired: use queue_io.queue_lock()")


def run_ingest(files, batch, dry_run=True, pipe=PIPE):
    """Full contract run. Returns audit dict. Raises on spot-check failure."""
    queue_path = os.path.join(pipe, "data", "queues", "standard-queue.json")
    queue_dir = os.path.join(pipe, "data", "queues")
    staged_dir = os.path.join(pipe, "hidden_files", "discovery-staging")

    # Per-file fail-fast (J-20260915-1227-meth-84): a file whose entries are
    # mostly malformed (> MALFORMED_FILE_THRESHOLD fail validation) is
    # rejected whole — one staged_rejected event per file, never one per
    # malformed entry (the 2026-09-15 10:53 incident emitted 1794 noise
    # events from a single scratch batch).
    file_rejections = []  # [{file, entries, malformed, reason}]
    ok_files = []
    rows = []
    for path in files:
        data = json.load(open(path))
        if not isinstance(data, list):
            raise ValueError(f"{path}: staging file must be a JSON list")
        name = os.path.basename(path)
        n_bad = sum(1 for e in data if validate_entry(e)[0])
        share = n_bad / len(data) if data else 0.0
        if data and share > MALFORMED_FILE_THRESHOLD:
            reason = (f"file rejected: {len(data)} entries, {n_bad} "
                      f"({share:.0%}) fail validation (missing required fields)")
            file_rejections.append({"file": name, "entries": len(data),
                                    "malformed": n_bad, "reason": reason})
            continue
        ok_files.append(path)
        for e in data:
            e = dict(e)
            e["_staging_file"] = name
            rows.append(e)
    # role_id -> staged row, for duplicate-rejection delta reporting
    # (feed-21: the delta vs the queued entry rides the staged_rejected
    # event so a legitimate re-stage delta is never silently dropped).
    staged_by_id = {e.get("role_id"): e for e in rows if e.get("role_id")}

    queue_ids, queue_keys = load_queue_keys(queue_dir)
    ledger_keys = load_ledger_keys(os.path.join(pipe, "data", "application-ledger.json"))
    # Persistent dedupe index: built once per ingest run, auto-refreshes
    # when any source file changed. Fail-soft: legacy inline checks apply.
    index = None
    if get_index is not None:
        try:
            index = get_index(pipe)
        except Exception:
            index = None
    ingested, rejected = triage(rows, queue_ids, queue_keys, ledger_keys,
                                index=index)
    # P2-4 (2026-09-16): title-triage deferral count — deferred leads are
    # ingested, not dropped; they carry PARKED-TRIAGE-DEFERRED and cost no
    # verify HTTP budget. Named metric for the title-triage build.
    triage_deferred = sum(
        1 for e in ingested
        if e.get("status") == "PARKED-TRIAGE-DEFERRED")

    # Intake materials gate (sour-518, 2026-09-16): no READY-family lead
    # enters the queue without truthful on-disk materials. Runs pre-merge
    # so no transient READY is ever written; held leads route to
    # PARKED-AWAITING-MATERIALS with the reason stamped.
    materials_held = materials_intake_gate(ingested, pipe)
    # None = the materials gate is unavailable in this build
    # (apply_loop.materials_readiness not present) — skipped fail-open.
    materials_gate_skipped = materials_held is None
    if materials_gate_skipped:
        materials_held = []

    # Flood gate (J-20260916-0022-feed-427): junk batches never become
    # per-entry telemetry noise. Evaluated on triage rejects only —
    # malformed fail-fast files already have their own per-file events.
    flood = flood_gate_check(rows, rejected)

    file_rejected_entries = sum(fr["entries"] for fr in file_rejections)
    audit = {
        "batch": batch,
        "ts": datetime.now(timezone.utc).isoformat(),
        "files": [os.path.basename(f) for f in files],
        "dry_run": dry_run,
        # ARM 113: files ingested via the record-shape fallback (not the
        # *-leads.json allowlist) are named explicitly so a future audit can
        # see exactly which non-allowlist files the fallback recovered.
        "shape_promoted": [os.path.basename(f) for f in files
                           if not os.path.basename(f).endswith("-leads.json")],
        # staged/ingested/rejected reconcile: staged == ingested + rejected
        # (+ merge_suppressed once the in-lock idempotence split runs —
        # see below), and every rejected file appears in file_rejections
        # with its count (never 0/0/[] while events stand). "ingested" is
        # the triage-fresh count here; the --live path overwrites it with
        # the actually-appended count and records triage_fresh separately.
        "staged": len(rows) + file_rejected_entries,
        "ingested": len(ingested),
        "triage_deferred": triage_deferred,
        # sour-518: leads held from READY at intake for missing/unbuildable
        # materials (would previously have entered READY and been demoted
        # by the apply loop with a 24h durable-gate cooldown).
        "materials_held_at_intake": len(materials_held),
        "materials_held_detail": materials_held,
        # True when the intake materials gate was skipped fail-open because
        # the host apply_loop does not provide materials_readiness.
        "materials_gate_skipped": materials_gate_skipped,
        "rejected": len(rejected) + file_rejected_entries,
        "file_rejections": file_rejections,
        "rejected_reasons": [{"role_id": r, "reason": x} for r, x in rejected],
        "flood_gate": flood,
    }

    if dry_run:
        audit["note"] = "dry-run: no queue write, no archive, no telemetry"
        return audit

    if flood["fired"]:
        # Junk batch: quarantine the triaged files, emit ONE event, skip
        # every per-entry staged_rejected (the 2026-09-15 incident emitted
        # ~1661 of them from one batch). Files are quarantined, never
        # deleted — a reviewer can still recover them.
        quarantine = os.path.join(staged_dir, "_quarantine")
        os.makedirs(quarantine, exist_ok=True)
        for path in ok_files:
            dst = os.path.join(quarantine, os.path.basename(path))
            if os.path.exists(dst):  # never clobber an older quarantine
                stem, ext = os.path.splitext(os.path.basename(path))
                dst = os.path.join(quarantine, f"{stem}-{batch}{ext}")
            if os.path.exists(path):
                os.rename(path, dst)
        log("staging_flood_gate", role_id="", source="staging-ingest",
            details={"batch": batch,
                     "batch_size": flood["batch_size"],
                     "rejected": flood["rejected"],
                     "reject_share": flood["reject_share"],
                     "reason": flood["reason"],
                     "quarantined_to": quarantine,
                     "files": [os.path.basename(f) for f in ok_files]})
        # Nothing was written to the queue — the whole batch (including any
        # triage-fresh entries) is quarantined, recoverable, as one unit.
        # Partial writes from a junk batch are worse than a clean quarantine.
        audit["ingested"] = 0
        # Flood-path reconciliation: every staged row was quarantined, so
        # staged == ingested + rejected holds with rejected == staged.
        # Triage detail is preserved separately for the post-mortem.
        audit["triage_rejected"] = len(rejected) + file_rejected_entries
        audit["triage_fresh_quarantined"] = len(ingested)
        audit["rejected"] = audit["staged"]
        audit["note"] = (f"flood gate fired: {len(ingested)} triage-fresh "
                         f"entries quarantined with the batch (recoverable "
                         f"from {quarantine}), one staging_flood_gate event, "
                         "no per-entry noise")
        audit["quarantined_to"] = quarantine
        return audit

    # 2026-09-15 (main agent): unified queue lock. The old .ingest.lock only
    # serialized against other ingests — verify_retry / apply_loop / lane
    # scripts use the flock-based queue_io.queue_lock(), so a concurrent
    # save from one of those could still clobber this merge (the 2026-09-15
    # 17:44 PDT verify_retry clobber was exactly this class of bug). One
    # lock for every queue writer now.
    _lk = queue_io.queue_lock()
    _lk.__enter__()
    try:
        # backup + sha256 verify
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_dir = os.path.join(queue_dir, f"_backup-{date}-staged-ingest")
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(queue_path, os.path.join(backup_dir, "standard-queue.json"))
        assert sha256(queue_path) == sha256(
            os.path.join(backup_dir, "standard-queue.json")), "backup hash mismatch"
        audit["backup_dir"] = backup_dir

        queue = json.load(open(queue_path))
        ready_before = sum(1 for e in queue if e.get("status") == "READY")

        for e in ingested:
            staging_file = e.pop("_staging_file", "")
            e["_ingest_batch"] = batch
            e["_ingest_staging_file"] = staging_file

        # Merge-idempotence (feed-21, 2026-09-16): triage deduped against a
        # PRE-LOCK snapshot, but the write lands inside the lock — a
        # non-atomic check-then-write. An overlapping writer may have merged
        # these role_ids since (the 95-rejection incident: a second run
        # re-globbed files after the merge; flipped the other way the same
        # race double-ingests). Re-dedupe against the FRESH queue so a
        # re-run of the same merge is a no-op, never a duplicate. The
        # check-then-write is now atomic inside the unified queue lock.
        fresh_ids = {e.get("role_id") for e in queue}
        fresh_by_id = {e.get("role_id"): e for e in queue
                       if e.get("role_id")}
        appended, merge_suppressed = [], []
        seen_ids = set(fresh_ids)
        for e in ingested:
            rid = e.get("role_id")
            if rid in seen_ids:
                qe = fresh_by_id.get(rid)  # None for intra-batch dupes
                merge_suppressed.append({
                    "role_id": rid,
                    "staging_file": e.get("_ingest_staging_file", ""),
                    "reason": ("duplicate role_id in queue "
                               "(merge-time idempotence: already present; "
                               "re-run is a no-op)"),
                    "delta_vs_queued": _entry_delta(qe, e) if qe else {},
                })
            else:
                appended.append(e)
                seen_ids.add(rid)
        new_queue = queue + appended

        # No-new-duplicates invariant: the merge must not grow any role_id's
        # multiplicity to 2+. Defense in depth behind the suppression above
        # — on violation roll back and fail loudly instead of writing dupes.
        introduced = _find_introduced_dupes(queue, new_queue)
        if introduced:
            shutil.copy2(os.path.join(backup_dir, "standard-queue.json"),
                         queue_path)
            log("error", source="staging-ingest",
                details={"batch": batch,
                         "reason": ("merge introduced duplicate role_ids; "
                                    "rolled back"),
                         "role_ids": introduced[:10]})
            raise RuntimeError(
                "merge-idempotence invariant FAILED, rolled back: "
                f"{introduced[:10]}")

        tmp = queue_path + ".tmp-ingest"
        json.dump(new_queue, open(tmp, "w"), indent=1)
        os.replace(tmp, queue_path)

        # post-write spot-check — never claim a merge without it
        q2ids = {e.get("role_id") for e in json.load(open(queue_path))}
        missing = [e.get("role_id") for e in appended
                   if e.get("role_id") not in q2ids]
        if missing:
            shutil.copy2(os.path.join(backup_dir, "standard-queue.json"), queue_path)
            log("error", source="staging-ingest",
                details={"batch": batch,
                         "reason": "spot-check failed; rolled back",
                         "missing": missing[:10]})
            raise RuntimeError(f"spot-check FAILED, rolled back: {missing}")
        audit["spot_check"] = f"{len(appended)}/{len(appended)} role_ids verified"
        # Merge-split accounting: staged == ingested(appended) +
        # merge_suppressed + rejected(+file_rejections, set pre-lock).
        audit["triage_fresh"] = len(ingested)
        audit["ingested"] = len(appended)
        audit["merge_suppressed"] = merge_suppressed
        if merge_suppressed:
            audit["note"] = (
                f"merge idempotence: {len(merge_suppressed)} re-staged "
                f"lead(s) already in queue — suppressed, not duplicated")

        audit["ready_before"] = ready_before
        audit["ready_after"] = sum(
            1 for e in json.load(open(queue_path)) if e.get("status") == "READY")

        # fail-fast files: ONE staged_rejected event per file (entry count +
        # reason + file name), then quarantine — never per-entry noise.
        quarantine = os.path.join(staged_dir, "_quarantine")
        for fr in file_rejections:
            log("staged_rejected", role_id="", source="staging-ingest",
                details={"batch": batch, "staging_file": fr["file"],
                         "entries": fr["entries"],
                         "malformed": fr["malformed"],
                         "reason": fr["reason"]})
        if file_rejections:
            os.makedirs(quarantine, exist_ok=True)
            for fr in file_rejections:
                src = os.path.join(staged_dir, fr["file"])
                dst = os.path.join(quarantine, fr["file"])
                if os.path.exists(dst):  # never clobber an older quarantine
                    stem, ext = os.path.splitext(fr["file"])
                    dst = os.path.join(quarantine, f"{stem}-{batch}{ext}")
                if os.path.exists(src):
                    os.rename(src, dst)
            audit["quarantined_to"] = quarantine

        # telemetry per lead
        for e in appended:
            log("staged_ingested", role_id=e.get("role_id"),
                company=e.get("company", ""), source="staging-ingest",
                details={"batch": batch,
                         "staging_file": e.get("_ingest_staging_file", ""),
                         "status": e.get("status"),
                         "action_band": e.get("action_band"),
                         "fit_score": e.get("fit_score")})
        # 2026-09-16 telemetry-noise (F-20260915-0247-telemetry-noise): ONE
        # aggregate staged_rejected event per batch instead of one event per
        # refused lead (measured 2412 of 7805 rows in the last 24h — 31% of
        # the telemetry log, with zero downstream consumers of this type).
        # Per-lead detail is preserved in the audit's rejected_reasons; the
        # event carries count + full role_ids + per-reason breakdown. A
        # single-reason batch keeps the legacy top-level "reason" field so
        # existing readers/tests don't break.
        # feed-21 (2026-09-16): merge-suppressed leads (triage-fresh but
        # already in the queue at merge time — the 95-rejection signature)
        # join the same aggregate with their idempotence reason, so the
        # signature survives as count + reason breakdown, not 95 events.
        combined_rejected = list(rejected)
        for s in merge_suppressed:
            combined_rejected.append((s["role_id"], s["reason"]))
        if combined_rejected:
            by_reason = {}
            for rid, reason in combined_rejected:
                by_reason.setdefault(reason, []).append(rid or "")
            det = {"batch": batch, "aggregate": True,
                   "count": len(combined_rejected),
                   "role_ids": [rid or "" for rid, _ in combined_rejected],
                   "reasons": {r: len(v) for r, v in by_reason.items()},
                   "reason_role_ids": by_reason}
            if len(by_reason) == 1:
                det["reason"] = next(iter(by_reason))
            # feed-21 / ARM71-2c: duplicate rejections carry the delta vs
            # the queued entry — a legitimate re-stage delta (materials
            # built since, URL recovered) is never silently dropped. The
            # event is evidence only; updates stay on the queue-write path.
            # The dedupe index never matches on role_id (its matches are
            # posting_url / employer_title / description), so a same-rid
            # queued entry is the right delta anchor for any duplicate
            # reason naming this role_id.
            deltas = {}
            stale = []
            for rid, reason in rejected:
                if "duplicate" not in reason:
                    continue
                qe = fresh_by_id.get(rid)
                if qe is not None:
                    d = _entry_delta(qe, staged_by_id.get(rid))
                    if d:
                        deltas[rid or ""] = d
                    continue
                # No queued entry by this role_id, yet rejected as its
                # duplicate: the legacy "duplicate role_id in queue" fires
                # only on the triage snapshot, so the entry vanished
                # mid-run (a concurrent writer removed it) or the snapshot
                # was stale. Left rejected (fail-closed: never resurrect
                # another writer's removal); flagged honestly for review.
                if reason == "duplicate role_id in queue":
                    stale.append(rid or "")
            for s in merge_suppressed:
                if s["delta_vs_queued"]:
                    deltas[s["role_id"] or ""] = s["delta_vs_queued"]
            if deltas:
                det["delta_vs_queued"] = deltas
            if stale:
                det["stale_snapshot_no_longer_in_queue"] = stale
            log("staged_rejected", role_id="", source="staging-ingest",
                details=det)

        # archive only on full success; fail-fast-rejected files were already
        # quarantined above, so only the triaged files move here
        arch = os.path.join(staged_dir, f"_archived-{date}-staged-ingest")
        os.makedirs(arch, exist_ok=True)
        for path in ok_files:
            if not os.path.exists(path):
                # Glob overlap (feed-21): another ingest run archived this
                # file first. The merge above was idempotent, so there is
                # nothing to redo — skip instead of crashing on rename.
                audit.setdefault("archive_skipped", []).append(
                    os.path.basename(path))
                continue
            os.rename(path, os.path.join(arch, os.path.basename(path)))
        audit["archived_to"] = arch
    finally:
        _lk.__exit__(None, None, None)
    return audit


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true",
                    help="apply the ingestion (default is dry-run)")
    ap.add_argument("--settle", type=int, default=SETTLE_SECONDS,
                    help="seconds a staging file must be untouched (default 300)")
    ap.add_argument("--only", nargs="+", default=None, metavar="FILE",
                    help="producer's own just-written file(s): the settle wait "
                         "is waived for exactly these paths (atomic write, so "
                         "complete already). Every other staging file still "
                         "honors --settle. 2026-09-21: replaces the blanket "
                         "--settle 0 which waived the window for the whole "
                         "staging dir.")
    ap.add_argument("--check-stale", action="store_true",
                    help="pulse-alert mode: list stale staged-but-unmerged files")
    args = ap.parse_args(argv)

    if args.check_stale:
        stale = check_stale_staging(settle_seconds=args.settle)
        print(json.dumps(stale, indent=1))
        return 0 if not stale else 2

    files = settled_files(settle_seconds=args.settle, waived=args.only)
    deferred = deferred_files(settle_seconds=args.settle, waived=args.only)
    # Directory separation runs on every pass (dry or live): non-lead
    # artifacts are moved out of staging before ingest is even considered.
    artifacts_moved = sweep_nonlead_artifacts()
    batch = batch_id()  # second-granular: two runs in one minute can't collide
    print(f"batch {batch}: {len(files)} settled file(s), "
          f"{len(deferred)} deferred (still being written), "
          f"{len(artifacts_moved)} non-lead artifact(s) swept")

    audit = run_ingest(files, batch, dry_run=not args.live)
    audit["artifacts_swept"] = artifacts_moved
    promoted = audit.get("shape_promoted", [])
    if promoted:
        print(f"shape fallback promoted {len(promoted)} file(s): "
              f"{', '.join(promoted)}")
    print(json.dumps(audit, indent=1))
    audit_path = os.path.join(PIPE, "hidden_files",
                              f"staged-ingest-audit-{batch}.json")
    if args.live:
        # never overwrite a prior run's audit (2026-09-15 10:53 incident)
        if os.path.exists(audit_path):
            i, stem = 2, audit_path[:-5]
            while os.path.exists(f"{stem}-{i}.json"):
                i += 1
            audit_path = f"{stem}-{i}.json"
        json.dump(audit, open(audit_path, "w"), indent=1)
        print("audit:", audit_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
