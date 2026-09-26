#!/usr/bin/env python3
"""rescreen_standard.py — canonical hard-gate re-screen for the standard queue.

Fills the tool/spec gap ARM 6 (keel octopus pulse #258) fail-closed on:
rescreen.py only operates on needs_input-queue.json and implements
stale-blocker resolution — not the posting-text/title hard-gate +
core-qualification re-screen the overnight READY refill needs over the
fit>=75 PARKED-PENDING-VERIFICATION leads in standard-queue.json.

Screen composition (all canonical, imported — NO invented gates):
  1. Title hard-gate: title_triage.triage_lead(title, location, fit_score).
     Title-family-miss / excluded-keyword / explicit D1 over-cap defer.
     The fit>=75 waiver inside triage_lead covers ONLY the location-signal
     deferral — title and over-cap gates still fire. Same function and
     verdict vocabulary the staging pipeline uses.
  2. Posting-text hard-gate: verify_retry.fetch_posting_text(url) +
     prescreen.posting_eligibility_screen(text). The exact pair
     verify_retry.screen_promotion_posting runs on the promote path
     (state exclusion, D1 office/travel, degree requirements).
     Fail-open: no URL / fetch trouble -> clean (prescreen rule: never
     park on absence).
  3. Form screen: verify_retry.screen_promotion_form(entry, url) =
     prescreen.screen_entry_prepromotion — the the applicant-approved 2026-09-15
     pre-promotion form check (essays, attestations, unmappable required
     questions, commitment gates). Fail-open on probe trouble.
The fit>=75 selection floor IS the core-qualification bar: fit scores are
evidence-scored by fit_push_score (domain-tenure evaluated there) and are
read here, never re-invented or re-scored.

Routing (mirrors verify_retry's apply section exactly):
  - title fail        -> PARKED-TRIAGE-DEFERRED via
                         title_triage.annotate_deferred; gate_blocked
                         gate="low_fit" (GATE_TYPES: fit rejections).
  - posting-text fail -> PARKED in the standard queue (verify_retry's
                         eligibility_park: recoverable, never needs_input,
                         never rejected). status_reason overwritten with
                         the exact disqualifying evidence quoted.
  - form fail         -> needs_input queue, PARKED-NEEDS-INPUT with
                         conventional unresolved blockers (verify_retry's
                         form_park, incl. the parking_schema contract).
  - clean             -> untouched PARKED-PENDING-VERIFICATION.
                         verify_retry owns promotion to READY. This engine
                         NEVER promotes to READY.

Telemetry: gate_blocked park events via log_event.log — the canonical
engine path (verify_retry's eligibility_park/form_park do exactly this).
NOTE on ingest_envelope.py: its schema accepts only submitted/blocked
*application attempts*; gate_blocked park events have no representation
there, and the P2 stale-park identity stamp requires the park event's own
ts, which only log_event.log returns. Routing park events through the
envelope would corrupt the park audit trail and reintroduce the
222-false-positive class. The batch summary goes out as scan_summary
(source="rescreen-standard") — no new event type needed.

Lock discipline (mirrors rescreen.py's 2026-09-16 redesign): Phase A
(selection + all HTTP screens) runs OUTSIDE the lock. Phase B takes ONE
short queue_lock, backs both queue files up to
queue/_backup-<ts>-rescreen-standard/ INSIDE the lock, reloads FRESH
queues, re-validates each planned mutation against the current entry
(status still PARKED-PENDING-VERIFICATION — fail closed, skip on
divergence), then atomic-writes. No network inside the lock, no nested
locks, max-2-writer discipline via the shared queue lock. Each entry is
mutated on a private copy and spliced in only after its whole block
succeeds; a per-entry failure is recorded in `errored` and the batch
continues (2026-09-17: fail-soft after the 03:01 PDT run died between
its 21st gate_blocked event and the atomic write).

429 hygiene: the canonical fetchers swallow transport errors to fail-open
"" per their contract, so this batch observes REAL HTTP 429s at the one
shared transport choke point (urllib.request.urlopen -- every board-API and
page fetch funnels through it, and it raises HTTPError on 429 rather than
returning a 429 response). A transparent recorder counts observed 429s and
aborts fail-closed (no writes) once --rate-limit-budget is reached (default
1: 429 = hard stop). Empty fetches are NOT treated as rate limiting -- a 200
page that yields no extractable text is an extraction miss or a dead
posting, and the old consecutive-empty breaker misfired on exactly that.
--pace spaces entries' HTTP work (default 1.0s).

Usage:
  python3 rescreen_standard.py [--min-fit 75] [--role-ids a,b | @file]
      [--limit N] [--pace 1.0] [--rate-limit-budget 1] [--live]
  Dry-run is the default: zero queue writes, zero telemetry writes.
  --live applies parks through the canonical paths above.
"""

import argparse
import contextlib
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
from keel_paths import DATA  # noqa: E402 — repo path convention
STD_Q = os.path.join(DATA, "queues", "standard-queue.json")
NI_Q = os.path.join(DATA, "queues", "needs_input-queue.json")

sys.path.insert(0, BASE)
import prescreen  # noqa: E402
import title_triage  # noqa: E402
import queue_io  # noqa: E402
import log_event  # noqa: E402
import parking_schema  # noqa: E402

TARGET_STATUS = "PARKED-PENDING-VERIFICATION"
SOURCE = "rescreen-standard"


def now_pdt():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M PDT")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_role_ids(spec):
    if not spec:
        return None
    if spec.startswith("@"):
        with open(spec[1:]) as f:
            ids = [l.strip() for l in f if l.strip()]
    else:
        ids = [r.strip() for r in spec.split(",") if r.strip()]
    return set(ids)


def select(std, min_fit, role_ids):
    """Entries eligible for the re-screen. Pure selection, no I/O."""
    out = []
    for e in std:
        if e.get("status") != TARGET_STATUS:
            continue
        if role_ids is not None and e.get("role_id") not in role_ids:
            continue
        fit = e.get("fit_score")
        if isinstance(fit, bool) or not isinstance(fit, (int, float)):
            continue
        if fit < min_fit:
            continue
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# 429 observation (single transport choke point)
#
# Every posting fetch in this batch -- Greenhouse/Lever/Ashby board APIs via
# http_cache, generic pages via verify_retry's direct calls -- funnels through
# safe_http.urlopen, which RAISES HTTPError on a 429 (it never returns a
# 429 response). The canonical fetchers swallow that into "" per their
# fail-open contract, so this batch wraps the one shared call site with a
# transparent recorder: it re-raises everything unchanged and only counts
# observed 429s. No fetch logic is duplicated; the signal is real, not a
# heuristic over empty fetches (an empty fetch on a 200-OK page is an
# extraction miss or a dead posting, NOT proof of rate limiting -- the old
# consecutive-empty breaker misfired on exactly that and was removed).
# ---------------------------------------------------------------------------

class _RateLimitObserved(Exception):
    """Internal: raised when the batch's 429 budget is exhausted."""


@contextlib.contextmanager
def observe_429s(counter, budget=1):
    """Patch the HTTP transports for the batch; count real HTTP 429s.

    counter[0] accumulates observed 429s. When it reaches `budget` the next
    fetch raises _RateLimitObserved instead of proceeding -- the batch then
    aborts fail-closed with zero writes (429 = hard stop). Restores the
    original urlopen on exit.

    Both `safe_http.urlopen` (the single policy-checked transport every
    bundled engine funnels through since the 2026-09-26 network-boundary
    routing) and `urllib.request.urlopen` (legacy direct callers) are
    wrapped, so the 429 budget holds regardless of which transport a fetch
    uses. Patching the module attributes intercepts all call sites because
    engines call via the module.
    """
    import safe_http
    real_safe = safe_http.urlopen
    real_urllib = urllib.request.urlopen

    def _recording(real):
        def _wrapped(*args, **kwargs):
            try:
                return real(*args, **kwargs)
            except urllib.error.HTTPError as he:
                if getattr(he, "code", None) == 429:
                    counter[0] += 1
                    if counter[0] >= budget:
                        raise _RateLimitObserved(
                            "rescreen-standard: HTTP 429 observed %dx during batch "
                            "-- aborting fail-closed (hard stop); no writes made."
                            % counter[0]) from he
                raise
        return _wrapped

    safe_http.urlopen = _recording(real_safe)
    urllib.request.urlopen = _recording(real_urllib)
    try:
        yield counter
    finally:
        safe_http.urlopen = real_safe
        urllib.request.urlopen = real_urllib


def screen_entry(entry, text=None, url=None):
    """Run the three canonical screens. Returns (decision, reasons).

    decision: "clean" | "title_park" | "eligibility_park" | "form_park".
    reasons: [quoted evidence strings]. Fail-open per canonical contracts:
    transport/probe trouble yields no reasons, never a park.
    verify_retry is imported lazily (heavy module).
    """
    import verify_retry as vr  # lazy: heavy imports

    title = entry.get("title") or ""
    location = entry.get("location") or ""
    fit = entry.get("fit_score")

    # 1. Title hard-gate (canonical: title_triage.triage_lead).
    decision, reason_code = title_triage.triage_lead(title, location, fit)
    if decision == "defer":
        return "title_park", [
            "title hard-gate defer (%s): title %r does not clear the "
            "canonical title gate" % (reason_code, title[:120])], reason_code

    # 2. Posting-text hard-gate (canonical: fetch + posting_eligibility_screen).
    # text/url are fetched ONCE by the caller (scan) and passed in, so each
    # role costs exactly one posting fetch per batch.
    if url is None:
        url = vr.posting_url(entry)
    if text is None:
        text = vr.fetch_posting_text(url) if url else ""
    if text:
        elig_reasons = prescreen.posting_eligibility_screen(text)
        if elig_reasons:
            return "eligibility_park", list(elig_reasons), None

    # 3. Form screen (canonical: pre-promotion form check).
    form_reasons = vr.screen_promotion_form(entry, url)
    if form_reasons:
        return "form_park", list(form_reasons), None

    return "clean", [], None


def scan(std, min_fit, role_ids, limit=None, pace=1.0,
         rate_limit_budget=1):
    """Phase A — selection + screens, OUTSIDE the queue lock.

    Returns (plan, stats). plan: {rid: {"entry", "decision", "reasons",
    "title_reason", "url"}}. Each role costs exactly one posting-text fetch.
    Empty fetches are counted as a stat (extraction miss / dead posting),
    never as parks. Raises _RateLimitObserved on a real HTTP 429 (hard stop).
    """
    import verify_retry as vr  # lazy: heavy imports

    selected = select(std, min_fit, role_ids)
    if limit:
        selected = selected[:limit]
    plan, stats = {}, {"selected": len(selected), "clean": 0,
                       "title_park": 0, "eligibility_park": 0,
                       "form_park": 0, "empty_fetch": 0, "observed_429s": 0}
    seen_429s = [0]
    with observe_429s(seen_429s, budget=rate_limit_budget):
        for i, entry in enumerate(selected):
            rid = entry.get("role_id")
            url = vr.posting_url(entry)
            # One posting fetch per role. Fail-open per the canonical
            # contract: transport trouble yields "", never a park.
            text = vr.fetch_posting_text(url) if url else ""
            if url and not text:
                stats["empty_fetch"] += 1
            decision, reasons, title_reason = screen_entry(
                entry, text=text, url=url)
            stats[decision if decision != "clean" else "clean"] += 1
            plan[rid] = {"entry": entry, "decision": decision,
                         "reasons": reasons, "title_reason": title_reason,
                         "url": url}
            if pace and i < len(selected) - 1:
                time.sleep(pace)
    stats["observed_429s"] = seen_429s[0]
    return plan, stats


def _park_event(rid, company, url, gate, screen, note):
    """Emit the canonical gate_blocked park event; return the event dict
    (carries the event's own ts for the P2 stale-park identity stamp)."""
    import ats
    return log_event.log(
        "gate_blocked", role_id=rid, company=company or "",
        ats=ats.detect_ats(url or ""), source=SOURCE,
        details={"gate": gate, "screen": screen, "reason": note})


def apply(plan, stats):
    """Phase B — ONE short queue lock: backup, reload fresh, re-validate,
    apply, atomic-write. No network inside the lock."""
    import verify_retry as vr  # lazy: _stamp_park, _validate_mutation, _qn_str

    stamp = now_pdt()
    ts = datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y%m%d-%H%M%S")
    # Backup dir is derived from the queue file's own directory (not PIPE)
    # so tests with relocated queues keep their backups in the scratch dir.
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(STD_Q)),
                              "_backup-%s-rescreen-standard" % ts)

    applied = {"title_park": [], "eligibility_park": [], "form_park": [],
               "skipped": [], "park_refused": [], "errored": []}
    with queue_io.queue_lock(owner="rescreen-standard:apply"):
        os.makedirs(backup_dir, exist_ok=True)
        for src, name in ((STD_Q, "standard-queue.json"),
                          (NI_Q, "needs_input-queue.json")):
            with open(src) as f:
                raw = f.read()
            with open(os.path.join(backup_dir, name), "w") as f:
                f.write(raw)
        with open(STD_Q) as f:
            fresh_std = json.load(f)
        with open(NI_Q) as f:
            fresh_ni = json.load(f)
        std_by_id = {e.get("role_id"): e for e in fresh_std}
        ni_by_id = {e.get("role_id"): e for e in fresh_ni}

        def _std_index(entry):
            # Identity-based: list.remove uses __eq__ and could splice a
            # same-content duplicate instead of the entry we screened.
            for i, e in enumerate(fresh_std):
                if e is entry:
                    return i
            return None

        for rid, mut in plan.items():
            cur = std_by_id.get(rid)
            if cur is None or cur.get("status") != TARGET_STATUS:
                # Concurrently moved/parked/promoted — fail closed, skip.
                applied["skipped"].append(rid)
                continue
            decision, reasons = mut["decision"], mut["reasons"]
            if decision == "clean":
                continue  # survivors stay untouched; verify_retry owns READY
            note = "; ".join(reasons)
            quoted = " | ".join('"%s"' % r[:200] for r in reasons)
            company = cur.get("company", "")
            url = mut["url"]
            # Mutate a private copy; splice it into the live lists only
            # after the whole per-entry block succeeds. A mid-entry
            # failure can never leave a half-mutated entry in the queue
            # (2026-09-17: the 03:01 PDT --live run logged 21 gate_blocked
            # events then died before the atomic write — fail-soft keeps
            # the rest of the batch instead of losing everything).
            new = copy.deepcopy(cur)
            try:
                if decision == "title_park":
                    park_ev = _park_event(rid, company, url, "low_fit",
                                          "title_triage", note)
                    # Canonical title-gate outcome: annotate_deferred stamps
                    # with the park event's own ts (P2 identity rule).
                    title_triage.annotate_deferred(
                        new, mut["title_reason"],
                        (park_ev or {}).get("ts") or now_iso())
                    new["queue_notes"] = (
                        (str(new.get("queue_notes") or "") + " | "
                         if new.get("queue_notes") else "")
                        + "[%s] %s: title hard-gate defer — never promoted "
                          "READY. Evidence: %s." % (stamp, SOURCE, quoted))
                    vr._validate_mutation(
                        vr._stamp_park(new, park_ev), "title triage park")
                    splice = ("std_replace", new)
                    applied["title_park"].append(rid)

                elif decision == "eligibility_park":
                    # Mirrors verify_retry eligibility_park exactly.
                    park_ev = _park_event(rid, company, url, "eligibility",
                                          "posting_text", note)
                    park_note = (
                        "%s %s: parked, posting eligibility block -- never "
                        "promoted READY. %s" % (SOURCE, stamp[:10], note))
                    new["status"] = "PARKED"
                    new["status_reason"] = park_note
                    new["queue_notes"] = (
                        vr._qn_str(new) + " | %s: %s" % (SOURCE, note)
                    ).strip(" |")
                    vr._validate_mutation(
                        vr._stamp_park(new, park_ev), "eligibility park")
                    splice = ("std_replace", new)
                    applied["eligibility_park"].append(rid)

                elif decision == "form_park":
                    # Mirrors verify_retry form_park exactly, incl. the
                    # parking_schema contract gate.
                    park_ev = _park_event(rid, company, url, "needs_input",
                                          "prepromotion", note)
                    park_note = (
                        "%s %s: pre-promotion form screen found input "
                        "blockers -- routed to needs_input without READY "
                        "promotion (no packet build burned). %s"
                        % (SOURCE, stamp[:10], note))
                    new["status"] = "PARKED-NEEDS-INPUT"
                    new["status_reason"] = park_note
                    new["unresolved"] = [r.strip() for r in reasons
                                         if r.strip()]
                    new["gate_note"] = (
                        "pre-promotion form screen %s: %d input blocker(s); "
                        "routed to tray" % (stamp[:10], len(reasons)))
                    new["queue_notes"] = (
                        vr._qn_str(new)
                        + " | %s: pre-promotion screen -> needs_input: %s"
                        % (SOURCE, note)).strip(" |")
                    try:
                        parking_schema.assert_park_entry(new)
                    except parking_schema.ParkingSchemaViolation as pvex:
                        try:
                            log_event.log(
                                "contract_violation", role_id=rid,
                                company=company, source=SOURCE,
                                details={"contract": "parking_schema",
                                         "version":
                                         parking_schema.PARKING_SCHEMA_VERSION,
                                         "violation": str(pvex),
                                         "refused_action": "form_park"})
                        except Exception as te:
                            print("  telemetry: contract_violation emission "
                                  "failed for %s: %s" % (rid, te),
                                  file=sys.stderr)
                        print("  parking-schema refused form_park for %s: %s "
                              "(lead left in place)" % (rid, pvex))
                        applied["park_refused"].append(rid)
                        continue  # `new` discarded; entry stays untouched
                    vr._validate_mutation(
                        vr._stamp_park(new, park_ev),
                        "pre-promotion form park")
                    splice = ("ni_move", new)
                    applied["form_park"].append(rid)

                else:
                    # Unknown decision — fail closed, never invent a routing.
                    applied["skipped"].append(rid)
                    continue
            except Exception as ex:  # noqa: BLE001 - fail-soft, batch continues
                print("  rescreen-standard: per-entry failure for %s (%s) -- "
                      "entry left untouched, batch continues" % (rid, ex),
                      file=sys.stderr)
                applied["errored"].append(rid)
                continue

            # Splice the fully-mutated copy into the live lists.
            idx = _std_index(cur)
            if idx is None:  # entry vanished mid-apply — fail closed
                applied["skipped"].append(rid)
                continue
            if splice[0] == "std_replace":
                fresh_std[idx] = splice[1]
                std_by_id[rid] = splice[1]
            else:  # ni_move
                del fresh_std[idx]
                fresh_ni.append(splice[1])
                std_by_id.pop(rid, None)
                ni_by_id[rid] = splice[1]

        queue_io.atomic_write_json(STD_Q, fresh_std)
        queue_io.atomic_write_json(NI_Q, fresh_ni)

    try:
        log_event.log(
            "scan_summary", source=SOURCE,
            details={"scanned": stats["selected"], "clean": stats["clean"],
                     "title_park": len(applied["title_park"]),
                     "eligibility_park": len(applied["eligibility_park"]),
                     "form_park": len(applied["form_park"]),
                     "skipped": len(applied["skipped"]),
                     "park_refused": len(applied["park_refused"]),
                     "errored": len(applied["errored"]),
                     "empty_fetch": stats["empty_fetch"],
                     "observed_429s": stats["observed_429s"],
                     "backup": backup_dir})
    except Exception as te:
        print("  telemetry: scan_summary emission failed: %s" % te,
              file=sys.stderr)
    applied["backup"] = backup_dir
    return applied


def main():
    ap = argparse.ArgumentParser(
        description="Canonical hard-gate re-screen over standard-queue "
                    "PARKED-PENDING-VERIFICATION leads. Dry-run by default.")
    ap.add_argument("--role-ids", default=None,
                    help="comma-separated role_ids or @file (default: all)")
    ap.add_argument("--min-fit", type=float, default=75,
                    help="fit_score floor for selection (default 75)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pace", type=float, default=1.0,
                    help="seconds between entries' HTTP work (default 1.0)")
    ap.add_argument("--rate-limit-budget", type=int, default=1,
                    help="abort fail-closed after this many OBSERVED HTTP 429s "
                         "(default 1: 429 = hard stop)")
    ap.add_argument("--live", action="store_true",
                    help="apply parks through canonical paths (default: dry-run)")
    args = ap.parse_args()

    role_ids = _load_role_ids(args.role_ids)
    with open(STD_Q) as f:
        std = json.load(f)

    try:
        plan, stats = scan(std, args.min_fit, role_ids, args.limit,
                           args.pace, args.rate_limit_budget)
    except _RateLimitObserved as rl:
        print("ABORTED: %s" % rl, file=sys.stderr)
        sys.exit(2)

    n_park = (stats["title_park"] + stats["eligibility_park"]
              + stats["form_park"])
    print("selected: %d (status=%s, fit>=%s)" % (
        stats["selected"], TARGET_STATUS, args.min_fit))
    print("clean (survivors, stay %s): %d" % (TARGET_STATUS, stats["clean"]))
    print("would title-park (PARKED-TRIAGE-DEFERRED): %d" % stats["title_park"])
    print("would eligibility-park (PARKED): %d" % stats["eligibility_park"])
    print("would form-park (needs_input): %d" % stats["form_park"])
    print("empty posting-text fetches (extraction miss/dead, not parks): %d"
          % stats["empty_fetch"])
    print("observed HTTP 429s: %d" % stats["observed_429s"])
    for rid, mut in plan.items():
        if mut["decision"] == "clean":
            continue
        print("  %s -> %s: %s" % (
            rid, mut["decision"], "; ".join(mut["reasons"])[:160]))

    if not args.live:
        print("\ndry-run: zero queue writes, zero telemetry writes. "
              "Re-run with --live to apply %d parks." % n_park)
        return

    applied = apply(plan, stats)
    print("\napplied: title=%d eligibility=%d form=%d skipped=%d refused=%d "
          "errored=%d" % (
        len(applied["title_park"]), len(applied["eligibility_park"]),
        len(applied["form_park"]), len(applied["skipped"]),
        len(applied["park_refused"]), len(applied["errored"])))
    print("backup: %s" % applied["backup"])


if __name__ == "__main__":
    main()
