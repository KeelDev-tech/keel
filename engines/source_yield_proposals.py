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
  (telemetry `submitted` events attributed by role_id prefix). A source that
  still converts is never decayed by this module.

Wiring: the dev-support-deep-sweep Sweep arm runs this with `--check-open`
at the start of its source-yield judgment and publishes emitted proposals
as `proposed` instead of hand-detecting decay.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

HOME = os.path.expanduser("~")
# Private-host data layout. The host may point at its own data tree via
# KEEL_JOB_PIPELINE_DIR; the default matches this host's layout and is not
# part of the public contract. Readers below fail soft (OSError -> empty)
# so a foreign host degrades to no-data, never to a crash.
_JOB_PIPE = os.environ.get("KEEL_JOB_PIPELINE_DIR",
                           os.path.join(HOME, "workspace/job-pipeline"))
TELEMETRY = os.path.join(_JOB_PIPE, "telemetry/events.jsonl")
ROSTER = os.path.join(_JOB_PIPE, "discovery/source-roster.md")
# Repo-local tooling: resolved from this checkout, never from a home dir.
_KEEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    """Longest-prefix match on role_id; None for unmapped prefixes.

    Kept for the submission-recency guard and as the fallback attribution
    signal; staging-file matching (family_of_event) takes precedence.
    """
    rid = (role_id or "").upper()
    for prefix in sorted(PREFIX_TO_FAMILY, key=len, reverse=True):
        if rid.startswith(prefix + "-") or rid == prefix:
            return PREFIX_TO_FAMILY[prefix]
    return None


def family_of_event(e):
    """Attribution for a staged event: staging-file substring first
    (names the sweep), role_id prefix as fallback. None -> not ranked,
    never proposed on."""
    sf = ((e.get("details") or {}).get("staging_file") or "").lower()
    for needle, fam in STAGING_FILE_TO_FAMILY:
        if needle in sf:
            return fam
    return family_of(e.get("role_id", ""))


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def load_events(since):
    events = []
    try:
        with open(TELEMETRY) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = parse_ts(e.get("ts", ""))
                if ts and ts >= since:
                    events.append(e)
    except OSError:
        pass
    return events


def parse_roster():
    """heading -> {cadence_label, ceiling_days, last_swept(date)}. Best effort."""
    roster = {}
    try:
        text = open(ROSTER).read()
    except OSError:
        return roster
    for m in re.finditer(r"^## (.+)$", text, re.M):
        heading = m.group(1).strip()
        fam = HEADING_TO_FAMILY.get(heading)
        if not fam:
            continue
        chunk = text[m.end(): m.end() + 4000]
        cad = re.search(r"^-\s*Cadence:\s*(.+?)\s*$", chunk, re.M)
        last = re.search(r"^-\s*Last swept:\s*(\d{4}-\d{2}-\d{2})", chunk, re.M)
        cadence_label = cad.group(1).strip() if cad else ""
        ceiling = None
        for label, days in CADENCE_DAYS.items():
            if label.upper() in cadence_label.upper():
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
    m = re.search(r"(\d{4}-\d{2}-\d{2})", d)
    if m:
        return m.group(1)
    ts = parse_ts(e.get("ts", ""))
    return ts.strftime("%Y-%m-%d") if ts else ""


def rank_sources(events):
    """Per-family yield stats from staged_ingested / staged_rejected events."""
    stats = defaultdict(lambda: {
        "ingested": 0, "dup_rejected": 0, "sweep_dates": set(),
        "fits": [], "submitted": 0,
    })
    for e in events:
        et = e.get("event_type")
        if et not in ("staged_ingested", "staged_rejected"):
            continue
        fam = family_of_event(e)
        if not fam:
            continue
        s = stats[fam]
        d = batch_date(e)
        if d:
            s["sweep_dates"].add(d)
        if et == "staged_ingested":
            s["ingested"] += 1
            fit = (e.get("details") or {}).get("fit_score")
            if isinstance(fit, (int, float)):
                s["fits"].append(fit)
        else:
            s["dup_rejected"] += 1
    out = {}
    for fam, s in stats.items():
        staged = s["ingested"] + s["dup_rejected"]
        fits = sorted(s["fits"])
        out[fam] = {
            "ingested": s["ingested"],
            "dup_rejected": s["dup_rejected"],
            "net_new": s["ingested"],
            "dup_rate": round(s["dup_rejected"] / staged, 4) if staged else 0.0,
            "staged_total": staged,
            "sweep_dates": sorted(s["sweep_dates"]),
            "fit_median": fits[len(fits) // 2] if fits else None,
        }
    return out


def recent_submitter_families(events):
    """Families with at least one `submitted` event (the demotion guard)."""
    fams = set()
    for e in events:
        if e.get("event_type") == "submitted":
            fam = family_of(e.get("role_id", ""))
            if fam:
                fams.add(fam)
    return fams


def open_proposal_families(loop):
    """Families already covered by an open source-yield proposal (de-dup)."""
    fams = set()
    for status in ("proposed", "claimed"):
        try:
            out = subprocess.run(
                [sys.executable, BLACKBOARD, "show",
                 "--domain", "source-yield", "--status", status],
                capture_output=True, text=True, timeout=30)
            data = json.loads(out.stdout or "{}")
        except Exception:
            continue
        for ent in data.get("entries", []):
            title = (ent.get("title") or "").lower()
            for fam in set(PREFIX_TO_FAMILY.values()):
                slug = fam.replace("-", " ")
                if slug in title or fam in title:
                    fams.add(fam)
    return fams


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


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
            "Roster file is untouched by this module. COST: ~15-30 compute-min "
            "for one dev arm to draft the roster change (no HTTP, no "
            "browser); 0 Trent taps; 0 queue writes; risk LOW (proposal only)."
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
            "measure next-cycle yield against this baseline. COST: ~15-30 "
            "compute-min for one dev arm (no HTTP); 0 Trent taps; 0 queue "
            "writes; risk LOW (proposal only)."
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


def generate(events, roster, window_hours, guard_events,
             skip_families=frozenset()):
    """Return (proposals, suppressed, healthy). Proposals only — nothing applied."""
    stats = rank_sources(events)
    submitters = recent_submitter_families(guard_events)
    proposals, suppressed, healthy = [], [], []

    for fam, st in sorted(stats.items()):
        if fam in skip_families:
            continue
        # --- promotion bar: rostered sources only (the exact change is a
        # cadence step; meaningless for one-shot enumeration families with
        # no roster cadence, e.g. census3x) ---
        if (st["net_new"] >= PROMOTE_NET_NEW_MIN
                and st["dup_rate"] < DUP_RATE_HEALTHY
                and (st["fit_median"] or 0) >= PROMOTE_FIT_MEDIAN
                and (roster.get(fam, {}) or {}).get("ceiling_days")):
            proposals.append(build_promotion(fam, st, roster, window_hours))
            continue
        # --- decay bar: dup rate over bar on an intra-cadence re-sweep ---
        resweep, resweep_ev = intra_cadence_resweep(fam, st, roster)
        resweep_in_window = len(st["sweep_dates"]) >= DECAY_SWEEP_DATES_MIN
        if st["dup_rate"] >= DUP_RATE_DECAY and (resweep or resweep_in_window):
            if resweep_ev:
                st = dict(st, resweep_note=resweep_ev)
            if fam in submitters:
                suppressed.append({
                    "family": fam,
                    "kind": "demotion",
                    "reason": (
                        "GUARD: %s produced a submission in the trailing "
                        "submission window — decay bar met (%.1f%% dup%s) "
                        "but the source still converts; no demotion "
                        "proposed."
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
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    events = load_events(now - dt.timedelta(hours=args.window_hours))
    guard_events = load_events(
        now - dt.timedelta(days=args.submission_window_days))
    roster = parse_roster()
    skip = open_proposal_families(args.loop) if args.check_open else frozenset()
    proposals, suppressed, healthy = generate(
        events, roster, args.window_hours, guard_events, skip)

    result = {
        "generated_at": now.isoformat(),
        "window_hours": args.window_hours,
        "submission_window_days": args.submission_window_days,
        "proposals": proposals,
        "suppressed": suppressed,
        "healthy": healthy,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
