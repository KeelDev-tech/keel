#!/usr/bin/env python3
"""Shared API-direct candidacy detection.

Single source of truth for the question: "can this lead be filed through
api_submit.py without spending a live browser run?"

Current rule (reuses the existing Enterprise heuristic — no new invention):
  - The URL must be a directly submittable Greenhouse board URL, i.e. the
    exact form api_submit.py accepts: job-boards.greenhouse.io/<board>/
    jobs/<id> (or the older boards.greenhouse.io equivalent). Employer
    career pages carrying a gh_jid param, aggregator URLs, and EU hosts
    are NOT candidates — api_submit.py cannot POST to them either.
  - The board must NOT use reCAPTCHA Enterprise (pure HTTP cannot mint a
    valid enterprise token; greenhouse_direct.py detects this from the
    posting page's recaptcha enterprise.js marker).

Fail-closed: unknown ATS, unparseable URL, or any fetch failure -> False.
The browser path stays the default; API-direct is an earned fast lane.

LEVER (added 2026-09-15): Lever URLs are recognized by detect() and return
candidate=False with a documented reason — never a candidate. Empirical
verdict (endpoint probes + frontend inspection, 2026-09-15): api.lever.co
POST requires an employer API key (HTTP 403 "You need an API key"), and
the hosted form at jobs.lever.co/{org}/{id}/apply renders Lever's
platform-wide hCaptcha on every submit. Pure-HTTP submission is impossible;
Lever leads route to the browser path, optionally via lever_submit.py's
dry-run pre-flight (question-mapping validation before the browser run).

Used by:
  - apply_loop.py (ordering bonus + transport selection)
  - verify_retry.py (flags leads at READY promotion time)
  - sweep workers (flagging at discovery; see
    discovery/_sweep-worker-prompt-snippet-apidirect.md)
  - backfill_api_direct.py (one-time retroactive flagging)
"""

import os
import re
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import ats as ats_mod  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"}

# The Enterprise heuristic: a recaptcha enterprise.js script tag on the
# posting page means pure-HTTP submission is impossible.
ENTERPRISE_JS_PAT = re.compile(r'recaptcha[^"\\\']*enterprise\.js', re.I)


def parse_board_url(url):
    """Parse a direct Greenhouse board URL into (board, job_id).

    Public-edition local parser (replaces the private greenhouse_direct
    helper). Raises ValueError when the URL is not a direct board URL.
    """
    m = re.search(r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_\-]+)/jobs/(\d+)",
                  url or "", re.I)
    if not m:
        raise ValueError("not a direct Greenhouse board URL: %r" % (url,))
    return m.group(1), m.group(2)


def fetch_html(url, timeout=15):
    """Fetch a page's HTML. Raises on any failure (caller fails closed)."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def is_enterprise_board(html):
    """True when the posting HTML carries the reCAPTCHA Enterprise marker."""
    return bool(ENTERPRISE_JS_PAT.search(html or ""))


LEVER_REASON = (
    "Lever: hosted apply form renders platform-wide hCaptcha on every "
    "submit (verified 2026-09-15) and api.lever.co POST requires an "
    "employer API key (HTTP 403 verified 2026-09-15) — pure-HTTP "
    "submission impossible; browser path required "
    "(lever_submit.py offers a dry-run pre-flight only)")


def detect(url, ats=None):
    """Full detection verdict.

    Returns {"candidate": bool, "ats": str, "reason": str}.
    candidate=True only when the URL is a directly submittable Greenhouse
    board URL (the exact direct-board form) AND
    the board carries no reCAPTCHA Enterprise marker. Lever URLs are
    recognized and return candidate=False with a documented reason — Lever
    is not an API-direct candidate (see LEVER_REASON). Anything else —
    employer career pages with gh_jid params, aggregator URLs, EU hosts,
    fetch failures — fails closed to the browser path.
    """
    url = (url or "").strip()
    if not url:
        return {"candidate": False, "ats": ats or "",
                "reason": "no application URL"}
    detected = (ats or "").strip().lower() or ats_mod.detect_ats(url)
    if detected == "lever" or "lever.co/" in url.lower():
        return {"candidate": False, "ats": "lever", "reason": LEVER_REASON}
    try:
        board, job_id = parse_board_url(url)
    except ValueError:
        return {"candidate": False, "ats": detected,
                "reason": "not a direct Greenhouse board URL "
                          "(direct submission needs "
                          "job-boards.greenhouse.io/<board>/jobs/<id>); "
                          "browser path"}
    canonical = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
    try:
        html = fetch_html(canonical)
    except Exception as e:
        return {"candidate": False, "ats": "greenhouse",
                "reason": f"posting page fetch failed ({e}); fail closed to "
                          f"browser path"}
    if is_enterprise_board(html):
        return {"candidate": False, "ats": "greenhouse",
                "reason": "board uses reCAPTCHA Enterprise — pure HTTP "
                          "cannot mint a token; browser path required"}
    return {"candidate": True, "ats": "greenhouse",
            "reason": "Greenhouse board, no Enterprise marker"}


def is_api_direct_candidate(url, ats=None):
    """Boolean convenience wrapper. Fail-closed on any error."""
    try:
        return bool(detect(url, ats=ats)["candidate"])
    except Exception:
        return False


if __name__ == "__main__":
    # CLI smoke check: python3 api_direct_detect.py <url> [ats]
    if len(sys.argv) < 2:
        sys.exit("usage: api_direct_detect.py <posting-url> [ats]")
    import json
    print(json.dumps(detect(sys.argv[1],
                            sys.argv[2] if len(sys.argv) > 2 else None),
                     indent=1))
