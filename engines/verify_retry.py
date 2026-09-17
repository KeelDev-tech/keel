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
import fcntl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

from keel_paths import HOME as PIPE, DATA  # noqa: E402
STD_Q = os.path.join(PIPE, "data", "queues", "standard-queue.json")
NI_Q = os.path.join(PIPE, "data", "queues", "needs_input-queue.json")
REJ_Q = os.path.join(PIPE, "data", "queues", "rejected-queue.json")

PDT = ZoneInfo("America/Los_Angeles")
# Leads that stayed parked as no_url/ambiguous are not re-scanned until
# this cooldown elapses — stops the cadence from burning its budget
# re-checking the same parked leads every run.
VERIFY_COOLDOWN_H = 24
# Enrichment carries its OWN 24h cooldown, independent of the
# verification-cycle cooldown (last_verify_attempt).
ENRICH_COOLDOWN_H = 24
# Pool cursor: consecutive runs walk the whole pending-verification pool
# instead of re-scanning from the top of the file.
CURSOR_PATH = os.path.join(DATA, "hidden_files", "verify_retry_cursor.json")
# --live run singleton (flock-guarded) + run records (verify_cron convention).
_RUN_LOCK_PATH = os.path.join(DATA, "hidden_files", "verify_run.lock")
_RUNS_LOG = os.path.join(DATA, "hidden_files", "verify_cron_runs.jsonl")


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
# content there says nothing about the specific role's liveness. The
# /companies/ branch exempts the individual-posting shape
# /companies/{slug}/jobs/{id} (a bare /companies/{slug} or
# /companies/{slug}/jobs listing page still matches).
LISTING_PAT = re.compile(r"/jobs/?(\?|$)|/jobs/role/|/search|/companies/(?![^/?#]+/jobs/)|/board/?$",
                          re.I)


# ---------------------------------------------------------------------------
# Verification-engine HTTP optimization: skip provably-dead resolve GETs +
# ordered gh_jid board guesses.
# ---------------------------------------------------------------------------
_EXCLUDED_BOARD_LABELS = ("jobs", "careers", "apply")


def _host_labels(url):
    """DNS labels of the URL host, minus a leading www. Never raises."""
    try:
        m = re.search(
            r"https?://(?:www\.)?([a-z0-9][a-z0-9.-]*)(?::\d+)?(?:[/?#]|$)",
            url or "", re.I)
        if not m:
            return []
        return m.group(1).lower().split(".")
    except Exception:
        return []


def _resolve_needed(url):
    """True when redirect-resolution can still change the verdict.

    The liveness branches take the FIRST parseable candidate, so when the
    original URL already yields board/job tokens — or a gh_jid board guess
    (the fallback breaks at the first candidate with a non-excluded host
    label) — the resolve GET is provably dead weight. Skipping it saves one
    request per lead and kills the 15s resolve-timeout tail."""
    which = ats.detect_ats(url)
    if which == "greenhouse":
        b, j = ats.parse_greenhouse(url)
        if b and j:
            return False
        m = re.search(r"[?&]gh_jid=(\d+)", url)
        labels = _host_labels(url)
        if m and labels and labels[0] not in _EXCLUDED_BOARD_LABELS:
            return False
        return True
    if which == "lever":
        o, p = ats.parse_lever(url)
        return not (o and p)
    if which == "ashby":
        return not re.search(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", url,
                             re.I)
    return True


def _gh_jid_guesses(candidates):
    """Ordered (board, job_id) guesses for gh_jid-carrying URLs.

    First pass: first non-excluded host label per candidate, candidate order
    preserved, deduped. Second pass: for excluded first labels
    (jobs./careers./apply.) try the next DNS label (e.g. jobs.elastic.co
    -> board 'elastic'). Every guess still fail-closes on board-API 404
    (never an invented verdict)."""
    first_pass, second_pass, seen = [], [], set()
    for u in candidates:
        m = re.search(r"[?&]gh_jid=(\d+)", u)
        if not m:
            continue
        labels = _host_labels(u)
        if not labels:
            continue
        if labels[0] not in _EXCLUDED_BOARD_LABELS:
            g, second = (labels[0], m.group(1)), False
        elif len(labels) > 1:
            g, second = (labels[1], m.group(1)), True
        else:
            continue
        if g in seen:
            continue
        seen.add(g)
        (second_pass if second else first_pass).append(g)
    return first_pass + second_pass


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
    explicit signal (ATS API 404/410 on the posting page, dead markers on
    the posting page). A `live` verdict needs an ATS API job record or
    strong apply markers on an individual posting page. Everything else is
    `ambiguous` (stays parked, no event spam).

    Fail-closed on board/posting APIs: a 404/410 from an ATS *API* is NOT
    posting-death evidence (the board API is decoupled from page
    rendering) — it falls through to ambiguous. With multiple board
    guesses, a 404 just tries the next guess.
    """
    try:
        # Skip the resolve GET when the original URL already determines the
        # branch (_resolve_needed).
        resolved = url if not _resolve_needed(url) \
            else ats.resolve_final_url(url)
    except Exception as e:
        return "ambiguous", f"resolve failed: {str(e)[:80]}"
    candidates = [url] if resolved == url else [url, resolved]
    detected = [(u, ats.detect_ats(u)) for u in candidates]
    final_url, which = next(
        ((u, a) for u, a in detected if a != "unknown"), detected[-1])
    if which == "unknown" and any(
            re.search(r"[?&]gh_jid=(\d+)", u) for u in candidates):
        which = "greenhouse"  # employer career URL carrying a Greenhouse
        # job id — the branch resolves board/job via gh_jid guesses.

    def dead_markers_in(html):
        return any(m in html for m in DEAD_MARKERS)

    try:
        if which == "greenhouse":
            guesses = []
            for u in candidates:
                board, job_id = ats.parse_greenhouse(u)
                if board and job_id:
                    guesses.append((board, job_id))
                    break
            if not guesses:
                # Employer career URLs carry the Greenhouse job id as gh_jid
                # (e.g. stripe.com/jobs/search?gh_jid=8052814). Ordered board
                # guesses via _gh_jid_guesses. A wrong guess 404s on the
                # board API and fails closed to ambiguous below — never an
                # invented live verdict.
                guesses = _gh_jid_guesses(candidates)
            if not guesses:
                return "ambiguous", "greenhouse URL but no board/job tokens"
            api_404 = None
            for board, job_id in guesses:
                try:
                    job = ats.greenhouse_job(board, job_id)
                except urllib.error.HTTPError as e:
                    if e.code in (404, 410):
                        api_404 = e.code
                        continue
                    raise
                if job.get("title"):
                    return "live", f"greenhouse: {job.get('title')}"
                return "ambiguous", "greenhouse API returned no title"
            return "ambiguous", (
                f"greenhouse board API HTTP {api_404} — "
                "not death evidence")
        if which == "lever":
            org = pid = None
            for u in candidates:
                org, pid = ats.parse_lever(u)
                if org and pid:
                    break
            if not (org and pid):
                return "ambiguous", "lever URL but no org/posting tokens"
            try:
                p = ats.lever_posting(org, pid)
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return "ambiguous", (
                        f"lever posting API HTTP {e.code} — "
                        "not death evidence")
                raise
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
# Promotion-time posting eligibility screen. Verify_retry already fetches the
# posting at verify time; this screen runs once more, on the live text, for
# would-be READY promotions -- burning one HTTP fetch instead of a browser
# launch. Fail-open throughout: a failed fetch never blocks a promotion
# (the claim-time re-verify gate and the browser Step-1 check remain as
# backstops).
# ---------------------------------------------------------------------------

def _html_to_text(html):
    """Crude HTML -> text for the eligibility screen."""
    t = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:50000]


def fetch_posting_text(url):
    """Best-effort posting text. Mirrors check_live's ATS detection;
    returns text or "". Never raises."""
    if not url:
        return ""
    try:
        resolved = ats.resolve_final_url(url)
    except Exception:
        return ""
    candidates = [url] if resolved == url else [url, resolved]
    detected = [(u, ats.detect_ats(u)) for u in candidates]
    final_url, which = next(
        ((u, a) for u, a in detected if a != "unknown"), detected[-1])
    if which == "unknown" and any(
            re.search(r"[?&]gh_jid=(\d+)", u) for u in candidates):
        which = "greenhouse"
    try:
        if which == "greenhouse":
            board = job_id = None
            for u in candidates:
                board, job_id = ats.parse_greenhouse(u)
                if board and job_id:
                    break
            if not (board and job_id):
                for board, job_id in _gh_jid_guesses(candidates):
                    break
            if board and job_id:
                try:
                    job = ats.greenhouse_job(board, job_id)
                except Exception:
                    return ""
                return _html_to_text(
                    job.get("content") or job.get("description") or "")
            return ""
        if which == "lever":
            org = pid = None
            for u in candidates:
                org, pid = ats.parse_lever(u)
                if org and pid:
                    break
            if org and pid:
                try:
                    p = ats.lever_posting(org, pid)
                except Exception:
                    return ""
                parts = [p.get("description") or ""]
                for lst in (p.get("lists") or []):
                    if isinstance(lst, dict):
                        parts.append(lst.get("text") or "")
                        parts.extend(x for x in (lst.get("content") or [])
                                     if isinstance(x, str))
                return _html_to_text(" ".join(parts))
            return ""
        req = urllib.request.Request(final_url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (404, 410):
                return ""
            html = resp.read().decode("utf-8", "replace")[:200000]
        return _html_to_text(html)
    except Exception:
        return ""


def screen_promotion_posting(entry, url):
    """Run prescreen.posting_eligibility_screen over the live posting text.

    Returns [reasons] (empty = clean). Fail-open: import/fetch/parse
    trouble -> []. Called in the --live apply section for would-be
    promote actions; a hit parks the lead (gate=eligibility) instead of
    promoting to READY."""
    try:
        from prescreen import posting_eligibility_screen
    except Exception:
        return []
    text = fetch_posting_text(url)
    if not text:
        return []
    try:
        return posting_eligibility_screen(text)
    except Exception:
        return []


def screen_promotion_form(entry, url):
    """Run the pre-promotion form screen (prescreen.screen_entry_prepromotion).

    Verify_retry promotes verified-live leads to READY; this probes the live
    application form over HTTP and runs the full prescreen
    commitment/essay/attestation/question checks BEFORE any READY
    promotion, so blocked leads route straight to needs_input (the tray)
    without ever promoting or burning a packet build.

    Returns [reasons] (empty = clean). Fail-open: import/probe/parse
    trouble -> []. A hit parks the lead to needs_input
    (gate=needs_input, screen=prepromotion) instead of promoting."""
    try:
        from prescreen import screen_entry_prepromotion
    except Exception:
        return []
    try:
        res = screen_entry_prepromotion(entry, url=url)
    except Exception:
        return []
    if not isinstance(res, dict):
        return []
    if res.get("verdict") == "PARK":
        return res.get("reasons") or ["pre-promotion form screen parked (no reason text)"]
    return []


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


def _empty_probe_info():
    """Probe-info shape for early exits (no board API touched)."""
    return {"http_429": False, "tokens_tried": 0, "per_board": {}}


def _probe_info(saw_429, candidates, probe_trace):
    """Assemble the probe-info dict for _enrich_with_meta's 5th return."""
    return {"http_429": bool(saw_429),
            "tokens_tried": len(candidates or []),
            "per_board": {b: dict(o) for b, o in (probe_trace or {}).items()}}


def _enrich_with_meta(entry):
    """Keyless board-API enrichment with match metadata.

    Tries Greenhouse, Lever, then Ashby board APIs with company-name-derived
    board tokens, matching the entry's company+title against each job. When
    the entry carries a recorded `ats_board` ref ("platform:token", observed
    at discovery time), that single board is queried directly and guessing
    is skipped — an observation beats a guess. An invalid/absent ref falls
    back to guessing.

    Entry shape: pipeline queue entries carry company/title in `role_key`
    ("Company | Title | ...") when the `company`/`title` fields are absent —
    parse the fallback there, never guess.

    Match rule (fail-closed): a job is eligible only when total distinctive
    token overlap >= 2 AND title-to-title token overlap >= 2. Among eligible
    jobs the best (highest total, then highest title overlap) wins; an exact
    tie selects nothing rather than risking a wrong-posting URL.

    Returns (url, api_source, token_overlap, fail_reason, probe_info):
      - url: employer-direct posting URL, or None.
      - api_source: "greenhouse" | "lever" | "ashby" | None.
      - token_overlap: {"total": n, "title": n} | None.
      - fail_reason: None on success; "no_match" | "no_board" | "api_error"
        otherwise. "no_match" covers: no eligible job, an exact tie, or
        missing company/title. "no_board" means every board API returned
        404/miss for every guessed token. "api_error" is reserved for
        genuine non-404 failures (network, timeout, 5xx, 429).
      - probe_info: {"http_429": bool, "tokens_tried": int,
        "per_board": {board: {outcome_class: count}}}.

    NEVER mutates `entry`. Fail-closed: any network/API failure -> None, entry
    left exactly as-is.
    """
    company, title = entry_company_title(entry)
    if not company or not title:
        return None, None, None, "no_match", _empty_probe_info()
    lead_text = f"{company} {title}"
    lead_sig = distinctive_tokens(lead_text)
    title_sig = distinctive_tokens(title)
    company_sig = distinctive_tokens(company)

    best_key = None
    best_url = None
    best_source = None
    tie = False
    saw_success = False
    saw_hard_error = False  # any non-404 failure (network, timeout, 5xx,
    # 429). Pure 404-on-guessed-token misses are "no_board", not errors.
    saw_429 = False  # distinguished so rate-limit pressure is measurable
    probe_trace = {}  # board -> {outcome_class: count}

    def note_board_outcome(board, outcome):
        probe_trace.setdefault(board, {}).setdefault(outcome, 0)
        probe_trace[board][outcome] += 1

    def note_api_error(board, exc):
        nonlocal saw_hard_error, saw_429
        code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        if code == 404:
            note_board_outcome(board, "http_404")
            return  # token miss — the board simply doesn't exist
        saw_hard_error = True
        if code == 429:
            saw_429 = True
            note_board_outcome(board, "http_429")
        elif isinstance(code, int) and 500 <= code < 600:
            note_board_outcome(board, "http_5xx")
        elif isinstance(code, int):
            note_board_outcome(board, "http_4xx")
        else:
            note_board_outcome(board, "net_error")

    def consider(api_source, job_title, job_company, url):
        nonlocal best_key, best_url, best_source, tie
        if not url:
            return
        job_sig = distinctive_tokens(f"{job_title} {job_company}")
        total = len(lead_sig & job_sig)
        t_ov = len(title_sig & distinctive_tokens(job_title or ""))
        if total < 2 or t_ov < 2:
            return
        key = (total, t_ov)
        if best_key is None or key > best_key:
            best_key, best_url, best_source, tie = key, url, api_source, False
        elif key == best_key:
            tie = True  # ambiguous: two equally-good jobs -> no pick

    def gh_url(token, j):
        return j.get("absolute_url") or \
            f"https://job-boards.greenhouse.io/{token}/jobs/{j.get('id')}"

    # A recorded board ref (a discovery-time observation in the entry's
    # `ats_board` field) replaces company-name token guessing. An
    # invalid/absent ref falls back to guessing. The same fail-closed match
    # rule applies either way; a recorded token that 404s reports
    # "no_board" exactly like a guessed-token miss.
    rec_platform, rec_token = ats.parse_board_ref(entry.get("ats_board"))
    if rec_platform:
        candidates = [(rec_platform, rec_token)]
    else:
        candidates = [(src, tok) for tok in board_token_guesses(company)
                      for src in ("greenhouse", "lever", "ashby")]

    try:
        for src, token in candidates:
            if src == "greenhouse":
                try:
                    gh_jobs = _gh_list_jobs(token)
                    saw_success = True
                    note_board_outcome("greenhouse", "jobs_returned")
                    for j in gh_jobs:
                        co = j.get("company_name") or ""
                        # CareerPuck white-label guard: board token returning
                        # other companies' jobs must not match.
                        if co and company_sig and not (
                                company_sig & distinctive_tokens(co)):
                            continue
                        consider("greenhouse", j.get("title") or "", co,
                                 gh_url(token, j))
                except Exception as exc:
                    note_api_error("greenhouse", exc)
            elif src == "lever":
                try:
                    lv_jobs = _lever_list_jobs(token)
                    saw_success = True
                    note_board_outcome("lever", "jobs_returned")
                    for j in lv_jobs:
                        jt = j.get("text") or ""
                        consider("lever", jt, "", j.get("hostedUrl") or
                                 f"https://jobs.lever.co/{token}/{j.get('id')}")
                except Exception as exc:
                    note_api_error("lever", exc)
            elif src == "ashby":
                try:  # posting API confirms isListed; same match rule
                    ab_jobs = ats.ashby_board_jobs(token)
                    saw_success = True
                    note_board_outcome("ashby", "jobs_returned")
                    for j in ab_jobs:
                        consider("ashby", j.get("title") or "", "",
                                 j.get("job_url") or
                                 f"https://jobs.ashbyhq.com/{token}/{j.get('id')}")
                except Exception as exc:
                    note_api_error("ashby", exc)
    except Exception:
        return None, None, None, "api_error", _probe_info(
            saw_429, candidates, probe_trace)
    if tie:
        return None, None, None, "no_match", _probe_info(
            saw_429, candidates, probe_trace)
    if best_url is None:
        if saw_success:
            return None, None, None, "no_match", _probe_info(
                saw_429, candidates, probe_trace)
        # "api_error" was masking no-board misses. Report them honestly:
        # all tokens 404'd -> "no_board"; a genuine non-404 failure
        # (network, timeout, 5xx, 429) -> "api_error".
        return None, None, None, \
            "api_error" if saw_hard_error else "no_board", _probe_info(
                saw_429, candidates, probe_trace)
    return best_url, best_source, {"total": best_key[0],
                                   "title": best_key[1]}, None, _probe_info(
        saw_429, candidates, probe_trace)


def enrich_url(entry):
    """Thin wrapper kept for callers that only need the URL.

    See _enrich_with_meta for the full match rule. NEVER mutates `entry`.
    Fail-closed: any network/API failure -> None, entry left exactly as-is.
    """
    url, _source, _overlap, _reason, _probe = _enrich_with_meta(entry)
    return url


# ---------------------------------------------------------------------------
# Cooldown / pool cursor (retry pacing): parked leads are not re-scanned
# until the cooldown elapses; consecutive runs rotate over the pool.
# ---------------------------------------------------------------------------

def in_cooldown(entry, now=None, hours=None):
    """True when a parked lead was scanned too recently.

    Covers the whole last_verify_attempt stamp union: no_url/ambiguous and
    verified-live-but-not-promoted leads. Fail-open by design: a missing or
    unparseable stamp means "not in cooldown" — a lead is never silently
    starved by a bad stamp, only paced by a valid one."""
    if hours is None:
        hours = VERIFY_COOLDOWN_H
    ts = entry.get("last_verify_attempt")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PDT)
    now = now or datetime.now(PDT)
    return (now - dt).total_seconds() < hours * 3600


def enrich_in_cooldown(entry, now=None, hours=ENRICH_COOLDOWN_H):
    """True when this lead's board-API enrichment was attempted too recently.

    The enrichment loop gets its own per-role_id cooldown
    (last_enrich_attempt), independent of the verification-cycle cooldown.
    Fail-open by design: a missing or unparseable stamp means "not in
    cooldown" — a lead is never silently starved of enrichment by a bad
    stamp, only paced by a valid one."""
    ts = entry.get("last_enrich_attempt")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PDT)
    now = now or datetime.now(PDT)
    return (now - dt).total_seconds() < hours * 3600


def load_cursor():
    try:
        return int(json.load(open(CURSOR_PATH)).get("cursor", 0) or 0)
    except (FileNotFoundError, ValueError, AttributeError):
        return 0


def save_cursor(n):
    os.makedirs(os.path.dirname(CURSOR_PATH), exist_ok=True)
    json.dump({"cursor": n,
               "updated": datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")},
              open(CURSOR_PATH, "w"))


def window_candidates(cands, limit, cursor):
    """Rotate the ordered pool by cursor, take the limit window.

    Returns (window, new_cursor). The cursor advances over the full pool
    (not just the scanned subset) so consecutive runs cover everything."""
    n = len(cands)
    if n == 0:
        return [], 0
    start = cursor % n
    rotated = cands[start:] + cands[:start]
    window = rotated[:limit] if limit else rotated
    return window, (start + len(window)) % n


# ---------------------------------------------------------------------------
# Stale-park guard: withhold promotion when a park-family gate event is
# newer than the queue's own status_updated stamp (the queue and the event
# log diverge — the park signal wins, never the stale queue state).
# ---------------------------------------------------------------------------
_stale_park_cache = None
_stale_park_mtime = None


def _stale_park_blocked_map():
    """Latest park-family gate_blocked timestamp per role_id."""
    global _stale_park_cache, _stale_park_mtime
    path = log_event.EVENTS
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _stale_park_cache is not None and _stale_park_mtime == mtime:
        return _stale_park_cache
    family = getattr(log_event, "PARK_FAMILY_GATES",
                     frozenset({"needs_input", "pending_verification"}))
    blocked = {}
    try:
        with open(path) as f:
            for line in f:
                if "gate_blocked" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") != "gate_blocked":
                    continue
                if (ev.get("details") or {}).get("gate") not in family:
                    continue
                rid = ev.get("role_id")
                ts = ev.get("ts")
                if rid and ts and (rid not in blocked or ts > blocked[rid]):
                    blocked[rid] = ts
    except (FileNotFoundError, OSError):
        return _stale_park_cache or {}
    _stale_park_cache = blocked
    _stale_park_mtime = mtime
    return blocked


def _parse_queue_ts(raw):
    """Parse a queue status_updated stamp to an aware datetime.

    Handles ISO-8601 ('2026-09-16T18:05:59Z', '2026-09-16T13:37:31-07:00')
    AND the space-separated writer formats ('2026-09-15 20:58 PDT',
    '2026-09-16 03:35 UTC'). None when unparseable (fail-open: the guard
    does not fire on a bad stamp)."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    tz = PDT
    for label, zone in (("PDT", PDT), ("PST", PDT), ("UTC", timezone.utc)):
        if text.endswith(" " + label):
            text = text[: -len(label)].strip()
            tz = zone
            break
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return dt.replace(tzinfo=tz)


def stale_park_guard(entry):
    """True when the freshness guard must withhold promotion of entry."""
    bts = _stale_park_blocked_map().get(entry.get("role_id"))
    if not bts:
        return False
    try:
        b_dt = datetime.fromisoformat(bts)
    except (ValueError, TypeError):
        return False
    if b_dt.tzinfo is None:
        b_dt = b_dt.replace(tzinfo=timezone.utc)
    su_dt = _parse_queue_ts(entry.get("status_updated"))
    # Missing/older queue stamp with a newer park signal = divergence.
    return su_dt is None or b_dt > su_dt


# ---------------------------------------------------------------------------
# --live run singleton + run records. A flock-guarded file: the probe is
# non-blocking, the holder keeps the fd open for the whole run, and the
# kernel releases the flock if the holder dies — no stale-singleton state
# possible.
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(PDT).isoformat()


def _pid_alive(pid):
    """True if pid refers to a live process. Fail-open: an unreadable or
    invalid pid counts as dead."""
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_lock_metadata(path):
    """Best-effort read of the singleton lock file's JSON metadata."""
    try:
        with open(path) as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _acquire_run_singleton(lock_path=None):
    """Non-blocking flock probe. Returns the open fd on success, None when
    another --live run holds it. lock_path override is for tests.

    The file is opened with "a+" so NO truncation happens before the flock
    is acquired — only the winner truncates and writes its metadata. A lock
    file whose recorded pid is DEAD is treated as free (the kernel has
    already released that holder's flock); the flock itself remains the
    authority — if a live process holds the fd, the non-blocking flock
    still refuses and we return None."""
    path = lock_path or _RUN_LOCK_PATH
    prior = _read_lock_metadata(path)
    prior_pid = prior.get("pid")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "a+")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            f.close()
        except OSError:
            pass
        if prior_pid is not None and not _pid_alive(prior_pid):
            print(f"verify_retry: flock held but recorded holder pid "
                  f"{prior_pid} is dead (leaked fd) — skipping, lock "
                  f"cannot be broken")
        return None
    try:
        f.seek(0)
        f.truncate()
        if prior_pid is not None and not _pid_alive(prior_pid):
            print(f"verify_retry: previous holder pid {prior_pid} dead — "
                  f"taking over stale singleton")
        json.dump({"pid": os.getpid(), "started": _now_iso(),
                   "argv": sys.argv[1:]}, f)
        f.flush()
    except OSError:
        pass
    return f


def _release_run_singleton(f):
    """Release the singleton. Truncates the lock file BEFORE unlocking:
    stale pid metadata used to linger after a holder died or exited,
    misleading diagnostics into reading the lock as held."""
    if f is None:
        return
    try:
        f.seek(0)
        f.truncate()
        f.flush()
    except OSError:
        pass
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        f.close()
    except OSError:
        pass


def _record_run(record):
    """Append to verify_cron_runs.jsonl (same convention as verify_cron.py).
    Best-effort: never break the run on a logging failure."""
    try:
        os.makedirs(os.path.dirname(_RUNS_LOG), exist_ok=True)
        with open(_RUNS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Async-parity helpers (shared with verify_retry_async.py).
# ---------------------------------------------------------------------------

def scan_lead_sync(qname, entry, enrich_events):
    """One sequential scan iteration (the production verdict path).

    enrich-if-no-URL + check_live, with the same in-memory entry mutations
    (last_enrich_attempt stamp at attempt time, ats_url/queue_notes persist
    on enriched-live) and the same enrich_events telemetry tuples. Printing,
    the inter-lead sleep, AND the results tally stay with the caller so the
    --async path can share the downstream — this function never mutates a
    results dict.

    Returns (rid, verdict, detail, enriched_url_or_None). Verdict in
    {live, dead, ambiguous, no_url}. The no_url `detail` is the exact branch
    message (the caller prints it); enriched-live returns the raw check
    detail (the caller prints the ENRICHED line + the shared line).
    """
    rid = entry.get("role_id")
    url = posting_url(entry)
    _co, entry_title = entry_company_title(entry)
    if not url:
        # Board-API enrichment — keyless HTTP, fail-closed. An enriched URL
        # must pass the live check before the entry is treated as
        # URL-bearing. Enrichment carries its OWN 24h cooldown, independent
        # of the verification-cycle cooldown (last_verify_attempt, stamped
        # at the end of the --live apply). last_enrich_attempt is stamped
        # HERE, at attempt time before any HTTP, so a second run — or a
        # second pass over the same pool before the apply section saves —
        # can never re-burn the API calls.
        if enrich_in_cooldown(entry):
            return (rid, "no_url",
                    f"enrichment in cooldown "
                    f"(attempted <{ENRICH_COOLDOWN_H}h ago) — "
                    f"stays parked, no API calls", None)
        entry["last_enrich_attempt"] = _now_iso()
        enr, enr_source, enr_overlap, enr_fail, enr_probe = \
            _enrich_with_meta(entry)
        if enr:
            verdict_e, detail_e = check_live(enr, entry_title)
            if verdict_e == "live":
                # Persist the enriched URL onto the in-memory entry NOW
                # (not in the --live apply): downstream decisions must see
                # the same entry state. In-memory only — save() is
                # --live-gated, so dry runs stay read-only. Enrichment never
                # fabricates: only LIVE-verified URLs are persisted.
                entry["ats_url"] = enr
                entry["queue_notes"] = (
                    (entry.get("queue_notes") or "")
                    + f" | verify-retry: posting URL enriched via board API"
                      f" ({enr}); confirmed live.").strip(" |")
                enrich_events.append(("url_enriched", rid, entry,
                                      {"recovered_url": enr,
                                       "api_source": enr_source,
                                       "token_overlap": enr_overlap,
                                       "title": entry_title,
                                       "probe": enr_probe}))
                return rid, "live", detail_e, enr
            enrich_events.append(("url_enrich_failed", rid, entry,
                                  {"reason": "live_check_failed",
                                   "live_verdict": verdict_e,
                                   "live_detail": detail_e,
                                   "title": entry_title,
                                   "probe": enr_probe}))
            return (rid, "no_url",
                    f"enrichment found {enr} but {verdict_e} ({detail_e}) — "
                    f"stays parked", None)
        enrich_events.append(("url_enrich_failed", rid, entry,
                              {"reason": enr_fail or "no_match",
                               "title": entry_title,
                               "probe": enr_probe}))
        return rid, "no_url", "NO URL — stays parked", None
    verdict, detail = check_live(url, entry_title)
    return rid, verdict, detail, None


def cache_precompute(scanned):
    """Consult live_cache before repeating HTTP.

    Returns (precomputed, hits, misses): precomputed maps role_id ->
    (verdict, detail, enr_url) for leads with a fresh 'live' verdict;
    those leads skip the fetch entirely (sync and async paths).
    Fail-open: any cache error yields zero precomputations."""
    precomputed, hits = {}, 0
    try:
        import live_cache as _lc
        for _q, _e in scanned:
            _rid = _e.get("role_id")
            _url = posting_url(_e)
            if _url and _lc.fresh_live(_rid, _url):
                precomputed[_rid] = (
                    "live",
                    "live_cache: fresh 'live' verdict — HTTP skipped",
                    None)
                hits += 1
    except Exception:
        precomputed, hits = {}, 0
    return precomputed, hits, len(scanned) - hits


def parity_probe(scanned, async_verdicts):
    """Sync/async parity guard.

    Re-classifies up to 5 sampled leads via the sync path (deep copies,
    throwaway result containers — no side effects on the real scan) and
    compares (verdict, enriched_url) against the async results.
    Returns (agree, total, drifts): drifts is a list of
    (rid, sync_verdict, async_verdict) tuples. Never raises: a probe
    failure for one lead is reported, not fatal."""
    import copy as _copy
    agree, total, drifts = 0, 0, []
    sample = [e for _q, e in scanned
              if e.get("role_id") in async_verdicts][:5]
    for _e in sample:
        _rid = _e.get("role_id")
        try:
            _s = scan_lead_sync("parity-probe",
                                _copy.deepcopy(_e), [])
            _av = async_verdicts[_rid]
            total += 1
            if _s[1] == _av[0] and (_s[3] or None) == (_av[2] or None):
                agree += 1
            else:
                drifts.append((_rid, _s[1], _av[0]))
        except Exception as _ex:
            print(f"  parity probe failed for {_rid}: {_ex}")
    return agree, total, drifts


def main():
    live = "--live" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    # --live singleton: exactly one --live run proceeds; a contender exits 0
    # after recording skipped_lock.
    run_singleton = None
    if live:
        run_singleton = _acquire_run_singleton()
        if run_singleton is None:
            print("verify_retry: another --live run is in flight — "
                  "skipping this run (exit 0)")
            _record_run({"ts": _now_iso(), "live": live, "limit": limit,
                         "status": "skipped_lock"})
            return
    try:
        return _scan_and_apply(live, limit)
    finally:
        _release_run_singleton(run_singleton)


def _scan_and_apply(live, limit):
    url_bearing_only = "--url-bearing-only" in sys.argv
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
    # The retry cooldown skips leads scanned too recently (last_verify_attempt).
    cands = []
    skipped_cooldown = 0
    for e in std:
        if (e.get("status") or "").upper() == "PARKED-PENDING-VERIFICATION":
            if in_cooldown(e):
                skipped_cooldown += 1
                continue
            cands.append(("standard", e))
    for e in ni:
        if is_verify_only(e):
            if in_cooldown(e):
                skipped_cooldown += 1
                continue
            cands.append(("needs_input", e))
    if url_bearing_only:
        # Skip the URL-less sub-pool (no enrichment API burn); URL-bearing
        # verification + dead-marking still run.
        cands = [(q, e) for q, e in cands if posting_url(e)]
    # Rotate the pool by cursor so consecutive runs walk everything.
    cands, new_cursor = window_candidates(cands, limit, load_cursor())
    if live:
        save_cursor(new_cursor)

    print(f"candidates: {len(cands)} | queue dupes to reconcile: {len(dupes)}"
          f" | in cooldown: {skipped_cooldown}")
    results = {"live": [], "dead": [], "ambiguous": [], "no_url": []}
    enrich_events = []  # (kind, rid, entry, details) telemetry tuples

    for i, (qname, e) in enumerate(cands):
        rid = e.get("role_id")
        r2, verdict, detail, enr = scan_lead_sync(qname, e, enrich_events)
        results[verdict if verdict in results else "ambiguous"].append(rid)
        if enr:
            print(f"  [{i+1}/{len(cands)}] {rid}: ENRICHED {enr} "
                  f"LIVE — {detail}")
        print(f"  [{i+1}/{len(cands)}] {rid}: {verdict.upper()} — {detail}")
        if i < len(cands) - 1:
            time.sleep(1.5)

    _record_run({"ts": _now_iso(), "live": live, "limit": limit,
                 "status": "scanned",
                 "results": {k: len(v) for k, v in results.items()}})

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
    # promotes to READY (the pre-browser re-verify gate handles any
    # remaining checks). Needs-input entries promote only when verification
    # is their SOLE blocker.
    #
    # Two fail-closed gates on the promote path:
    #   1. stale-park guard: a park-family gate event newer than the queue's
    #      status_updated withholds promotion (the park signal wins).
    #   2. promotion screening: the live posting text and application form
    #      are screened BEFORE promotion; a hit parks the lead to
    #      needs_input instead of promoting.
    #
    # Enriched URLs that passed the live check are persisted on the entry
    # (ats_url + queue note) so the promotion flow treats the lead as
    # URL-bearing downstream. Enrichment never fabricates: only LIVE URLs.
    moved = dead_n = held = 0
    parked_reasons = {}
    for qname, e in cands:
        rid = e.get("role_id")
        url = posting_url(e)
        if rid in results["live"]:
            log_event.log("lead_verified", role_id=rid,
                          company=e.get("company", ""),
                          ats=ats.detect_ats(url),
                          source="verify-retry",
                          details={"verified": "posting live via ATS API"})
            log_event.log("gate_cleared", role_id=rid,
                          company=e.get("company", ""),
                          source="verify-retry",
                          details={"gate": "pending_verification"})
            promote = (qname == "standard") or is_verify_only(e)
            if not promote:
                for x in ni:
                    if x.get("role_id") == rid:
                        x["unresolved"] = [u for u in (x.get("unresolved") or [])
                                           if not VERIFY_PAT.search(u)] or x.get("unresolved")
                continue
            if stale_park_guard(e):
                held += 1
                parked_reasons[rid] = \
                    "stale-park guard: newer park-family event than status_updated"
                e["queue_notes"] = (
                    (e.get("queue_notes") or "")
                    + " | verify-retry: LIVE but promotion withheld — "
                      "stale-park guard (park-family event newer than "
                      "status_updated).").strip(" |")
                continue
            reasons = screen_promotion_posting(e, url) + \
                screen_promotion_form(e, url)
            if reasons:
                # Blocked at the form/posting level: park to needs_input
                # (the tray), never promote.
                held += 1
                parked_reasons[rid] = "; ".join(reasons)[:300]
                e["queue_notes"] = (
                    (e.get("queue_notes") or "")
                    + " | verify-retry: posting live but pre-promotion "
                      f"screen parked ({'; '.join(reasons)[:200]}).").strip(" |")
                e["status"] = "PARKED-NEEDS-INPUT"
                e["status_reason"] = \
                    "verify-retry: live posting blocked at pre-promotion screen."
                if qname == "standard":
                    std = [x for x in std if x.get("role_id") != rid]
                    ni.append(e)
                continue
            # Flag API-direct candidacy at promotion time so the apply
            # loop can route the lead to the fast lane without a browser
            # run. Fail-closed: any detection error -> False.
            try:
                e["api_direct_candidate"] = \
                    api_direct_detect.is_api_direct_candidate(
                        url, ats=e.get("ats"))
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
        else:
            # Stayed parked: stamp the attempt so the retry cooldown paces
            # the next run.
            e["last_verify_attempt"] = _now_iso()

    for kind, rid, entry, details in enrich_events:
        log_event.log(kind, role_id=rid, company=entry.get("company", ""),
                      source="verify-retry", details=details)

    for rid, keep in reconcile:
        if keep == "needs_input":
            std = [x for x in std if x.get("role_id") != rid]
        else:
            ni = [x for x in ni if x.get("role_id") != rid]

    save(STD_Q, std)
    save(NI_Q, ni)
    save(REJ_Q, rej)
    print(f"\napplied: promoted={moved} dead={dead_n} held={held} "
          f"reconciled={len(reconcile)} left_parked="
          f"{len(results['ambiguous']) + len(results['no_url'])}")
    _record_run({"ts": _now_iso(), "live": True, "limit": limit,
                 "status": "applied", "promoted": moved, "dead": dead_n,
                 "held": held})


if __name__ == "__main__":
    main()
