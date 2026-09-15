#!/usr/bin/env python3
"""Conversion analytics for the Western Application Pipeline.

Reads the application ledger (SUBMITTED rows) and the append-only
telemetry log's employer_response events, then computes:

  1. Lane-level metrics (resume_lane): submissions, linked outcomes,
     acknowledgment / rejection / invite / assessment rates.
  2. Source-tier conversion: conservative rule-based tiers with an
     explicit "unknown" bucket — the unknown share IS the headline.
  3. Acknowledgment latency: hours from submission to first AUTO_ACK.

HONESTY RULES (fail-closed analytics):
  - A rate is reported only when its denominator >= MIN_N (5). Below that
    the metric reads "insufficient outcome data" — never a zero or a rate
    that looks like signal.
  - Missing ats / source / transport / lane values are reported as data
    gaps with counts and shares, never silently dropped.
  - Source tiers are assigned by conservative rules; anything ambiguous
    lands in "unknown". No guessing.
  - Employer responses link to ledger rows by normalized company name and
    are attributed to the most recent SUBMITTED row at-or-before the event
    timestamp. The linking rule is printed in every report.
  - "Hypothesis-ready" bullets are hedged and labeled as hypotheses, not
    claims.

Two response-event formats are normalized:
  - current: details.outcome in {AUTO_ACK, REJECTION, INTERVIEW_INVITE,
    ASSESSMENT, INFO_REQUEST, OTHER}, details.company_key
  - legacy backfill: details.response in {interview_invited -> INTERVIEW_INVITE,
    waitlisted -> OTHER (limbo state, neither rejection nor invite)}

Decisive outcomes (for the cron threshold) = REJECTION + INTERVIEW_INVITE
+ ASSESSMENT + INFO_REQUEST. Acknowledgments are confirmatory, not
decisive.

Outputs (hidden_files/outcome-tracking/):
  outcome-analytics-<YYYYMMDD-HHMM>.json
  outcome-analytics-<YYYYMMDD-HHMM>.md
  outcome-analytics-latest.json / .md  (copies of the newest run)

CLI: python3 outcome_analytics.py [--out DIR]
Prints "SURFACE: ..." lines when a lane newly crosses 5+ decisive
outcomes versus the previous latest report — the weekly cron surfaces
ONLY those lines and stays silent otherwise.
"""

import collections
import json
import os
import re
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
from keel_paths import HOME as PIPE  # noqa: E402
OUT_DIR = os.path.join(PIPE, "hidden_files", "outcome-tracking")

MIN_N = 5            # minimum denominator before a rate is reported
LATENCY_MIN_N = 3    # minimum samples before latency stats are reported

VALID_OUTCOMES = {"AUTO_ACK", "REJECTION", "INTERVIEW_INVITE",
                  "ASSESSMENT", "INFO_REQUEST", "OTHER"}
LEGACY_MAP = {"interview_invited": "INTERVIEW_INVITE",
              "waitlisted": "OTHER"}

# ---------------------------------------------------------------- loading

def load_submitted(ledger_path=None):
    path = ledger_path or os.path.join(PIPE, "data", "application-ledger.json")
    rows = json.load(open(path))
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    return [r for r in rows if r.get("status") == "SUBMITTED"]


def load_responses(events_path=None):
    path = events_path or os.path.join(PIPE, "data", "telemetry", "events.jsonl")
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("event_type") != "employer_response":
                continue
            d = e.get("details", {}) or {}
            outcome = (d.get("outcome") or "").strip().upper()
            if outcome not in VALID_OUTCOMES:
                legacy = (d.get("response") or "").strip().lower()
                outcome = LEGACY_MAP.get(legacy, "OTHER") if legacy else "OTHER"
            # Backfilled events carry the backfill RUN time, not the actual
            # receipt time — their timestamps are not trustworthy for
            # latency math.
            trustworthy = not d.get("backfilled")
            out.append({
                "ts": e.get("ts") or "",
                "ts_trustworthy": trustworthy,
                "company": e.get("company") or "",
                "company_key": (d.get("company_key") or "").strip().lower(),
                "outcome": outcome,
                "source": e.get("source") or "",
            })
    return out


# ---------------------------------------------------------------- helpers

def norm_company(name):
    s = (name or "").lower()
    s = re.sub(r"\b(llc|inc|incorporated|corp|corporation|co|ltd|limited|"
               r"company|vineyards|wines|wine)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s or (name or "").lower().strip()


def parse_ts(ts):
    """Tolerant timestamp parse -> aware datetime, or None."""
    if not ts:
        return None
    s = str(ts).strip()
    # ledger style: "2026-09-14 21:25 PDT"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}):(\d{2}))?\s*"
                 r"(PDT|PST|PT)?$", s)
    if m:
        off = -7 if m.group(4) in (None, "PDT", "PT") else -8
        hh, mm = int(m.group(2) or 0), int(m.group(3) or 0)
        dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
            hour=hh, minute=mm)
        return dt.replace(
            tzinfo=timezone(__import__("datetime").timedelta(hours=off)))
    # ISO with offset
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def source_tier(row):
    """Conservative tier assignment. Ambiguous -> 'unknown' (never guessed)."""
    transport = (row.get("transport") or "").lower()
    source = (row.get("source") or "").lower()
    ats = (row.get("ats") or "").lower()
    if transport == "api-direct":
        return "api_direct"
    if re.search(r"indeed|talent\.com|ziprecruiter|glassdoor|simplyhired|"
                 r"linkedin.*job|jobot", source):
        return "aggregator"
    if re.search(r"winebusiness|employer.?direct|company.?site|careers.?page|"
                 r"direct.?apply|employer.?portal", source):
        return "employer_site"
    if transport == "browser" and (ats.split() or [""])[0] in (
            "greenhouse", "lever", "ashby", "workday", "icims", "taleo",
            "smartrecruiters", "adp", "breezy", "jazzhr", "workable"):
        return "ats_browser"
    return "unknown"


# ---------------------------------------------------------------- linking

def link_responses(rows, events):
    """Attribute each response event to one SUBMITTED row.

    Rule: normalized company match; among matching SUBMITTED rows, pick
    the one with the latest parseable date_submitted that is <= the event
    ts (fallback: latest parseable date_submitted; then the first match).
    Events with no company match stay unlinked and are reported as such.
    Returns (row_id -> [events], unlinked_events).
    """
    by_company = collections.defaultdict(list)
    for i, r in enumerate(rows):
        key = norm_company(r.get("company"))
        if key:
            by_company[key].append(i)
    # also index company_key aliases from events lazily below
    linked = collections.defaultdict(list)
    unlinked = []
    for e in events:
        keys = {norm_company(e["company"]), (e["company_key"] or "").strip()}
        keys.discard("")
        cand = []
        for k in keys:
            cand.extend(by_company.get(k, []))
        cand = sorted(set(cand))
        if not cand:
            unlinked.append(e)
            continue
        ets = parse_ts(e["ts"])
        best, best_sub = None, None
        for i in cand:
            sts = parse_ts(rows[i].get("date_submitted"))
            if ets and sts and sts <= ets and (best_sub is None or sts > best_sub):
                best, best_sub = i, sts
            elif best is None and sts and (best_sub is None or sts > best_sub):
                best, best_sub = i, sts
        linked[best if best is not None else cand[0]].append(e)
    return linked, unlinked


# ---------------------------------------------------------------- metrics

def rate(num, den):
    if den is None or den < MIN_N:
        return {"value": None, "note": "insufficient outcome data",
                "n": num, "denominator": den}
    return {"value": round(num / den, 3), "n": num, "denominator": den,
            "note": None}


def lane_metrics(rows, linked):
    lanes = collections.defaultdict(list)
    for i, r in enumerate(rows):
        lanes[r.get("resume_lane") or "unlabeled"].append(i)
    out = {}
    for lane, idxs in sorted(lanes.items()):
        evs = [e for i in idxs for e in linked.get(i, [])]
        by_out = collections.Counter(e["outcome"] for e in evs)
        n_sub = len(idxs)
        n_linked = len({i for i in idxs if linked.get(i)})
        decisive = sum(by_out[o] for o in
                       ("REJECTION", "INTERVIEW_INVITE", "ASSESSMENT",
                        "INFO_REQUEST"))
        out[lane] = {
            "submissions": n_sub,
            "submissions_with_linked_response": n_linked,
            "decisive_outcomes": decisive,
            "outcomes": dict(by_out),
            "ack_rate": rate(by_out.get("AUTO_ACK", 0), n_sub),
            "rejection_rate": rate(by_out.get("REJECTION", 0), decisive),
            "invite_rate": rate(by_out.get("INTERVIEW_INVITE", 0), decisive),
        }
    return out


def tier_metrics(rows, linked):
    tiers = collections.defaultdict(list)
    for i, r in enumerate(rows):
        tiers[source_tier(r)].append(i)
    out = {}
    for tier, idxs in sorted(tiers.items()):
        evs = [e for i in idxs for e in linked.get(i, [])]
        by_out = collections.Counter(e["outcome"] for e in evs)
        decisive = sum(by_out[o] for o in
                       ("REJECTION", "INTERVIEW_INVITE", "ASSESSMENT",
                        "INFO_REQUEST"))
        out[tier] = {
            "submissions": len(idxs),
            "decisive_outcomes": decisive,
            "outcomes": dict(by_out),
            "ack_rate": rate(by_out.get("AUTO_ACK", 0), len(idxs)),
            "invite_rate": rate(by_out.get("INTERVIEW_INVITE", 0), decisive),
        }
    return out


def ack_latency_hours(rows, linked):
    hours = []
    untrusted = 0
    for i, r in enumerate(rows):
        sts = parse_ts(r.get("date_submitted"))
        if not sts:
            continue
        for e in linked.get(i, []):
            if e["outcome"] != "AUTO_ACK":
                continue
            if not e.get("ts_trustworthy"):
                untrusted += 1
                continue
            a = parse_ts(e["ts"])
            if a and a >= sts:
                hours.append((a - sts).total_seconds() / 3600.0)
    if len(hours) < LATENCY_MIN_N:
        note = ("insufficient outcome data"
                + (f"; {untrusted} linked ack(s) carry backfill-run "
                   f"timestamps, not actual receipt times" if untrusted else ""))
        return {"n": len(hours), "stats": None, "note": note}
    s = sorted(hours)
    mid = s[len(s) // 2]
    return {"n": len(hours),
            "stats": {"min_h": round(s[0], 1), "median_h": round(mid, 1),
                      "max_h": round(s[-1], 1)},
            "note": None}


def data_gaps(rows):
    n = len(rows)
    gaps = {}
    for field in ("transport", "ats", "resume_lane", "source",
                  "date_submitted"):
        missing = sum(1 for r in rows if not r.get(field))
        gaps[field] = {"missing": missing, "total": n,
                       "share": round(missing / n, 3) if n else 0}
    return gaps


# ---------------------------------------------------------------- report

def hypotheses(lanes):
    out = []
    for lane, m in lanes.items():
        inv = m["invite_rate"]
        if inv.get("value") is not None and inv["value"] > 0:
            out.append(
                f"HYPOTHESIS [{lane}]: invite signal present "
                f"({inv['n']}/{inv['denominator']}). Worth watching as n "
                f"grows; do not overweight — n is small.")
        elif m["decisive_outcomes"] >= MIN_N and inv.get("value") == 0:
            out.append(
                f"HYPOTHESIS [{lane}]: {m['decisive_outcomes']} decisive "
                f"outcomes, zero invites so far — possible fit or "
                f"positioning problem in this lane; revisit targeting "
                f"before scaling volume.")
        if m["ack_rate"].get("value") is not None and m["ack_rate"]["value"] < 0.3:
            out.append(
                f"HYPOTHESIS [{lane}]: ack rate "
                f"{m['ack_rate']['value']:.0%} ({m['ack_rate']['n']}/"
                f"{m['ack_rate']['denominator']}) — applications may not be "
                f"landing (deliverability) or employers rarely confirm; "
                f"verify a sample before inferring rejection.")
    if not out:
        out.append("No lane has enough decisive outcomes for even a "
                   "hypothesis — keep collecting; do not infer from zeros.")
    return out


def build_report(rows, events):
    linked, unlinked = link_responses(rows, events)
    lanes = lane_metrics(rows, linked)
    tiers = tier_metrics(rows, linked)
    gaps = data_gaps(rows)
    lat = ack_latency_hours(rows, linked)
    linked_n = sum(len(v) for v in linked.values())
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {"submitted_rows": len(rows),
                   "response_events": len(events),
                   "events_linked": linked_n,
                   "events_unlinked": len(unlinked)},
        "linking_rule": ("normalized company match; attributed to the most "
                         "recent SUBMITTED row at-or-before the event ts"),
        "min_n_for_rates": MIN_N,
        "data_gaps": gaps,
        "lanes": lanes,
        "source_tiers": tiers,
        "ack_latency_hours": lat,
        "hypotheses": hypotheses(lanes),
        "unlinked_events": [
            {"ts": e["ts"], "company": e["company"],
             "outcome": e["outcome"]} for e in unlinked[:20]],
    }
    return report


def _fmt_rate(r):
    if r.get("value") is None:
        return f"insufficient outcome data (n={r['n']}, den={r['denominator']})"
    return f"{r['value']:.1%} ({r['n']}/{r['denominator']})"


def render_markdown(report):
    L = []
    A = L.append
    A("# Outcome Analytics — Western Application Pipeline")
    A("")
    A(f"_Generated {report['generated_at']}. "
      f"{report['inputs']['submitted_rows']} SUBMITTED rows, "
      f"{report['inputs']['response_events']} employer-response events, "
      f"{report['inputs']['events_linked']} linked. "
      f"Rates need n≥{report['min_n_for_rates']} or read "
      f"'insufficient outcome data'._")
    A("")
    A("## Data quality (gaps first)")
    A("")
    A("| field | missing | share |")
    A("|---|---|---|")
    for f, g in report["data_gaps"].items():
        A(f"| {f} | {g['missing']}/{g['total']} | {g['share']:.0%} |")
    A("")
    A("_Unknowns are reported as unknowns, never guessed._")
    A("")
    A("## Lane metrics")
    A("")
    A("| lane | submitted | w/ response | decisive | ack rate | "
      "rejection rate | invite rate |")
    A("|---|---|---|---|---|---|---|")
    for lane, m in report["lanes"].items():
        A(f"| {lane} | {m['submissions']} | "
          f"{m['submissions_with_linked_response']} | "
          f"{m['decisive_outcomes']} | {_fmt_rate(m['ack_rate'])} | "
          f"{_fmt_rate(m['rejection_rate'])} | {_fmt_rate(m['invite_rate'])} |")
    A("")
    A("## Source-tier conversion")
    A("")
    A("| tier | submitted | decisive | ack rate | invite rate |")
    A("|---|---|---|---|---|")
    for t, m in report["source_tiers"].items():
        A(f"| {t} | {m['submissions']} | {m['decisive_outcomes']} | "
          f"{_fmt_rate(m['ack_rate'])} | {_fmt_rate(m['invite_rate'])} |")
    A("")
    lat = report["ack_latency_hours"]
    A("## Acknowledgment latency")
    A("")
    if lat["stats"]:
        s = lat["stats"]
        A(f"n={lat['n']}: min {s['min_h']}h / median {s['median_h']}h / "
          f"max {s['max_h']}h (submission → first AUTO_ACK, "
          f"trustworthy timestamps only).")
    else:
        A(f"{lat['note']} (n={lat['n']}).")
    A("")
    A("_Recommendation: the outcome listener should record each Gmail "
      "message's actual receipt date on future events; the 2026-09-14 "
      "backfill stamped run time, which cannot support latency math._")
    A("")
    A("## Hypothesis-ready reads (not claims)")
    A("")
    for h in report["hypotheses"]:
        A(f"- {h}")
    A("")
    A("## Method notes")
    A("")
    A(f"- Linking: {report['linking_rule']}.")
    A(f"- Unlinked events this run: {report['inputs']['events_unlinked']}.")
    A("- Legacy backfill labels normalized: interview_invited → "
      "INTERVIEW_INVITE; waitlisted → OTHER (limbo state).")
    A("- Decisive outcomes = REJECTION + INTERVIEW_INVITE + ASSESSMENT + "
      "INFO_REQUEST. Acknowledgments are confirmatory, not decisive.")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- driver

def previous_latest(out_dir):
    p = os.path.join(out_dir, "outcome-analytics-latest.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def threshold_crossings(prev, report):
    """Lanes newly at/above 5 decisive outcomes vs the previous report."""
    if not prev:
        return []
    prev_lanes = (prev.get("lanes") or {})
    out = []
    for lane, m in (report.get("lanes") or {}).items():
        now = m.get("decisive_outcomes", 0)
        was = (prev_lanes.get(lane) or {}).get("decisive_outcomes", 0)
        if now >= MIN_N and was < MIN_N:
            out.append(f"SURFACE: lane '{lane}' crossed {MIN_N} decisive "
                       f"outcomes ({was} -> {now})")
    return out


def main(argv):
    out_dir = OUT_DIR
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
    os.makedirs(out_dir, exist_ok=True)

    rows = load_submitted()
    events = load_responses()
    report = build_report(rows, events)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    jp = os.path.join(out_dir, f"outcome-analytics-{stamp}.json")
    mp = os.path.join(out_dir, f"outcome-analytics-{stamp}.md")
    json.dump(report, open(jp, "w"), indent=1)
    open(mp, "w").write(render_markdown(report))
    json.dump(report, open(os.path.join(out_dir,
                                        "outcome-analytics-latest.json"), "w"),
              indent=1)
    open(os.path.join(out_dir, "outcome-analytics-latest.md"),
         "w").write(render_markdown(report))

    print(f"report: {mp}")
    return report, jp, mp


if __name__ == "__main__":
    # Snapshot the previous latest BEFORE main() overwrites it, so the
    # threshold comparison is run-over-run, not report-vs-itself.
    prev = previous_latest(OUT_DIR if "--out" not in sys.argv[1:]
                           else sys.argv[sys.argv.index("--out") + 1])
    report, jp, mp = main(sys.argv[1:])
    for line in threshold_crossings(prev, report):
        print(line)
