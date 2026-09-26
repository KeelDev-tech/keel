#!/usr/bin/env python3
"""Automatic source-yield demotion/promotion proposal generation (workstream 4).

Operationalizes the pulse source-yield arm methodology (ARM 245-E): ranks
discovery sources on net-new yield vs dup rate over a trailing window,
detects re-sweeps inside the source roster's cadence ceiling, and emits
blackboard-publish-ready PROPOSAL objects — never auto-applies anything.

Safety model (standing doctrine):
- Read-only on telemetry, the source roster, the ledger, and the blackboard.
  Zero queue writes, zero roster edits, zero ledger writes, zero cron edits.
  Published entries go through `blackboard.py publish`, which creates them
  with status `proposed`; the weekly charter-evaluator triage owns verdicts.
- Hard guard: never propose demoting a source that produced a submission in
  the trailing submission window. The recency check is evidence-backed
  (explicit source IDs on telemetry submission observations). Unattributed
  submissions hold demotions for reconciliation; a prefix is not attribution.

Wiring: the dev-support-deep-sweep Sweep arm runs this with `--check-open`
at the start of its source-yield judgment and publishes emitted proposals
as `proposed` instead of hand-detecting decay.
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict

_TASK_HOME = os.path.expanduser("~")
# Private-host data layout. The host may point at its own data tree via
# KEEL_JOB_PIPELINE_DIR; the default matches this host's layout and is not
# part of the public contract. Missing or malformed inputs produce HOLD,
# never an empty-success report or a proposal based on partial evidence.
_JOB_PIPE = os.environ.get("KEEL_JOB_PIPELINE_DIR",
                           os.path.join(_TASK_HOME, "workspace/job-pipeline"))
TELEMETRY = os.path.join(_JOB_PIPE, "telemetry/events.jsonl")
ROSTER = os.path.join(_JOB_PIPE, "discovery/source-roster.md")
# Repo-local tooling: resolved from this checkout, never from a home dir.
_KEEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _KEEL not in sys.path:
    sys.path.insert(0, _KEEL)
from engines.safe_io import loads as strict_loads, canonical, aware_time
BLACKBOARD = os.path.join(_KEEL, "monitors", "blackboard.py")

# Bars (2026-09-17; change only via human-approved proposal / Trent directive)
DUP_RATE_DECAY = 0.20          # dup rate at/above this on an intra-cadence re-sweep -> demote
DUP_RATE_HEALTHY = 0.10        # below this counts as healthy for promotion
PROMOTE_NET_NEW_MIN = 5        # minimum net-new in the window to earn a promotion proposal
PROMOTE_FIT_MEDIAN = 75        # fit bar for promotion (the pipeline's standing bar)
DECAY_SWEEP_DATES_MIN = 2      # >= this many distinct staging dates inside the
                               # window = an intra-window re-sweep pattern

# staging-file substring -> source family, in priority order. Mirrors the
# ARM 245-E hand mapping; this is the PRIMARY attribution signal because the
# staging file names the sweep that produced the batch.
STAGING_FILE_TO_FAMILY = [
    ("yc-mirrors", "yc-mirrors"),
    ("census3x", "census3x"),
    ("getro", "getro"),
    ("sweep26", "getro"),            # sweep26-* families are getro waves
    ("wave2a", "wave2a-ashby-lever"),
    ("ashby-lever", "wave2a-ashby-lever"),
    ("a16z", "a16z-new-terms"),
    ("builtin", "builtin"),
    ("sweep25", "builtin"),          # sweep25 = Built In national sweep
    ("greenhouse", "greenhouse-watch"),
    ("clean-board", "greenhouse-watch"),
]

# role_id prefix -> source family; FALLBACK signal when no staging file is
# present (mirrors ARM 245-E hand mapping; longest match wins)
PREFIX_TO_FAMILY = {
    "SWEEP26-GETRO": "getro",
    "CENSUS3X": "census3x",
    "C3X": "census3x",
    "CEN": "census3x",
    "A16ZNT": "a16z-new-terms",
    "SWEEP25": "builtin",
    "YCM": "yc-mirrors",
    "W2A": "wave2a-ashby-lever",
    "ATS8": "greenhouse-watch",
    "GH": "greenhouse-watch",
}

# roster "## heading" -> family key used above
HEADING_TO_FAMILY = {
    "YC mirrors (company mirrors + /jobs)": "yc-mirrors",
    "Getro network boards (VC portfolio job boards)": "getro",
    "Built In (national + regional editions)": "builtin",
    "jobs.a16z.com": "a16z-new-terms",
    "HN \"Who is hiring?\" (Algolia)": "hn-whoishiring",
    "TheMuse": "themuse",
    "Remotive (category pages only)": "remotive",
    "levels.fyi": "levelsfyi",
    "We Work Remotely": "weworkremotely",
    "RemoteOK": "remoteok",
}

CADENCE_DAYS = {
    "WEEKLY": 7,
    "BIWEEKLY": 14,
    "MONTHLY": 30,
    "QUARTERLY-RETRY": 90,
    "daily": 1,
}


def family_of(role_id):
    """Legacy display hint only. Never used to attribute measured yield."""
    rid = (role_id or "").upper()
    for prefix in sorted(PREFIX_TO_FAMILY, key=len, reverse=True):
        if rid.startswith(prefix + "-") or rid == prefix:
            return PREFIX_TO_FAMILY[prefix]
    return None


def family_of_event(e):
    """Only an explicit, nonconflicting source_id can own measured yield."""
    if not isinstance(e, dict) or not isinstance(e.get("details", {}), dict):
        return None
    values = [v for v in (e.get("source_id"), e.get("details", {}).get("source_id")) if v is not None]
    if not values or any(type(v) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", v)
                         for v in values) or len(set(values)) != 1:
        return None
    return values[0]


def parse_ts(s):
    try:
        return aware_time(s)
    except (ValueError, TypeError, OverflowError):
        return None


def _checked_events(events, now):
    if not isinstance(now, dt.datetime) or now.utcoffset() is None:
        raise ValueError("aware_now_required")
    seen, result = {}, []
    for e in events:
        if (type(e) is not dict or type(e.get("details", {})) is not dict
                or type(e.get("event_type")) is not str):
            raise ValueError("invalid_telemetry_event")
        stamp = parse_ts(e.get("ts"))
        if stamp is None or stamp > now:
            raise ValueError("invalid_or_future_event_timestamp")
        if e.get("synthetic_test") or e.get("details", {}).get("synthetic_test"):
            continue
        encoded = canonical(e)
        eid = e.get("event_id")
        if eid is not None and (type(eid) is not str or not eid.strip()):
            raise ValueError("invalid_event_id")
        key = ("id", eid) if eid is not None else ("content", encoded)
        if key in seen:
            if seen[key] != encoded:
                raise ValueError("conflicting_event_id")
            continue
        seen[key] = encoded
        result.append(e)
        if len(result) > 100000:
            raise ValueError("telemetry_event_limit")
    return result


def load_events(since, *, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(since, dt.datetime) or since.utcoffset() is None or since > now:
        raise ValueError("invalid_window_start")
    events, size = [], 0
    with open(TELEMETRY, "rb") as stream:
        while True:
            raw = stream.readline(256 * 1024 + 1)
            if not raw:
                break
            size += len(raw)
            if len(raw) > 256 * 1024 or size > 32 * 1024 * 1024:
                raise ValueError("telemetry_size_limit")
            if raw.strip():
                events.append(strict_loads(raw))
    return [e for e in _checked_events(events, now) if parse_ts(e["ts"]) >= since]


def parse_roster():
    """Read each level-two section independently; unknown cadence stays unknown."""
    roster = {}
    with open(ROSTER) as stream:
        text = stream.read(1024 * 1024 + 1)
    if len(text) > 1024 * 1024:
        raise ValueError("roster_size_limit")
    headings = list(re.finditer(r"^#{1,2} (.+)$", text, re.M))
    for index, m in enumerate(headings):
        if not m.group(0).startswith("## "):
            continue
        heading = m.group(1).strip()
        fam = HEADING_TO_FAMILY.get(heading)
        if not fam:
            continue
        if fam in roster:
            raise ValueError("duplicate_roster_source")
        chunk = text[m.end(): headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        cad = re.search(r"^-\s*Cadence:\s*(.+?)\s*$", chunk, re.M)
        last = re.search(r"^-\s*Last swept:\s*(\d{4}-\d{2}-\d{2})", chunk, re.M)
        cadence_label = cad.group(1).strip() if cad else ""
        ceiling = None
        for label, days in sorted(CADENCE_DAYS.items(), key=lambda item: -len(item[0])):
            if re.search(r"(?<![A-Za-z])" + re.escape(label) + r"(?![A-Za-z])", cadence_label, re.I):
                ceiling = days
                break
        roster[fam] = {
            "heading": heading,
            "cadence_label": cadence_label,
            "ceiling_days": ceiling,
            "last_swept": last.group(1) if last else "",
        }
    return roster


def batch_date(e):
    """Date of the staging batch (evidence), falling back to event date."""
    d = (e.get("details") or {}).get("batch", "")
    if type(d) is not str:
        raise ValueError("invalid_batch")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", d)
    if m:
        day = dt.date.fromisoformat(m.group(1))
        stamp = parse_ts(e.get("ts"))
        if stamp is None or day > stamp.date():
            raise ValueError("future_batch_date")
        return day.isoformat()
    ts = parse_ts(e.get("ts", ""))
    return ts.strftime("%Y-%m-%d") if ts else ""


def _candidate_rows(event):
    details = event.get("details", {})
    if details.get("aggregate") is not True:
        if type(event.get("role_id")) is not str or not event["role_id"].strip():
            raise ValueError("candidate_identity_missing")
        return [event]
    reasons = details.get("reason_role_ids")
    if (event["event_type"] != "staged_rejected" or type(reasons) is not dict
            or not reasons or type(details.get("count")) is not int):
        raise ValueError("aggregate_rejection_invalid")
    rows, ids = [], []
    for reason, members in sorted(reasons.items()):
        if type(reason) is not str or type(members) is not list:
            raise ValueError("aggregate_rejection_invalid")
        for rid in members:
            if type(rid) is not str or not rid.strip():
                raise ValueError("candidate_identity_missing")
            ids.append(rid)
            rows.append({**event, "role_id": rid,
                         "details": {**details, "reason": reason}})
    if (len(ids) != details["count"] or len(ids) != len(set(ids))
            or sorted(ids) != sorted(details.get("role_ids", []))
            or details.get("reasons") != {reason: len(members) for reason, members in reasons.items()}):
        raise ValueError("aggregate_rejection_count_mismatch")
    return rows


def rank_sources(events, *, now=None):
    """Unique attributed candidate checks, with explicit duplicate reasons only."""
    stats = defaultdict(lambda: {"ingested": 0, "dup_rejected": 0,
        "other_rejected": 0, "sweep_dates": set(), "fits": {}, "new_ids": set(), "unscored": set()})
    seen = {}
    for event in _checked_events(events, now or dt.datetime.now(dt.timezone.utc)):
        et = event["event_type"]
        if et not in ("staged_ingested", "staged_rejected"):
            continue
        fam = family_of_event(event)
        if not fam:
            continue
        for e in _candidate_rows(event):
            details = e.get("details", {})
            batch = details.get("batch") or e["ts"]
            if type(batch) is not str:
                raise ValueError("invalid_batch")
            key = (fam, e["role_id"], batch)
            reason = details.get("reason", "")
            if type(reason) is not str:
                raise ValueError("invalid_rejection_reason")
            duplicate = (details.get("reason_code") == "DUPLICATE" or
                         bool(re.match(r"^(?:precheck: )?duplicate(?:[: ]|$)", reason, re.I)))
            semantic = (et, duplicate, details.get("fit_score"))
            if key in seen:
                if seen[key] != semantic:
                    raise ValueError("conflicting_candidate_observation")
                continue
            seen[key] = semantic
            st = stats[fam]
            st["sweep_dates"].add(batch_date(e))
            if et == "staged_ingested":
                st["ingested"] += 1
                st["new_ids"].add(e["role_id"])
                fit = details.get("fit_score")
                if type(fit) in (int, float) and math.isfinite(fit) and 0 <= fit <= 100:
                    old = st["fits"].get(e["role_id"])
                    measured = (parse_ts(e["ts"]), fit)
                    if old is None or measured[0] > old[0]:
                        st["fits"][e["role_id"]] = measured
                    elif measured[0] == old[0] and fit != old[1]:
                        raise ValueError("conflicting_fit_measurement")
                else:
                    st["unscored"].add(e["role_id"])
            else:
                st["dup_rejected" if duplicate else "other_rejected"] += 1
    out = {}
    for fam, st in stats.items():
        staged = st["ingested"] + st["dup_rejected"] + st["other_rejected"]
        out[fam] = {"ingested": st["ingested"], "dup_rejected": st["dup_rejected"],
                    "other_rejected": st["other_rejected"], "net_new": len(st["new_ids"]),
                    "dup_rate": round(st["dup_rejected"] / staged, 4) if staged else 0.0,
                    "staged_total": staged, "sweep_dates": sorted(st["sweep_dates"]),
                    "fit_median": median([fit for _, fit in st["fits"].values()]),
                    "fit_coverage_complete": bool(st["new_ids"]) and not st["unscored"]
                                             and set(st["fits"]) == st["new_ids"]}
    return out


def recent_submitter_families(events):
    """Protect explicit sources with a submission claim; '*' means unknown owner."""
    return {family_of_event(e) or "*" for e in events
            if e.get("event_type") in ("submitted", "submission_claimed", "submission_observed")}


def open_proposal_families(loop):
    """Families already covered by an open source-yield proposal (de-dup)."""
    fams = set()
    for status in ("proposed", "claimed"):
        try:
            out = subprocess.run(
                [sys.executable, BLACKBOARD, "show",
                 "--domain", "source-yield", "--status", status],
                capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise ValueError("blackboard_read_failed")
            data = strict_loads(out.stdout)
            if type(data) is not dict or type(data.get("entries")) is not list:
                raise ValueError("blackboard_response_invalid")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ValueError("open_proposals_unverified") from None
        for ent in data.get("entries", []):
            title = (ent.get("title") or "").lower()
            for fam in set(PREFIX_TO_FAMILY.values()):
                slug = fam.replace("-", " ")
                if slug in title or fam in title:
                    fams.add(fam)
    return fams


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    middle = len(xs) // 2
    return xs[middle] if len(xs) % 2 else (xs[middle - 1] + xs[middle]) / 2


def build_demotion(fam, st, roster, window_hours):
    r = roster.get(fam, {})
    ceiling = r.get("ceiling_days")
    cadence = r.get("cadence_label") or "unstated"
    dates = ", ".join(st["sweep_dates"])
    resweep_note = st.get("resweep_note")
    evidence = (
        "Window: last %dh of telemetry events.jsonl. %s: %d ingested, "
        "%d dup-rejected, %d net-new, dup rate %.1f%% across %d distinct "
        "staging date(s) (%s). Roster cadence %s (ceiling %s) — "
        "intra-cadence re-sweep is documented re-harvest "
        "(roster: source-roster.md '%s'; cadence is the ceiling, not the "
        "floor). Hand-audit precedent: arm245-sources.md."
    ) % (window_hours, fam, st["ingested"], st["dup_rejected"],
         st["net_new"], st["dup_rate"] * 100, len(st["sweep_dates"]),
         dates, cadence,
         (str(ceiling) + "d") if ceiling else "unknown",
         r.get("heading", fam))
    if resweep_note:
        evidence += " %s." % resweep_note
    return {
        "domain": "source-yield",
        "kind": "demotion",
        "family": fam,
        "title": ("Demote %s to delta-gated cadence (%d%% dup rate on "
                  "intra-cadence re-sweep)") % (fam, round(st["dup_rate"] * 100)),
        "evidence": evidence,
        "proposal_or_fix": (
            "EXACT CHANGE (draft, needs human approval): demote %s from %s "
            "to delta-gated cadence — re-sweep only when a new roster cycle "
            "exists (e.g. new monthly thread) or the delta detector finds "
            "un-KEY'd board IDs; kills the intra-week delta sweep pattern. "
            "Review effort is unmeasured; operator approval is required. "
            "No queue, roster, or schedule changes are performed."
        ) % (fam, cadence),
    }


def build_promotion(fam, st, roster, window_hours):
    r = roster.get(fam, {})
    cadence = r.get("cadence_label") or "unstated"
    return {
        "domain": "source-yield",
        "kind": "promotion",
        "family": fam,
        "title": ("Promote %s (%d net-new, %.0f%% dup rate, fit median %s)") % (
            fam, st["net_new"], st["dup_rate"] * 100,
            st["fit_median"] if st["fit_median"] is not None else "n/a"),
        "evidence": (
            "Window: last %dh of telemetry events.jsonl. %s: %d net-new at "
            "%.1f%% dup rate, fit median %s (>= %d bar), across %d distinct "
            "staging date(s). Roster cadence %s (roster: source-roster.md)."
        ) % (window_hours, fam, st["net_new"], st["dup_rate"] * 100,
             st["fit_median"] if st["fit_median"] is not None else "n/a",
             PROMOTE_FIT_MEDIAN, len(st["sweep_dates"]), cadence),
        "proposal_or_fix": (
            "EXACT CHANGE (draft, needs human approval): raise %s cadence "
            "one step (e.g. WEEKLY->2x-weekly probe) or widen its sweep scope; "
            "measure next-cycle yield against this baseline. Review effort "
            "is unmeasured; operator approval is required. No queue, roster, "
            "or schedule changes are performed."
        ) % fam,
    }


def intra_cadence_resweep(fam, st, roster):
    """True when telemetry shows a sweep inside the roster's cadence ceiling
    measured from the roster's recorded last-swept date — i.e. the sweep the
    roster says shouldn't have happened (YC-mirrors 2026-09-16: one day after
    last-swept 2026-09-15 against a WEEKLY ceiling)."""
    r = roster.get(fam, {}) if roster else {}
    ceiling = r.get("ceiling_days")
    last = r.get("last_swept")
    if not ceiling or not last:
        return False, ""
    try:
        anchor = dt.date.fromisoformat(last)
    except ValueError:
        return False, ""
    for d in st["sweep_dates"]:
        try:
            dd = dt.date.fromisoformat(d)
        except ValueError:
            continue
        gap = (dd - anchor).days
        if 0 < gap < ceiling:
            return True, ("re-swept %s, %d day(s) after roster last-swept %s "
                          "(ceiling %dd — the cadence is the ceiling, not "
                          "the floor)") % (d, gap, last, ceiling)
    return False, ""


def measured_resweep(st, roster_entry):
    """Two dates prove a re-sweep only when their gap violates known cadence."""
    ceiling = roster_entry.get("ceiling_days")
    if type(ceiling) not in (int, float) or isinstance(ceiling, bool) or not math.isfinite(ceiling) or ceiling <= 0:
        return False
    dates = sorted(dt.date.fromisoformat(d) for d in st["sweep_dates"])
    return any(0 < (b - a).days < ceiling for a, b in zip(dates, dates[1:]))


def generate(events, roster, window_hours, guard_events,
             skip_families=frozenset(), *, now=None):
    """Return (proposals, suppressed, healthy). Proposals only — nothing applied."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if type(window_hours) is not int or not 1 <= window_hours <= 4320:
        raise ValueError("invalid_window_hours")
    events = _checked_events(events, now)
    guard_events = _checked_events(guard_events, now)
    if any(e["event_type"] in ("staged_ingested", "staged_rejected") and not family_of_event(e) for e in events):
        return [], [{"family": None, "kind": "hold", "reason": "ATTRIBUTION_INCOMPLETE: explicit source IDs required"}], []
    stats = rank_sources(events, now=now)
    submitters = recent_submitter_families(guard_events)
    proposals, suppressed, healthy = [], [], []
    if not stats:
        return [], [{"family": None, "kind": "hold", "reason": "NO_MEASURED_SOURCE_OBSERVATIONS"}], []

    for fam, st in sorted(stats.items()):
        if fam in skip_families:
            continue
        # --- promotion bar: rostered sources only (the exact change is a
        # cadence step; meaningless for one-shot enumeration families with
        # no roster cadence, e.g. census3x) ---
        if (st["net_new"] >= PROMOTE_NET_NEW_MIN
                and st["dup_rate"] < DUP_RATE_HEALTHY
                and (st["fit_median"] or 0) >= PROMOTE_FIT_MEDIAN
                and st["fit_coverage_complete"]
                and (roster.get(fam, {}) or {}).get("ceiling_days")):
            proposals.append(build_promotion(fam, st, roster, window_hours))
            continue
        # --- decay bar: dup rate over bar on an intra-cadence re-sweep ---
        resweep, resweep_ev = intra_cadence_resweep(fam, st, roster)
        resweep_in_window = measured_resweep(st, roster.get(fam, {}))
        if st["dup_rate"] >= DUP_RATE_DECAY and (resweep or resweep_in_window):
            if resweep_ev:
                st = dict(st, resweep_note=resweep_ev)
            if fam in submitters or "*" in submitters:
                suppressed.append({
                    "family": fam,
                    "kind": "demotion",
                    "reason": (
                        "GUARD: %s has a submission observation, or an observation "
                        "has unknown source ownership in the trailing window. "
                        "Decay bar met (%.1f%% dup%s); reconcile before demotion. "
                        "Provider acceptance is not authenticated here."
                    ) % (fam, st["dup_rate"] * 100,
                         (", " + st.get("resweep_note", "")) if st.get("resweep_note") else ""),
                })
            else:
                proposals.append(build_demotion(fam, st, roster, window_hours))
            continue
        healthy.append({"family": fam, "kind": "healthy",
                        "note": "%d net-new, %.1f%% dup" % (
                            st["net_new"], st["dup_rate"] * 100)})
    return proposals, suppressed, healthy


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate blackboard-publish-ready source-yield "
                    "demotion/promotion proposals. Proposals only; never "
                    "auto-applies.")
    ap.add_argument("--window-hours", type=int, default=30,
                    help="telemetry window for yield ranking (default 30; "
                         "pulse-shaped ~27h passes use 27 explicitly)")
    ap.add_argument("--submission-window-days", type=int, default=7,
                    help="trailing window for the submission recency guard "
                         "(default 7)")
    ap.add_argument("--check-open", action="store_true",
                    help="skip families already covered by an open "
                         "source-yield blackboard proposal")
    ap.add_argument("--loop", default="keel-octopus-pulse",
                    help="blackboard loop for publishing (default "
                         "keel-octopus-pulse)")
    ap.add_argument("--publish", action="store_true",
                    help="publish proposals to the blackboard as `proposed` "
                         "(dry-run prints JSON by default)")
    ap.add_argument("--feedback-input", help="bounded local measured-feedback JSON; proposal only")
    ap.add_argument("--budget-minutes", type=int, help="whole human-minute budget, 0 through 240")
    ap.add_argument("--now", help="explicit timezone-aware analysis time for reproducible replay")
    args = ap.parse_args(argv)

    if bool(args.feedback_input) != (args.budget_minutes is not None):
        ap.error("--feedback-input and --budget-minutes are required together")
    if args.feedback_input and (args.publish or args.check_open):
        ap.error("feedback budgets are local review proposals; --publish/--check-open do not apply")
    try:
        now = parse_ts(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        if now is None:
            raise ValueError("invalid_analysis_time")
        if args.feedback_input:
            from engines.source_feedback import propose
            with open(args.feedback_input, "rb") as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("feedback_size_limit")
            result = propose(strict_loads(raw), budget_minutes=args.budget_minutes, now=now)
            print(json.dumps(result, indent=1, allow_nan=False))
            return 0 if result["status"] == "PROPOSAL_ONLY" else 2
        if not 1 <= args.window_hours <= 4320 or not 1 <= args.submission_window_days <= 180:
            raise ValueError("invalid_window")
        # Read once, so ranking and submission protection share one snapshot.
        oldest = now - dt.timedelta(hours=max(args.window_hours, args.submission_window_days * 24))
        all_events = load_events(oldest, now=now)
        events = [e for e in all_events if parse_ts(e["ts"]) >= now - dt.timedelta(hours=args.window_hours)]
        guard_events = [e for e in all_events if parse_ts(e["ts"]) >= now - dt.timedelta(days=args.submission_window_days)]
        roster = parse_roster()
        skip = open_proposal_families(args.loop) if args.check_open else frozenset()
        proposals, suppressed, healthy = generate(
            events, roster, args.window_hours, guard_events, skip, now=now)
    except (OSError, ValueError, TypeError, OverflowError, UnicodeError):
        print(json.dumps({"status": "HOLD", "input_status": "UNVERIFIED",
                          "reason": "Input missing, malformed, stale, contradictory, or outside its contract; review local measurements.",
                          "proposals": [], "execution_authorized": False, "schedule_writes": 0}))
        return 2

    result = {
        "generated_at": now.isoformat(),
        "window_hours": args.window_hours,
        "submission_window_days": args.submission_window_days,
        "proposals": proposals,
        "suppressed": suppressed,
        "healthy": healthy,
        "status": "HOLD" if any(p.get("kind") == "hold" for p in suppressed) else "PROPOSAL_ONLY",
        "execution_authorized": False,
        "schedule_writes": 0,
    }

    if args.publish:
        published = []
        for p in proposals:
            cmd = [sys.executable, BLACKBOARD, "publish",
                   "--loop", args.loop,
                   "--domain", p["domain"],
                   "--title", p["title"],
                   "--evidence", p["evidence"],
                   "--proposal-or-fix", p["proposal_or_fix"]]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=60)
            try:
                published.append(json.loads(out.stdout or "{}"))
            except Exception:
                published.append({"ok": False, "error": out.stderr[-500:]})
        result["published"] = published

    print(json.dumps(result, indent=1))
    return 2 if result["status"] == "HOLD" else 0


if __name__ == "__main__":
    raise SystemExit(main())
