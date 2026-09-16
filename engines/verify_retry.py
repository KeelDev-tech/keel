#!/usr/bin/env python3
"""verify_retry.py — verification-retry worker for the pending_verification pool.

Owns leads parked as `pending_verification` (posting liveness unverified).
For each candidate whose ONLY blocker is verification, re-fetch the
employer-direct posting / ATS page over HTTP and:

  verified live + accepting -> log lead_verified + gate_cleared,
                              promote to READY
  confirmed dead (404/410, job gone) -> log lead_dead,
                              move to rejected-queue.json
  ambiguous (aggregator, API error, no direct URL, needs browser)
                             -> leave parked, log nothing (no event spam)

Hard boundaries: HTTP only. Never submits applications, creates accounts,
solves captchas, or touches leads with genuine needs_input blockers
(essays, wording, travel, attestations, references).

Also reconciles one-lead-one-queue: a role_id present in both the standard
queue and the needs_input queue is kept in exactly one, chosen by its true
blocker.

Usage:
    python3 verify_retry.py            # dry run: report what it WOULD do
    python3 verify_retry.py --live     # apply queue moves + telemetry
    python3 verify_retry.py --live --limit 20
"""

import json
import os
import re
import sys
import time
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import api_direct_detect
import ats
import log_event

# Classification logic lives in genuine_pat.py (2026-09-16 restructure:
# the GENUINE_PAT v2 arm owns the classifier; this module owns the run
# machinery). Re-exported here so existing consumers keep working unchanged.
from genuine_pat import (  # noqa: E402
    VERIFY_PAT, GENUINE_PAT, APPLICANT_NOTE_PAT, CLEARED_HISTORY_PAT,
    RESOLVED_STATE_PAT, _field_text, unresolved_text, is_verify_only,
)

from keel_paths import HOME as PIPE  # noqa: E402
STD_Q = os.path.join(PIPE, "data", "queues", "standard-queue.json")
NI_Q = os.path.join(PIPE, "data", "queues", "needs_input-queue.json")
REJ_Q = os.path.join(PIPE, "data", "queues", "rejected-queue.json")


def _as_items(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("entries", d.get("items", []))
    return []


def load(p):
    if not os.path.exists(p):
        return []
    return _as_items(json.load(open(p)))


def save(p, rows):
    try:
        cur = json.load(open(p)) if os.path.exists(p) else []
    except Exception:
        cur = []
    if isinstance(cur, dict):
        key = "entries" if "entries" in cur else ("items" if "items" in cur else "entries")
        cur[key] = rows
        json.dump(cur, open(p, "w"), indent=1)
    else:
        json.dump(rows, open(p, "w"), indent=1)


def posting_url(entry):
    return (entry.get("ats_url") or entry.get("application_url") or "").strip()


DEAD_MARKERS = ("no longer available", "this job is no longer",
                 "job not found", "posting has expired",
                 "position has been filled", "no longer accepting applications",
                 "this posting is no longer")
LIVE_MARKERS = ("apply for this job", "apply now", "submit application",
                "apply to this position")
# URL shapes that are listing/board pages, not individual postings — page
# content there says nothing about the specific role's liveness.
LISTING_PAT = re.compile(r"/jobs/?(\?|$)|/jobs/role/|/search|/companies/|/board/?$",
                          re.I)


def entry_company_title(e):
    """Company/title for an entry: explicit fields first, then the role_key
    fallback ("Company | Title | ...") used by pipeline queue entries that
    carry no dedicated company/title fields. Never guesses. Trailing
    parentheticals (salary bands like "($150.4-176.9K)") are stripped from
    the title so token matchers don't choke on numeric noise."""
    company = (e.get("company") or "").strip()
    title = (e.get("title") or "").strip()
    if (not company or not title):
        parts = [p.strip() for p in (e.get("role_key") or "").split("|")]
        if len(parts) >= 2:
            company = company or parts[0]
            title = title or parts[1]
    title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
    return company, title


def check_live(url, title_hint=""):
    """Returns (verdict, detail). Verdict: live | dead | ambiguous.

    Conservative by design: a `dead` verdict only ever comes from an
    explicit signal (ATS API 404/410, dead markers on the posting page).
    A `live` verdict needs an ATS API job record or strong apply markers
    on an individual posting page. Everything else is `ambiguous`
    (stays parked, no event spam).
    """
    try:
        resolved = ats.resolve_final_url(url)
    except Exception as e:
        return "ambiguous", f"resolve failed: {str(e)[:80]}"
    candidates = [url] if resolved == url else [url, resolved]
    detected = [(u, ats.detect_ats(u)) for u in candidates]
    final_url, which = next(
        ((u, a) for u, a in detected if a != "unknown"), detected[-1])

    def dead_markers_in(html):
        return any(m in html for m in DEAD_MARKERS)

    try:
        if which == "greenhouse":
            board = job_id = None
            for u in candidates:
                board, job_id = ats.parse_greenhouse(u)
                if board and job_id:
                    break
            if not (board and job_id):
                return "ambiguous", "greenhouse URL but no board/job tokens"
            job = ats.greenhouse_job(board, job_id)
            if job.get("title"):
                return "live", f"greenhouse: {job.get('title')}"
            return "ambiguous", "greenhouse API returned no title"
        if which == "lever":
            org = pid = None
            for u in candidates:
                org, pid = ats.parse_lever(u)
                if org and pid:
                    break
            if not (org and pid):
                return "ambiguous", "lever URL but no org/posting tokens"
            p = ats.lever_posting(org, pid)
            if p.get("title"):
                return "live", f"lever: {p.get('title')}"
            return "ambiguous", "lever API returned no title"
        if which == "ashby":
            m = re.search(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", final_url)
            if not m:
                return "ambiguous", "ashby URL but no board token"
            jobs = ats.ashby_board_jobs(m.group(1))
            if not jobs:
                return "ambiguous", "ashby board empty/unreachable"
            # Board-level listing is not enough: the SPECIFIC role must be
            # present. Match on title tokens.
            hint = set(re.findall(r"[a-z0-9]+", (title_hint or "").lower()))
            hint -= {"senior", "sr", "jr", "ii", "iii", "lead", "manager",
                     "remote", "us", "the", "and", "for"}
            hint = {t for t in hint if not t.isdigit()}
            for j in jobs:
                jt = set(re.findall(r"[a-z0-9]+", (j.get("title") or "").lower()))
                if hint and jt and len(hint & jt) / len(hint) >= 0.6:
                    return "live", f"ashby: role listed as '{j.get('title')}'"
            return "ambiguous", \
                f"ashby board live ({len(jobs)} jobs) but role not matched"
        if which != "unknown" and not ats.is_aggregator(url):
            return "ambiguous", f"ATS '{which}' has no HTTP liveness check"
        # Generic fallback for non-ATS posting pages (e.g. Wellfound):
        # only individual posting pages count; board/listing pages prove nothing.
        if LISTING_PAT.search(final_url):
            return "ambiguous", "listing/board page — not the individual posting"
        req = urllib.request.Request(final_url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (404, 410):
                return "dead", f"HTTP {resp.status} on posting page"
            html = resp.read().decode("utf-8", "replace")[:200000].lower()
        if dead_markers_in(html):
            return "dead", "posting page shows removed/expired markers"
        if any(m in html for m in LIVE_MARKERS):
            return "live", "posting page renders with apply markers"
        return "ambiguous", "page fetched but liveness unclear"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "dead", f"HTTP {e.code}"
        return "ambiguous", f"HTTP {e.code}"
    except Exception as e:
        return "ambiguous", f"check failed: {str(e)[:80]}"


# ---------------------------------------------------------------------------
# URL enrichment (Track 2, edge-test 2026-09-14): keyless board-API lookup for
# URL-less pending-verification leads. HTTP only, read-only, fail-closed.
# Empirically: ~10-15% of the URL-less pool yields a live employer-direct URL
# per cycle via Greenhouse/Lever/Ashby board APIs with a tightened matcher.
# ---------------------------------------------------------------------------

ENRICH_STOP = {
    "senior", "sr", "junior", "jr", "ii", "iii", "iv", "lead", "principal",
    "staff", "associate", "assistant", "intern", "manager", "director", "vp",
    "svp", "evp", "president", "head", "coordinator", "specialist", "analyst",
    "executive", "representative", "engineer", "generalist",
    "remote", "us", "usa", "hybrid", "onsite", "on-site",
    "the", "and", "for", "of", "to", "in", "a", "an", "at", "on", "with", "&",
}


def distinctive_tokens(text):
    """Lowercase alphanumeric tokens minus generic seniority / function /
    location stopwords. These are the tokens that discriminate one role from
    another."""
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in ENRICH_STOP}


def match_title(lead_text, result_text, min_overlap=2):
    """True when >= min_overlap distinctive tokens overlap between the lead
    and the board-API result. Kills single-token fuzzy matches (e.g. Edmentum
    "Virtual Program Manager" matching 11 "Virtual Teacher" posts on the
    token "virtual" alone)."""
    a, b = distinctive_tokens(lead_text), distinctive_tokens(result_text)
    return len(a & b) >= min_overlap


def board_token_guesses(company):
    """Board-token candidates derived from the company name (lowercased full
    name with punctuation stripped, plus first word). Fail-closed: guesses
    that 404 simply miss."""
    toks = re.findall(r"[a-z0-9]+", (company or "").lower())
    guesses = []
    for g in ("".join(toks), toks[0] if toks else ""):
        if g and g not in guesses:
            guesses.append(g)
    return guesses


def _gh_list_jobs(token):
    """Public Greenhouse board listing. Raises on HTTP error (404 -> miss)."""
    d = ats._get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return d.get("jobs", [])


def _lever_list_jobs(token):
    """Public Lever postings list. Raises on HTTP error."""
    d = ats._get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    return d if isinstance(d, list) else []


def enrich_url(entry):
    """Keyless board-API enrichment for a URL-less pending-verification entry.

    Tries Greenhouse, Lever, then Ashby board APIs with company-name-derived
    board tokens, matching the entry's company+title against each job.

    Entry shape: pipeline queue entries carry company/title in `role_key`
    ("Company | Title | ...") when the `company`/`title` fields are absent —
    parse the fallback there, never guess.

    Match rule (fail-closed): a job is eligible only when total distinctive
    token overlap >= 2 AND title-to-title token overlap >= 2. Among eligible
    jobs the best (highest total, then highest title overlap) wins; an exact
    tie returns None rather than risking a wrong-posting URL.

    Returns an employer-direct posting URL, or None. NEVER mutates `entry`.
    Fail-closed: any network/API failure -> None, entry left exactly as-is.
    """
    company = (entry_company_title(entry))[0]
    title = entry_company_title(entry)[1]
    if not company or not title:
        return None
    lead_text = f"{company} {title}"
    lead_sig = distinctive_tokens(lead_text)
    title_sig = distinctive_tokens(title)
    company_sig = distinctive_tokens(company)

    best_key = None
    best_url = None
    tie = False

    def consider(job_title, job_company, url):
        nonlocal best_key, best_url, tie
        if not url:
            return
        job_sig = distinctive_tokens(f"{job_title} {job_company}")
        total = len(lead_sig & job_sig)
        t_ov = len(title_sig & distinctive_tokens(job_title or ""))
        if total < 2 or t_ov < 2:
            return
        key = (total, t_ov)
        if best_key is None or key > best_key:
            best_key, best_url, tie = key, url, False
        elif key == best_key:
            tie = True  # ambiguous: two equally-good jobs -> no pick

    def gh_url(token, j):
        return j.get("absolute_url") or \
            f"https://job-boards.greenhouse.io/{token}/jobs/{j.get('id')}"

    try:
        for token in board_token_guesses(company):
            try:  # Greenhouse
                for j in _gh_list_jobs(token):
                    co = j.get("company_name") or ""
                    # CareerPuck white-label guard: board token returning other
                    # companies' jobs must not match (e.g. 'coursera' token
                    # returning Udemy listings).
                    if co and company_sig and not (
                            company_sig & distinctive_tokens(co)):
                        continue
                    consider(j.get("title") or "", co, gh_url(token, j))
            except Exception:
                pass
            try:  # Lever
                for j in _lever_list_jobs(token):
                    jt = j.get("text") or ""
                    consider(jt, "", j.get("hostedUrl") or
                             f"https://jobs.lever.co/{token}/{j.get('id')}")
            except Exception:
                pass
            try:  # Ashby (posting API confirms isListed; same match rule)
                for j in ats.ashby_board_jobs(token):
                    consider(j.get("title") or "", "",
                             j.get("job_url") or
                             f"https://jobs.ashbyhq.com/{token}/{j.get('id')}")
            except Exception:
                pass
    except Exception:
        return None
    if tie:
        return None
    return best_url


def main():
    live = "--live" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    std, ni, rej = load(STD_Q), load(NI_Q), load(REJ_Q)
    std_by_id = {e.get("role_id"): e for e in std}
    ni_by_id = {e.get("role_id"): e for e in ni}

    # ---- one-lead-one-queue reconcile ----
    dupes = sorted(set(std_by_id) & set(ni_by_id) - {""})
    reconcile = []
    for rid in dupes:
        keep = "needs_input" if not is_verify_only(ni_by_id[rid]) else "standard"
        reconcile.append((rid, keep))

    # ---- verification candidates ----
    # Standard queue: every PARKED-PENDING-VERIFICATION lead gets a liveness
    # check; promotion to READY only for verification-ONLY leads (is_verify_only
    # decides at apply time). Needs-input queue: verification-only entries.
    cands = []
    for e in std:
        if (e.get("status") or "").upper() == "PARKED-PENDING-VERIFICATION":
            cands.append(("standard", e))
    for e in ni:
        if is_verify_only(e):
            cands.append(("needs_input", e))
    if limit:
        cands = cands[:limit]

    print(f"candidates: {len(cands)} | queue dupes to reconcile: {len(dupes)}")
    results = {"live": [], "dead": [], "ambiguous": [], "no_url": []}
    enriched_urls = {}  # role_id -> enriched URL (persisted only on LIVE)

    for i, (qname, e) in enumerate(cands):
        rid = e.get("role_id")
        url = posting_url(e)
        _co, entry_title = entry_company_title(e)
        if not url:
            # Track 2 (2026-09-14): board-API enrichment — keyless HTTP,
            # fail-closed. An enriched URL must pass the live check before
            # the entry is treated as URL-bearing.
            enr = enrich_url(e)
            if enr:
                verdict_e, detail_e = check_live(enr, entry_title)
                if verdict_e == "live":
                    print(f"  [{i+1}/{len(cands)}] {rid}: ENRICHED {enr} "
                          f"LIVE — {detail_e}")
                    url = enr
                    enriched_urls[rid] = enr
                else:
                    results["no_url"].append(rid)
                    print(f"  [{i+1}/{len(cands)}] {rid}: enrichment found "
                          f"{enr} but {verdict_e} ({detail_e}) — stays parked")
                    continue
            else:
                results["no_url"].append(rid)
                print(f"  [{i+1}/{len(cands)}] {rid}: NO URL — stays parked")
                continue
        verdict, detail = check_live(url, entry_title)
        results[verdict if verdict in results else "ambiguous"].append(rid)
        print(f"  [{i+1}/{len(cands)}] {rid}: {verdict.upper()} — {detail}")
        if i < len(cands) - 1:
            time.sleep(1.5)

    if not live:
        print("\nDRY RUN — no queues or telemetry touched. "
              "Re-run with --live to apply.")
        if reconcile:
            print("Would reconcile (keep in one queue):")
            for rid, keep in reconcile:
                print(f"  {rid} -> keep in {keep}")
        return

    # ---- apply ----
    # Liveness and promotion: for standard-queue PARKED-PENDING-VERIFICATION
    # leads the status itself is the authoritative blocker, so a LIVE verdict
    # promotes to READY (the pre-browser re-verify gate + brief builder handle
    # any remaining JD checks per pipeline convention). Needs-input entries
    # promote only when verification is their SOLE blocker.
    #
    # Enriched URLs that passed the live check are persisted on the entry
    # (ats_url + queue note) so the promotion flow treats the lead as
    # URL-bearing downstream. Enrichment never fabricates: only LIVE URLs.
    for rid, eu in enriched_urls.items():
        if rid in results["live"]:
            e = std_by_id.get(rid) or ni_by_id.get(rid)
            if e is not None and not posting_url(e):
                e["ats_url"] = eu
                e["queue_notes"] = (
                    (e.get("queue_notes") or "")
                    + f" | verify-retry: posting URL enriched via board API"
                      f" ({eu}); confirmed live.").strip(" |")
    moved = dead_n = 0
    for qname, e in cands:
        rid = e.get("role_id")
        if rid in results["live"]:
            log_event.log("lead_verified", role_id=rid,
                          company=e.get("company", ""),
                          ats=ats.detect_ats(posting_url(e)),
                          source="verify-retry",
                          details={"verified": "posting live via ATS API"})
            log_event.log("gate_cleared", role_id=rid,
                          company=e.get("company", ""),
                          source="verify-retry",
                          details={"gate": "pending_verification"})
            promote = (qname == "standard") or is_verify_only(e)
            if promote:
                # Flag API-direct candidacy at promotion time so the apply
                # loop can route the lead to the fast lane without a browser
                # run. Fail-closed: any detection error -> False.
                try:
                    e["api_direct_candidate"] = \
                        api_direct_detect.is_api_direct_candidate(
                            posting_url(e), ats=e.get("ats"))
                except Exception:
                    e["api_direct_candidate"] = False
                if qname == "needs_input":
                    ni = [x for x in ni if x.get("role_id") != rid]
                    e["status"] = "READY"
                    e["queue_notes"] = (
                        (e.get("queue_notes") or "")
                        + " | verify-retry: posting confirmed live; promoted READY.").strip(" |")
                    std.append(e)
                else:
                    std_by_id[rid]["status"] = "READY"
                    std_by_id[rid]["status_reason"] = \
                        "verify-retry: posting confirmed live via ATS API; promoted READY."
                moved += 1
            else:
                for x in ni:
                    if x.get("role_id") == rid:
                        x["unresolved"] = [u for u in (x.get("unresolved") or [])
                                           if not VERIFY_PAT.search(u)] or x.get("unresolved")
        elif rid in results["dead"]:
            log_event.log("lead_dead", role_id=rid,
                          company=e.get("company", ""),
                          source="verify-retry",
                          details={"reason": "verify-retry: posting confirmed dead (ATS 404/410)"})
            if qname == "needs_input":
                ni = [x for x in ni if x.get("role_id") != rid]
            else:
                std = [x for x in std if x.get("role_id") != rid]
            rej.append({"role_id": rid,
                        "role_key": e.get("role_key") or e.get("company"),
                        "reason": "verify-retry: posting confirmed dead",
                        "worker": "verify-retry"})
            dead_n += 1

    for rid, keep in reconcile:
        if keep == "needs_input":
            std = [x for x in std if x.get("role_id") != rid]
        else:
            ni = [x for x in ni if x.get("role_id") != rid]

    save(STD_Q, std)
    save(NI_Q, ni)
    save(REJ_Q, rej)
    print(f"\napplied: promoted={moved} dead={dead_n} "
          f"reconciled={len(reconcile)} left_parked="
          f"{len(results['ambiguous']) + len(results['no_url'])}")


if __name__ == "__main__":
    main()
