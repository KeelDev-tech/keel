#!/usr/bin/env python3
"""verify_retry_async.py — async variant of verify_retry's verdict path.

V3 value audit (2026-09-15): verify_retry is 99.5% HTTP-wait with a fixed
1.5s inter-lead sleep (~37% of wall). This module re-runs the SAME
classification functions (check_live's verdict logic, _enrich_with_meta's
match rule) over asyncio/aiohttp with per-host semaphore + jitter.

Wired into verify_retry.py behind the `--async` flag (trial-gated:
verdict parity 31/31 leads over two samples, 5-6.8x speedup, zero 429s).
The sync path remains the default; --async is opt-in.

Behavioral parity notes:
  - async_resolve_final_url mirrors ats.resolve_final_url: GET with redirect
    following; ANY exception -> original URL.
  - async_get_json mirrors ats._get_json: GET, json.loads; non-2xx raises
    aiohttp.ClientResponseError (mapped like urllib.error.HTTPError).
  - async_check_live mirrors verify_retry.check_live line-by-line: same
    ATS branches, same marker matching, same (verdict, detail) strings.
  - async_enrich_with_meta mirrors verify_retry._enrich_with_meta: same
    token guessing, same consider()/tie rule, same
    (url, source, overlap, fail_reason) return shape.
  - scan_one_async mirrors verify_retry.scan_lead_sync: same in-memory
    entry mutations (last_enrich_attempt stamp, ats_url/queue_notes on
    enriched-live), same enrich_events telemetry payloads, same
    no_url message strings.
  - Politeness replaces the fixed 1.5s sleep: per-host semaphore
    (PER_HOST_LIMIT) + small random jitter before each request.
  - The sandbox egresses via an HTTP proxy (urllib honors it by default);
    the ClientSession is created with trust_env=True so aiohttp does too.
"""

import asyncio
import os
import random
import re
import sys
import urllib.parse

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import verify_retry as vr  # noqa: E402
import ats  # noqa: E402

# ---------------------------------------------------------------------------
# Trial tuning
# ---------------------------------------------------------------------------
PER_HOST_LIMIT = 12         # max concurrent requests to one host
                              # (2026-09-16 verification-opt: 3->6, one 2x
                              # step; 429s halve it back immediately)
                              # (2026-09-16 P-2026-09-16-verify-2,
                              # applicant-approved: 6->12, one 2x step; any 429
                              # stops the trial and halves back to 6/20)
GLOBAL_LIMIT = 40         # max concurrent requests overall (shared proxy
                          # is the real bottleneck; caps total connections)
                          # (2026-09-16 verification-opt: 10->20, one 2x step)
                          # (2026-09-16 P-2026-09-16-verify-2,
                          # applicant-approved: 20->40, one 2x step)
JITTER_MIN_S = 0.05         # jitter before each request (politeness)
JITTER_MAX_S = 0.25
UA = {"User-Agent": "Mozilla/5.0"}

# Per-run stats, reset by reset_stats(). Never raises.
STATS = {"requests": 0, "r429": 0, "r5xx": 0, "timeouts": 0}


def reset_stats():
    global STATS
    STATS = {"requests": 0, "r429": 0, "r5xx": 0, "timeouts": 0}


_semaphores = {}
_global_sem = None


def _sem_for(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    sem = _semaphores.get(host)
    if sem is None:
        sem = asyncio.Semaphore(PER_HOST_LIMIT)
        _semaphores[host] = sem
    return sem


def _global_semaphore():
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(GLOBAL_LIMIT)
    return _global_sem


async def _get(session, url, timeout, read_body=True):
    """One polite GET. Returns (status, body_bytes, final_url).

    Raises aiohttp.ClientResponseError on non-2xx (mirrors urllib's
    HTTPError); aiohttp.ClientError subclasses on network failures.
    """
    sem = _sem_for(url)
    gsem = _global_semaphore()
    await asyncio.sleep(random.uniform(JITTER_MIN_S, JITTER_MAX_S))
    async with gsem, sem:
        try:
            async with session.get(
                url, headers=UA,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                STATS["requests"] += 1
                if resp.status == 429:
                    STATS["r429"] += 1
                elif 500 <= resp.status < 600:
                    STATS["r5xx"] += 1
                body = await resp.read() if read_body else b""
                final = str(resp.url)
                resp.raise_for_status()  # mirrors urllib raising HTTPError
                return resp.status, body, final
        except asyncio.TimeoutError:
            STATS["timeouts"] += 1
            raise


async def async_resolve_final_url(session, url, timeout=15):
    """Mirror of ats.resolve_final_url: follow redirects; any failure ->
    return the original URL."""
    try:
        _status, _body, final = await _get(session, url, timeout,
                                           read_body=False)
        return final
    except Exception:
        return url


async def async_get_json(session, url, timeout=20):
    """Mirror of ats._get_json: GET + json.loads. Non-2xx raises
    aiohttp.ClientResponseError (caller maps .status like HTTPError.code)."""
    import json
    _status, body, _final = await _get(session, url, timeout)
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# async_check_live — line-by-line mirror of verify_retry.check_live
# ---------------------------------------------------------------------------
async def async_check_live(session, url, title_hint=""):
    """Returns (verdict, detail). Verdict: live | dead | ambiguous.

    Same conservative rules as verify_retry.check_live; only the HTTP layer
    is async. Verdicts must match the sync version lead-for-lead.
    """
    try:
        # 2026-09-16 verification-opt: skip the resolve GET when the
        # original URL already determines the branch (vr._resolve_needed).
        if vr._resolve_needed(url):
            resolved = await async_resolve_final_url(session, url)
        else:
            resolved = url
    except Exception as e:
        return "ambiguous", f"resolve failed: {str(e)[:80]}"
    candidates = [url] if resolved == url else [url, resolved]
    detected = [(u, ats.detect_ats(u)) for u in candidates]
    final_url, which = next(
        ((u, a) for u, a in detected if a != "unknown"), detected[-1])

    def dead_markers_in(html):
        return any(m in html for m in vr.DEAD_MARKERS)

    try:
        if which == "greenhouse":
            guesses = []
            for u in candidates:
                board, job_id = ats.parse_greenhouse(u)
                if board and job_id:
                    guesses.append((board, job_id))
                    break
            if not guesses:
                # Mirror of verify_retry.check_live (2026-09-16): employer
                # career URLs carry the Greenhouse job id as gh_jid.
                # Ordered guesses via vr._gh_jid_guesses (first-label, then
                # next-label for excluded hosts). A wrong guess 404s and
                # fail-closes to ambiguous; a 404 tries the next guess.
                guesses = vr._gh_jid_guesses(candidates)
            if not guesses:
                return "ambiguous", "greenhouse URL but no board/job tokens"
            api_404 = None
            for board, job_id in guesses:
                try:
                    job = await async_greenhouse_job(session, board, job_id)
                except aiohttp.ClientResponseError as e:
                    # Mirror of the sync fail-closed rule
                    # (J-20260916-0045-veri-440): a board-API 404/410 is not
                    # posting-death evidence. Try the next guess.
                    if e.status in (404, 410):
                        api_404 = e.status
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
                p = await async_lever_posting(session, org, pid)
            except aiohttp.ClientResponseError as e:
                # Mirror of the sync fail-closed rule (J-20260916-0045-veri-440).
                if e.status in (404, 410):
                    return "ambiguous", (
                        f"lever posting API HTTP {e.status} — "
                        "not death evidence")
                raise
            if p.get("title"):
                return "live", f"lever: {p.get('title')}"
            return "ambiguous", "lever API returned no title"
        if which == "ashby":
            m = re.search(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", final_url)
            if not m:
                return "ambiguous", "ashby URL but no board token"
            jobs = await async_ashby_board_jobs(session, m.group(1))
            if not jobs:
                return "ambiguous", "ashby board empty/unreachable"
            hint = set(re.findall(r"[a-z0-9]+", (title_hint or "").lower()))
            hint -= {"senior", "sr", "jr", "ii", "iii", "lead", "manager",
                     "remote", "us", "the", "and", "for"}
            hint = {t for t in hint if not t.isdigit()}
            for j in jobs:
                jt = set(re.findall(r"[a-z0-9]+",
                                    (j.get("title") or "").lower()))
                if hint and jt and len(hint & jt) / len(hint) >= 0.6:
                    return "live", f"ashby: role listed as '{j.get('title')}'"
            return "ambiguous", \
                f"ashby board live ({len(jobs)} jobs) but role not matched"
        if which != "unknown" and not ats.is_aggregator(url):
            return "ambiguous", f"ATS '{which}' has no HTTP liveness check"
        # Generic fallback for non-ATS posting pages (e.g. Wellfound).
        if vr.LISTING_PAT.search(final_url):
            return "ambiguous", "listing/board page — not the individual posting"
        status, body, _final = await _get(session, final_url, 15)
        # NOTE: sync's `resp.status in (404, 410)` branch is dead code
        # (urllib raises before returning); raise_for_status() in _get
        # keeps the async version equally dead there. Kept for shape parity.
        if status in (404, 410):
            return "dead", f"HTTP {status} on posting page"
        html = body.decode("utf-8", "replace")[:200000].lower()
        if dead_markers_in(html):
            return "dead", "posting page shows removed/expired markers"
        if any(m in html for m in vr.LIVE_MARKERS):
            return "live", "posting page renders with apply markers"
        return "ambiguous", "page fetched but liveness unclear"
    except aiohttp.ClientResponseError as e:
        # Mirrors `except urllib.error.HTTPError as e` in check_live.
        if e.status in (404, 410):
            return "dead", f"HTTP {e.status}"
        return "ambiguous", f"HTTP {e.status}"
    except Exception as e:
        return "ambiguous", f"check failed: {str(e)[:80]}"


async def async_greenhouse_job(session, board, job_id):
    d = await async_get_json(
        session,
        ats.GREENHOUSE_BOARD_API.format(board=board, job_id=job_id))
    return {
        "title": d.get("title"),
        "company": d.get("company_name"),
        "location": (d.get("location") or {}).get("name"),
        "departments": [x.get("name") for x in d.get("departments", [])],
        "offices": [x.get("name") for x in d.get("offices", [])],
        "absolute_url": d.get("absolute_url"),
        "first_published": d.get("first_published"),
        "questions_via_api": False,
    }


async def async_lever_posting(session, org, posting_id):
    d = await async_get_json(
        session, f"https://api.lever.co/v0/postings/{org}/{posting_id}")
    return {
        "title": d.get("text"),
        "categories": d.get("categories"),
        "country": d.get("country"),
        "workplace_type": d.get("workplaceType"),
        "apply_url": d.get("applyUrl"),
        "custom_questions": [
            {"text": q.get("text"), "fields": q.get("fields")}
            for q in (d.get("lists") or [])
        ],
        "questions_via_api": True,
    }


async def async_ashby_board_jobs(session, board):
    d = await async_get_json(
        session, f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    return [
        {
            "id": j.get("id"),
            "title": j.get("title"),
            "location": j.get("location"),
            "workplace_type": j.get("workplaceType"),
            "employment_type": j.get("employmentType"),
            "job_url": j.get("jobUrl"),
        }
        for j in d.get("jobs", [])
        if j.get("isListed")
    ]


# ---------------------------------------------------------------------------
# async_enrich_with_meta — mirror of verify_retry._enrich_with_meta
# ---------------------------------------------------------------------------
async def async_gh_list_jobs(session, token):
    d = await async_get_json(
        session, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return d.get("jobs", [])


async def async_lever_list_jobs(session, token):
    d = await async_get_json(
        session, f"https://api.lever.co/v0/postings/{token}?mode=json")
    return d if isinstance(d, list) else []


async def async_enrich_with_meta(session, entry):
    """Mirror of verify_retry._enrich_with_meta.

    Same match rule, same tie handling, same
    (url, api_source, token_overlap, fail_reason, probe_info) return shape.
    NEVER mutates `entry`.
    """
    company, title = vr.entry_company_title(entry)
    if not company or not title:
        return None, None, None, "no_match", vr._empty_probe_info()
    lead_text = f"{company} {title}"
    lead_sig = vr.distinctive_tokens(lead_text)
    title_sig = vr.distinctive_tokens(title)
    company_sig = vr.distinctive_tokens(company)

    best_key = None
    best_url = None
    best_source = None
    tie = False
    saw_success = False
    saw_hard_error = False
    saw_429 = False  # ENGINE-BRIDGE (2026-09-16, gap-hunt #19): mirror of
    # the sync 429 split — ClientResponseError.status, not HTTPError.code.
    probe_trace = {}

    def note_board_outcome(board, outcome):
        probe_trace.setdefault(board, {}).setdefault(outcome, 0)
        probe_trace[board][outcome] += 1

    def note_api_error(board, exc):
        nonlocal saw_hard_error, saw_429
        # Mirrors sync: 404 on a guessed token is a miss, not an error.
        status = getattr(exc, "status", None)
        if status == 404:
            note_board_outcome(board, "http_404")
            return
        saw_hard_error = True
        if status == 429:
            saw_429 = True
            note_board_outcome(board, "http_429")
        elif isinstance(status, int) and 500 <= status < 600:
            note_board_outcome(board, "http_5xx")
        elif isinstance(status, int):
            note_board_outcome(board, "http_4xx")
        else:
            note_board_outcome(board, "net_error")

    def consider(api_source, job_title, job_company, url):
        nonlocal best_key, best_url, best_source, tie
        if not url:
            return
        job_sig = vr.distinctive_tokens(f"{job_title} {job_company}")
        total = len(lead_sig & job_sig)
        t_ov = len(title_sig & vr.distinctive_tokens(job_title or ""))
        if total < 2 or t_ov < 2:
            return
        key = (total, t_ov)
        if best_key is None or key > best_key:
            best_key, best_url, best_source, tie = key, url, api_source, False
        elif key == best_key:
            tie = True

    def gh_url(token, j):
        return j.get("absolute_url") or \
            f"https://job-boards.greenhouse.io/{token}/jobs/{j.get('id')}"

    rec_platform, rec_token = ats.parse_board_ref(entry.get("ats_board"))
    if rec_platform:
        candidates = [(rec_platform, rec_token)]
    else:
        candidates = [(src, tok) for tok in vr.board_token_guesses(company)
                      for src in ("greenhouse", "lever", "ashby")]

    try:
        for src, token in candidates:
            if src == "greenhouse":
                try:
                    gh_jobs = await async_gh_list_jobs(session, token)
                    saw_success = True
                    note_board_outcome("greenhouse", "jobs_returned")
                    for j in gh_jobs:
                        co = j.get("company_name") or ""
                        if co and company_sig and not (
                                company_sig & vr.distinctive_tokens(co)):
                            continue
                        consider("greenhouse", j.get("title") or "", co,
                                 gh_url(token, j))
                except Exception as exc:
                    note_api_error("greenhouse", exc)
            elif src == "lever":
                try:
                    lv_jobs = await async_lever_list_jobs(session, token)
                    saw_success = True
                    note_board_outcome("lever", "jobs_returned")
                    for j in lv_jobs:
                        jt = j.get("text") or ""
                        consider("lever", jt, "",
                                 j.get("hostedUrl") or
                                 f"https://jobs.lever.co/{token}/{j.get('id')}")
                except Exception as exc:
                    note_api_error("lever", exc)
            elif src == "ashby":
                try:
                    ab_jobs = await async_ashby_board_jobs(session, token)
                    saw_success = True
                    note_board_outcome("ashby", "jobs_returned")
                    for j in ab_jobs:
                        consider("ashby", j.get("title") or "", "",
                                 j.get("job_url") or
                                 f"https://jobs.ashbyhq.com/{token}/{j.get('id')}")
                except Exception as exc:
                    note_api_error("ashby", exc)
    except Exception:
        return None, None, None, "api_error", vr._probe_info(
            saw_429, candidates, probe_trace)
    if tie:
        return None, None, None, "no_match", vr._probe_info(
            saw_429, candidates, probe_trace)
    if best_url is None:
        if saw_success:
            return None, None, None, "no_match", vr._probe_info(
                saw_429, candidates, probe_trace)
        return None, None, None, \
            "api_error" if saw_hard_error else "no_board", vr._probe_info(
                saw_429, candidates, probe_trace)
    return best_url, best_source, {"total": best_key[0],
                                   "title": best_key[1]}, None, vr._probe_info(
        saw_429, candidates, probe_trace)


# ---------------------------------------------------------------------------
# Per-lead scan — mirrors verify_retry.scan_lead_sync.
# ---------------------------------------------------------------------------
async def scan_one_async(session, qname, entry, enrich_events):
    """Async mirror of verify_retry.scan_lead_sync.

    Returns (role_id, verdict, detail, enriched_url_or_None).
    Verdict in {live, dead, ambiguous, no_url}. Mutates the passed entry
    dict exactly like the sync version (last_enrich_attempt stamp,
    ats_url/queue_notes on enriched-live) and appends the same
    enrich_events telemetry tuples. The no_url `detail` strings are the
    exact branch messages the sync scan prints (minus the [i/N] prefix);
    enriched-live returns the raw check detail like the sync version.
    """
    from datetime import datetime as _dt
    rid = entry.get("role_id")
    # queue_notes list-type safety (2026-09-16): ~500 queue entries carry
    # queue_notes as a list; downstream code concatenates it as str. Normalize
    # once at scan entry so the scan never TypeErrors mid-flight.
    _qn = entry.get("queue_notes")
    if isinstance(_qn, list):
        entry["queue_notes"] = " | ".join(str(x) for x in _qn)
    url = vr.posting_url(entry)
    _co, entry_title = vr.entry_company_title(entry)
    if not url:
        if vr.enrich_in_cooldown(entry):
            return (rid, "no_url",
                    f"enrichment in cooldown "
                    f"(attempted <{vr.ENRICH_COOLDOWN_H}h ago) — "
                    f"stays parked, no API calls", None)
        # Same stamp-at-attempt-time semantics as the sync scan: paced
        # before any HTTP so a second pass can never re-burn the calls.
        entry["last_enrich_attempt"] = _dt.now(vr.PDT).isoformat()
        enr, enr_source, enr_overlap, enr_fail, enr_probe = \
            await async_enrich_with_meta(session, entry)
        if enr:
            verdict_e, detail_e = await async_check_live(session, enr,
                                                         entry_title)
            if verdict_e == "live":
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
    verdict, detail = await async_check_live(session, url, entry_title)
    return rid, verdict, detail, None


async def scan_window_async(entries, enrich_events=None):
    """Batch-scan a list of (qname, entry) pairs concurrently.

    Returns dict role_id -> (verdict, detail, enriched_url_or_None).
    One shared ClientSession (trust_env=True for the egress proxy);
    per-host semaphore + jitter pace requests. Entry dicts are mutated
    in place exactly like the sync scan (stamps, ats_url/queue_notes);
    enrich telemetry tuples append to `enrich_events` when given.
    No queue writes, no log_event emission — verdict computation only.
    """
    if aiohttp is None:
        raise RuntimeError("verify_retry_async requires aiohttp "
                           "(not installed in this environment)")
    if enrich_events is None:
        enrich_events = []
    reset_stats()
    _semaphores.clear()
    global _global_sem
    _global_sem = None  # fresh semaphore per event loop
    results = {}
    connector = aiohttp.TCPConnector(limit=0)  # per-host pacing is explicit
    # trust_env=True: honor HTTP(S)_PROXY (urllib honors it by default,
    # aiohttp does not).
    async with aiohttp.ClientSession(connector=connector,
                                     trust_env=True) as session:
        async def one(qname, entry):
            out = await scan_one_async(session, qname, entry, enrich_events)
            results[out[0]] = (out[1], out[2], out[3])
        await asyncio.gather(*(one(q, e) for q, e in entries))
    return results
