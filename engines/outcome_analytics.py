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
  - Employer responses link to ledger rows through tiered linkage:
    (1) deterministic role_id match; (2) receipt/submission-ref match
    against the row's submission_ref / receipt_ref / confirmation
    evidence; (3) posting_url / ats_job_id identity match against the
    row's posting_url / application_url; (4) normalized company-name
    fallback — a single candidate keeps the existing latest-date<=ts
    rule. Ambiguous company matches are held in an explicit review queue
    (HOLD_AMBIGUOUS_FOR_REVIEW), never silently resolved. Every linked
    or held event carries a "linkage" provenance dict; original evidence
    is never mutated. The linking rule is printed in every report.
  - "Hypothesis-ready" bullets are hedged and labeled as hypotheses, not
    claims.

Two response-event formats are normalized:
  - current: details.outcome in {AUTO_ACK, REJECTION, INTERVIEW_INVITE,
    ASSESSMENT, INFO_REQUEST, OFFER, OTHER}, details.company_key
  - legacy backfill: details.response in {interview_invited -> INTERVIEW_INVITE,
    waitlisted -> OTHER (limbo state, neither rejection nor invite),
    offer -> OFFER}

Linkable ledger statuses: SUBMITTED, INTERVIEW_INVITED, WAITLISTED,
ASSESSMENT, OFFERED. Response events may land on rows that already
advanced past SUBMITTED (e.g. an invite event for a row already marked
INTERVIEW_INVITED). Dead statuses are never indexed.

Decisive outcomes (for the cron threshold) = REJECTION + INTERVIEW_INVITE
+ ASSESSMENT + INFO_REQUEST + OFFER. Acknowledgments are confirmatory, not
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
import hashlib
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
                  "ASSESSMENT", "INFO_REQUEST", "OFFER", "OTHER"}
LEGACY_MAP = {"interview_invited": "INTERVIEW_INVITE",
              "waitlisted": "OTHER",
              "offer": "OFFER"}

# ---------------------------------------------------------------- loading

def load_submitted(ledger_path=None):
    path = ledger_path or os.path.join(PIPE, "data", "application-ledger.json")
    rows = json.load(open(path))
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    return [r for r in rows if r.get("status") == "SUBMITTED"]


# Statuses a response event may legitimately link to. Rows that already
# advanced past SUBMITTED via an employer response stay linkable so
# follow-up events (a second ack, an assessment invite) don't go unlinked.
# Deliberately NOT "any row with a company": indexing dead rows
# (SKIP/CLOSED/REJECTED/...) would let stray events misattribute to dead
# leads — an event matching a dead lead stays unlinked (fail-closed).
LINKABLE_STATUSES = ("SUBMITTED", "INTERVIEW_INVITED", "WAITLISTED",
                     "ASSESSMENT", "OFFERED")


def load_linkable_rows(ledger_path=None):
    """Ledger rows response events may link to (see LINKABLE_STATUSES)."""
    path = ledger_path or os.path.join(PIPE, "data", "application-ledger.json")
    rows = json.load(open(path))
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    return [r for r in rows if r.get("status") in LINKABLE_STATUSES]


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
            # latency math. Live listener events now carry a real receipt
            # date (details.message_date), which is preferred when present.
            trustworthy = bool(d.get("message_date")) or not d.get("backfilled")
            out.append({
                "ts": d.get("message_date") or e.get("ts") or "",
                "ts_trustworthy": trustworthy,
                "company": e.get("company") or "",
                "company_key": (d.get("company_key") or "").strip().lower(),
                "outcome": outcome,
                "source": e.get("source") or "",
                # Linkage evidence (additive; powers deterministic tiers).
                "role_id": (d.get("role_id") or "").strip(),
                "receipt_ref": (d.get("receipt_ref") or
                                d.get("submission_ref") or
                                d.get("confirmation_ref") or "").strip(),
                "message_id": d.get("message_id") or "",
                "match_how": d.get("match_how") or "",
                "posting_url": d.get("posting_url") or "",
                "ats_job_id": (d.get("ats_job_id") or "").strip(),
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
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                hour=hh, minute=mm)
        except ValueError:
            return None
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
# Toggle for tier-4 company-name ambiguity. When True (default), a
# response event whose normalized company name matches more than one
# distinct linkable ledger row is NOT attributed — it is held in an
# explicit review queue and reported as held, never silently resolved.
# When False, the legacy behavior applies (latest-date<=ts rule across
# all candidates).
HOLD_AMBIGUOUS_FOR_REVIEW = True

# Query version pinned into every report's snapshot block. Bump when the
# linkage rules, denominators, or metric definitions change so
# historical reports stay comparable.
QUERY_VERSION = "outcome-analytics/1"

# Plain-language denominator contract, published in every snapshot.
DENOMINATOR_DEFINITION = (
    "denominators are SUBMITTED ledger rows; response rates count "
    "distinct applications; decisive rates use decisive applications")


def _norm_ref(ref):
    """Normalize a receipt/submission reference for comparison."""
    return re.sub(r"\s+", "", (ref or "").strip().lower())


def _norm_url(url):
    """Normalize a posting/application URL for identity comparison."""
    return (url or "").strip().rstrip("/").lower()


def _row_refs(r):
    """Normalized receipt/submission reference values on a ledger row."""
    refs = set()
    for k in ("submission_ref", "receipt_ref", "confirmation_ref",
              "reference_id"):
        v = _norm_ref(r.get(k))
        if v:
            refs.add(v)
    return refs


def _row_urls(r):
    """Normalized posting/application URLs on a ledger row."""
    urls = set()
    for k in ("posting_url", "application_url", "job_url"):
        v = _norm_url(r.get(k))
        if v:
            urls.add(v)
    return urls


def _row_confirmation_evidence(r):
    """Free-text confirmation evidence fields on a ledger row."""
    return [str(r.get(k) or "") for k in ("confirmation_text",
                                          "confirmation_url")]


def link_responses(rows, events):
    """Attribute each response event to one linkable ledger row.

    Tiered linkage (first hit wins):
      Tier 1 — deterministic: event details.role_id matches a linkable
        row's role_id.
      Tier 2 — receipt: event details.receipt_ref (or submission_ref /
        confirmation_ref) matches the row's submission_ref / receipt_ref
        / confirmation_ref exactly, or appears inside the row's
        confirmation_text / confirmation_url evidence.
      Tier 3 — provider identity: event details.posting_url matches the
        row's posting_url / application_url exactly (normalized), or the
        event's ats_job_id appears inside a row posting URL.
      Tier 4 — company-name fallback: normalized company match; among
        matching linkable rows, the latest-date<=ts rule applies.
        If the company name matches more than one distinct linkable row,
        the event is NOT attributed — it is held for review when
        HOLD_AMBIGUOUS_FOR_REVIEW is True (fail-closed; ambiguous matches
        are never silently resolved).

    All tiers require a parseable submission date no later than the
    response date. An explicit role identity that cannot match stays
    unlinked; it must not fall back to another application at the company.
    Only rows whose status is in LINKABLE_STATUSES are indexed at any
    tier (dead rows stay unlinked even if a caller passes them unfiltered
    — fail-closed). The listener's raw company_key is normalized the same
    way as ledger names before matching.

    Every linked or held event is a COPY of the input event with a
    "linkage" provenance dict {"tier", "rule", "basis"} attached; the
    original event dicts are never mutated. Unlinked events pass through
    unchanged (no company match at any tier).

    Returns (linked, unlinked, held_for_review): linked is row-index ->
    [event copies] into the `rows` list passed in; unlinked is the list
    of events with no match; held_for_review is the list of ambiguous
    tier-4 event copies. Held events are neither linked nor unlinked and
    must stay OUT of rate denominators.
    """
    linkable_idx = [i for i, r in enumerate(rows)
                    if r.get("status") in LINKABLE_STATUSES]
    by_company = collections.defaultdict(list)
    by_role_id = {}
    by_ref = collections.defaultdict(list)
    by_url = collections.defaultdict(list)
    for i in linkable_idx:
        r = rows[i]
        key = norm_company(r.get("company"))
        if key:
            by_company[key].append(i)
        rid = (r.get("role_id") or "").strip()
        if rid and rid not in by_role_id:
            by_role_id[rid] = i
        for ref in _row_refs(r):
            by_ref[ref].append(i)
        for u in _row_urls(r):
            by_url[u].append(i)

    linked = collections.defaultdict(list)
    unlinked = []
    held = []

    def attach(e, tier, rule, basis):
        """Copy the event and attach tier provenance (never mutates e)."""
        c = dict(e)
        c["linkage"] = {"tier": tier, "rule": rule, "basis": basis}
        return c

    for e in events:
        ets = parse_ts(e.get("ts"))
        eligible = {i for i in linkable_idx
                    if ets is not None
                    and (sts := parse_ts(rows[i].get("date_submitted"))) is not None
                    and sts <= ets}
        if not eligible:
            unlinked.append(e)
            continue
        # --- Tier 1: deterministic role_id match ---------------------
        rid = (e.get("role_id") or "").strip()
        if rid:
            i = by_role_id.get(rid)
            if i in eligible:
                linked[i].append(attach(e, 1, "role_id match",
                                        f"event role_id={rid!r} == row "
                                        f"role_id (row idx {i})"))
            else:
                unlinked.append(e)
            continue
        # --- Tier 2: receipt / submission ref -------------------------
        ref = _norm_ref(e.get("receipt_ref") or e.get("submission_ref"))
        tier2 = None
        ref_candidates = sorted(set(by_ref.get(ref, [])) & eligible)
        if ref and ref_candidates:
            i = ref_candidates[0]
            tier2 = (i, f"event receipt_ref={ref!r} == row ref (row idx {i})")
        if tier2 is None and ref:
            for i in sorted(eligible):  # reference inside confirmation evidence
                ev = [x for x in _row_confirmation_evidence(rows[i])
                      if re.search(r"(?<![a-z0-9_-])" + re.escape(ref)
                                   + r"(?![a-z0-9_-])", x.lower())]
                if ev:
                    tier2 = (i, f"event receipt_ref={ref!r} inside row "
                                f"confirmation evidence (row idx {i})")
                    break
        if tier2 is not None:
            i, basis = tier2
            linked[i].append(attach(e, 2, "receipt_ref match", basis))
            continue
        if ref:
            unlinked.append(e)
            continue
        # --- Tier 3: posting / ATS job identity -----------------------
        eurl = _norm_url(e.get("posting_url"))
        jobid = (e.get("ats_job_id") or "").strip().lower()
        tier3 = None
        url_candidates = sorted(set(by_url.get(eurl, [])) & eligible)
        if eurl and url_candidates:
            i = url_candidates[0]
            tier3 = (i, f"event posting_url == row posting_url (row idx {i})")
        if tier3 is None and jobid:
            for i in sorted(eligible):
                if any(re.search(r"(?<![a-z0-9_-])" + re.escape(jobid)
                                 + r"(?![a-z0-9_-])", u)
                       for u in _row_urls(rows[i])):
                    tier3 = (i, f"event ats_job_id={jobid!r} inside row "
                                f"posting_url (row idx {i})")
                    break
        if tier3 is not None:
            i, basis = tier3
            linked[i].append(attach(e, 3, "posting/ats identity match",
                                    basis))
            continue
        if eurl or jobid:
            unlinked.append(e)
            continue
        # --- Tier 4: company-name fallback ------------------------------
        keys = {norm_company(e.get("company")),
                norm_company(e.get("company_key"))}
        keys.discard("")
        cand = []
        for k in keys:
            cand.extend(by_company.get(k, []))
        cand = sorted(set(cand) & eligible)
        if not cand:
            unlinked.append(e)
            continue
        identities = {_row_id_key(rows[i]) for i in cand}
        if len(identities) > 1 and HOLD_AMBIGUOUS_FOR_REVIEW:
            held.append(attach(
                e, 4, "ambiguous company match held for review",
                f"company {e.get('company')!r} matched "
                f"{len(identities)} distinct linkable rows; not attributed"))
            continue
        best, best_sub = None, None
        for i in cand:
            sts = parse_ts(rows[i].get("date_submitted"))
            if ets and sts and sts <= ets and (best_sub is None or sts > best_sub):
                best, best_sub = i, sts
        i = best
        linked[i].append(attach(
            e, 4, "company-name fallback (latest-date<=ts rule)",
            f"company {e.get('company')!r} matched 1 row; latest "
            f"date_submitted<=ts attributed (row idx {i})"))
    return linked, unlinked, held


def _row_id(r):
    """Stable identity for cross-list index mapping."""
    return r.get("role_id") or (r.get("company"), r.get("title"))


def _row_id_key(r):
    """Distinct-row identity for tier-4 ambiguity (role_id, or
    company+title when the row has no role_id)."""
    rid = (r.get("role_id") or "").strip()
    if rid:
        return ("role_id", rid)
    return ("company+title", norm_company(r.get("company")),
            (r.get("title") or "").strip().lower())


# ---------------------------------------------------------------- metrics

def rate(num, den):
    if den is None or den < MIN_N:
        return {"value": None, "note": "insufficient outcome data",
                "n": num, "denominator": den}
    return {"value": round(num / den, 3), "n": num, "denominator": den,
            "note": None}


_DECISIVE_OUTCOMES = ("REJECTION", "INTERVIEW_INVITE", "ASSESSMENT",
                      "INFO_REQUEST", "OFFER")


def _app_outcome_sets(idxs, linked):
    """Distinct outcome set per application.

    Repeated messages on one application (e.g. three AUTO_ACKs) count once
    for rate purposes; raw event volumes stay in the per-outcome counters.
    """
    return [{e["outcome"] for e in linked.get(i, [])} for i in idxs]


def _app_rates(app_sets, n_sub):
    decisive = set(_DECISIVE_OUTCOMES)
    decisive_apps = sum(1 for s in app_sets if s & decisive)
    apps_with = lambda o: sum(1 for s in app_sets if o in s)
    return {
        "decisive_applications": decisive_apps,
        "ack_rate": rate(apps_with("AUTO_ACK"), n_sub),
        "rejection_rate": rate(apps_with("REJECTION"), decisive_apps),
        "invite_rate": rate(apps_with("INTERVIEW_INVITE"), decisive_apps),
        "offer_rate": rate(apps_with("OFFER"), decisive_apps),
    }


def lane_metrics(rows, linked):
    lanes = collections.defaultdict(list)
    for i, r in enumerate(rows):
        lanes[r.get("resume_lane") or "unlabeled"].append(i)
    out = {}
    for lane, idxs in sorted(lanes.items()):
        evs = [e for i in idxs for e in linked.get(i, [])]
        by_out = collections.Counter(e["outcome"] for e in evs)
        app_sets = _app_outcome_sets(idxs, linked)
        n_sub = len(idxs)
        n_linked = sum(1 for s in app_sets if s)
        decisive = sum(by_out[o] for o in _DECISIVE_OUTCOMES)
        out[lane] = {
            "submissions": n_sub,
            "submissions_with_linked_response": n_linked,
            "decisive_outcomes": decisive,
            "outcomes": dict(by_out),
            **_app_rates(app_sets, n_sub),
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
        app_sets = _app_outcome_sets(idxs, linked)
        decisive = sum(by_out[o] for o in _DECISIVE_OUTCOMES)
        out[tier] = {
            "submissions": len(idxs),
            "decisive_outcomes": decisive,
            "outcomes": dict(by_out),
            **_app_rates(app_sets, len(idxs)),
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


def _snapshot(rows, events, held, unlinked, linked):
    """Published analytics snapshot: pins this report's data boundary.

    cutoff_utc is the data cut (max parseable timestamp across included
    rows and events) — generated_at is run time, not the data boundary.
    cohort_id is a stable id over the exact inputs so a report rebuilt
    from the same inputs pins the same cohort; new evidence changes the
    inputs and therefore the cohort_id, never the old report.
    """
    stamps = []
    rows_bad_dates = 0
    for r in rows:
        d = parse_ts(r.get("date_submitted"))
        if d is None:
            rows_bad_dates += 1
        else:
            stamps.append(d)
    for e in events:
        d = parse_ts(e.get("ts"))
        if d is not None:
            stamps.append(d)
    cutoff = (max(stamps).astimezone(timezone.utc).isoformat()
              if stamps else None)
    role_ids = sorted(str(r.get("role_id") or "") for r in rows)
    ev_ids = sorted(json.dumps({
        "ts": e.get("ts") or "", "company": e.get("company") or "",
        "company_key": e.get("company_key") or "",
        "outcome": e.get("outcome") or "",
        "ref": e.get("receipt_ref") or ""}, sort_keys=True)
        for e in events)
    params = {"min_n_for_rates": MIN_N, "latency_min_n": LATENCY_MIN_N,
              "linkable_statuses": sorted(LINKABLE_STATUSES),
              "query_version": QUERY_VERSION}
    blob = json.dumps({"role_ids": role_ids, "events": ev_ids,
                       "params": params}, sort_keys=True)
    cohort_id = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    latency_excluded_untrusted = 0
    for i, r in enumerate(rows):
        if not parse_ts(r.get("date_submitted")):
            continue
        for e in linked.get(i, []):
            if e["outcome"] == "AUTO_ACK" and not e.get("ts_trustworthy"):
                latency_excluded_untrusted += 1
    return {
        "cutoff_utc": cutoff,
        "cohort_id": cohort_id,
        "query_version": QUERY_VERSION,
        "denominator_definition": DENOMINATOR_DEFINITION,
        "missingness": {
            "rows_missing_or_unparseable_date_submitted": rows_bad_dates,
            "latency_samples_excluded_untrusted_ts":
                latency_excluded_untrusted,
            "events_unlinked": len(unlinked),
            "events_held_for_review": len(held),
        },
    }


def build_report(rows, events, link_rows=None):
    """rows: submitted rows (metric denominators stay submission-based).
    link_rows: rows events may link to (defaults to rows); events landing
    on advanced (invited/waitlisted) rows count as linked but don't feed
    lane/tier denominators. Held-for-review events are neither linked nor
    unlinked and stay OUT of all rates."""
    link_src = link_rows if link_rows is not None else rows
    linked_raw, unlinked, held = link_responses(link_src, events)
    # Re-key linked events onto the submitted-row index space.
    submitted_ids = {_row_id(r): j for j, r in enumerate(rows)}
    linked = collections.defaultdict(list)
    advanced_linked = 0
    for i, evs in linked_raw.items():
        j = submitted_ids.get(_row_id(link_src[i]))
        if j is None:
            advanced_linked += len(evs)
        else:
            linked[j].extend(evs)
    lanes = lane_metrics(rows, linked)
    tiers = tier_metrics(rows, linked)
    gaps = data_gaps(rows)
    lat = ack_latency_hours(rows, linked)
    linked_n = sum(len(v) for v in linked_raw.values())
    unlinked_rate = (len(unlinked) / len(events)) if events else 0
    held_rate = (len(held) / len(events)) if events else 0
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {"submitted_rows": len(rows),
                   "linkable_rows": len(link_src),
                   "response_events": len(events),
                   "events_linked": linked_n,
                   "events_linked_to_advanced_rows": advanced_linked,
                   "events_unlinked": len(unlinked),
                   "unlinked_rate": round(unlinked_rate, 3),
                   "events_held_for_review": len(held),
                   "held_rate": round(held_rate, 3)},
        "linking_rule": ("tiered linkage: (1) deterministic role_id "
                         "match; (2) receipt/submission-ref match; "
                         "(3) posting_url / ats_job_id identity match; "
                         "(4) normalized company-name fallback "
                         "attributed to the most recent linkable ledger "
                         "row (SUBMITTED / INTERVIEW_INVITED / WAITLISTED / "
                         "ASSESSMENT) at-or-before the event ts. "
                         "Ambiguous company matches are held for review, "
                         "never silently resolved"),
        "min_n_for_rates": MIN_N,
        "snapshot": _snapshot(rows, events, held, unlinked, linked),
        "data_gaps": gaps,
        "lanes": lanes,
        "source_tiers": tiers,
        "ack_latency_hours": lat,
        "hypotheses": hypotheses(lanes),
        "unlinked_events": [
            {"ts": e["ts"], "company": e["company"],
             "outcome": e["outcome"]} for e in unlinked[:20]],
        "held_for_review": [
            {"ts": e.get("ts"), "company": e.get("company"),
             "outcome": e.get("outcome"),
             "linkage": e.get("linkage")} for e in held[:20]],
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
      f"{report['inputs']['events_linked']} linked, "
      f"{report['inputs']['events_unlinked']} unlinked "
      f"({report['inputs']['unlinked_rate']:.0%} unlinked-rate "
      f"data-quality signal). "
      f"Rates need n≥{report['min_n_for_rates']} or read "
      f"'insufficient outcome data'._")
    A("")
    snap = report.get("snapshot") or {}
    cutoff = snap.get("cutoff_utc") or "no parseable timestamps"
    A("## Snapshot (data cut)")
    A("")
    A(f"_Cutoff (data boundary): {cutoff} · cohort "
      f"`{snap.get('cohort_id')}` · query {snap.get('query_version')}. "
      f"Historical reports keep their original cut when new evidence "
      f"arrives — rebuilds from identical inputs pin the same cohort._")
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
    A("_Recommendation: the outcome listener should record each "
      "message's actual receipt date on future events; backfill runs "
      "stamped run time, which cannot support latency math._")
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
    A(f"- Ambiguous company matches held in the review queue: "
      f"{report['inputs']['events_held_for_review']} — never silently "
      f"resolved; held events are excluded from all rates.")
    A("- Legacy backfill labels normalized: interview_invited → "
      "INTERVIEW_INVITE; waitlisted → OTHER (limbo state); "
      "offer → OFFER.")
    A("- Decisive outcomes = REJECTION + INTERVIEW_INVITE + ASSESSMENT + "
      "INFO_REQUEST + OFFER. Acknowledgments are confirmatory, not "
      "decisive.")
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
    link_rows = load_linkable_rows()
    events = load_responses()
    report = build_report(rows, events, link_rows)

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
