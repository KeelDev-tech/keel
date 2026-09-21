"""ATS detection + pre-flight board intelligence for the application executor.

Purpose: before any browser application task launches, identify the ATS behind
the application URL and pull every scrap of machine-readable intelligence the
ATS exposes publicly. This is what lets the brief builder hand the browser task
exact option labels and the proven commit technique for that ATS instead of
guessing.

Honest boundary: the browser task tool has no JavaScript execution capability,
so techniques are DOM-event-level (real clicks, key events, native setters via
the task's own tooling). Nothing here pretends to run code in the page.
"""

import json
import re
import urllib.request
from urllib.parse import urlparse

# Parsed-identity matching rules. Whole-URL substring matching is retired: a
# vendor token in a path, fragment or unrelated query parameter no longer
# attributes the URL to that vendor.
#
# Each ATS maps to:
#   "hosts": regexes matched against the parsed, lowercased hostname only
#            (userinfo and port stripped; dot-boundary anchored);
#   "paths": (host_regex, path_regex) pairs for host+path identity;
#   "query": regexes matched against the raw query string for trusted
#            provider identifiers.
ATS_RULES = {
    "greenhouse": {
        "hosts": [r"(^|\.)greenhouse\.io$"],
        "paths": [],
        # Greenhouse's own job-id query param on employer career pages
        # (e.g. /careers/positions/7980600?gh_jid=7980600) — identity only;
        # detection never assumes a direct board URL from it.
        "query": [r"(^|&)gh_jid="],
    },
    "lever":        {"hosts": [r"(^|\.)lever\.co$"], "paths": [], "query": []},
    "ashby":        {"hosts": [r"(^|\.)ashbyhq\.com$"], "paths": [], "query": []},
    "workday":      {"hosts": [r"(^|\.)myworkdayjobs\.com$"], "paths": [], "query": []},
    "icims":        {"hosts": [r"(^|\.)icims\.com$"], "paths": [], "query": []},
    "smartrecruiters": {"hosts": [r"(^|\.)smartrecruiters\.com$"], "paths": [], "query": []},
    "jobvite":      {"hosts": [r"(^|\.)jobvite\.com$"], "paths": [], "query": []},
    "breezy":       {"hosts": [r"(^|\.)breezy\.hr$"], "paths": [], "query": []},
    "applytojob":   {"hosts": [r"(^|\.)applytojob\.com$"], "paths": [], "query": []},
    "workatastartup": {
        "hosts": [],
        "paths": [(r"(^|\.)ycombinator\.com$", r"^/companies(/|$)")],
        "query": [],
    },
    "rippling":     {"hosts": [r"(^|\.)ats\.rippling\.com$"], "paths": [], "query": []},
    "tealhq":       {"hosts": [r"(^|\.)tealhq\.com$"], "paths": [], "query": []},
    "oracle_recruiting_cloud": {
        # Oracle HCM Candidate Experience tenants (e.g. egmn.fa.us2.oraclecloud.com)
        "hosts": [r"(^|\.)fa\.[a-z0-9]+\.oraclecloud\.com$"], "paths": [], "query": [],
    },
    "comeet":       {"hosts": [r"(^|\.)comeet\.co$"], "paths": [], "query": []},
    "teamtailor":   {"hosts": [r"(^|\.)teamtailor\.com$"], "paths": [], "query": []},
    # third-party career-board host; resolve the proxied ATS per posting
    "careerpuck":   {"hosts": [r"(^|\.)careerpuck\.com$"], "paths": [], "query": []},
    "workable":     {"hosts": [r"(^|\.)workable\.com$"], "paths": [], "query": []},
    "recruitee":    {"hosts": [r"(^|\.)recruitee\.com$"], "paths": [], "query": []},
    "pinpoint":     {"hosts": [r"(^|\.)pinpointhq\.com$"], "paths": [], "query": []},
    "personio":     {"hosts": [r"(^|\.)personio\.com$"], "paths": [], "query": []},
    "bamboohr":     {"hosts": [r"(^|\.)bamboohr\.com$"], "paths": [], "query": []},
}

# Canonical platform-key registry. record_outcome.VALID_ATS reads the keys;
# the values are the structured rules above.
ATS_PATTERNS = ATS_RULES


# Aggregator/redirector domains whose application_url is NOT the final ATS page.
# The browser task resolves the real apply route at runtime (Step 0 of the brief).
AGGREGATORS = [
    "jobicy.com",
    "remoterocketship.com",
    "remoteok.com",
    "wellfound.com",
    "otta.com",
    "builtin.com",
    "dice.com",
]


def resolve_final_url(url: str, timeout: int = 15) -> str:
    """Follow HTTP redirect chains (shorteners, redirector services).

    Best-effort: aggregator pages with JS apply buttons (Jobicy etc.) do not
    HTTP-redirect — those resolve at runtime inside the browser task (Step 0).
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl()
    except Exception:
        return url


def is_aggregator(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(a) for a in AGGREGATORS)


def _split_url(url):
    """Parse a URL into (host, path, query) on parsed identity.

    Returns None when the URL is absent or unparseable — callers treat that
    as 'unknown', never as a guess. Schemeless host forms ("boards
    .greenhouse.io/x") are canonicalized with "//" so the hostname still
    parses.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    if "://" not in text and not text.startswith("//"):
        text = "//" + text
    try:
        parts = urlparse(text)
        host = (parts.hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    return host, parts.path or "", parts.query or ""


def detect_ats_explain(url):
    """Return (ats_key, evidence).

    evidence lists every rule that fired as (rule_kind, ats_key, detail)
    where rule_kind is "host", "host_path" or "query". Multiple hits mean
    the URL is ambiguous; the primary detect_ats keeps first-table-order for
    a stable single answer, and the full evidence is recorded here instead
    of silently discarded.
    """
    split = _split_url(url)
    if split is None:
        return "unknown", []
    host, path, query = split
    hits = []
    for ats, rules in ATS_RULES.items():
        for rule in rules["hosts"]:
            if re.search(rule, host):
                hits.append(("host", ats, host))
                break
        for host_rule, path_rule in rules["paths"]:
            if re.search(host_rule, host) and re.search(path_rule, path):
                hits.append(("host_path", ats, host + path))
        for qrule in rules["query"]:
            if re.search(qrule, query):
                hits.append(("query", ats, qrule))
    if not hits:
        return "unknown", []
    return hits[0][1], hits


def detect_ats(url: str) -> str:
    """Return the ATS key for a URL, or 'unknown'."""
    return detect_ats_explain(url)[0]


# ---------------------------------------------------------------------------
# Board refs — the deterministic identity of a company's ATS board.
# Format: "platform:token", e.g. "greenhouse:sofi", "ashby:headway",
# "lever:acme". Discovery workers record these in the queue entry's
# `ats_board` field when the board token is visible in the posting URL.
# Verification prefers a recorded ref over company-name token guessing —
# a recorded ref is an observation, guessing is a fallback.
# ---------------------------------------------------------------------------

_BOARD_REF_RE = re.compile(
    r"^(greenhouse|lever|ashby):[a-z0-9][a-z0-9_-]*$")


def parse_board_ref(ref) -> tuple:
    """Parse a recorded board ref ("platform:token") -> (platform, token).

    Returns (None, None) for anything absent or malformed — callers treat
    that as "no recorded ref" and fall back to guessing. Never raises."""
    if not isinstance(ref, str):
        return None, None
    m = _BOARD_REF_RE.match(ref.strip().lower())
    if not m:
        return None, None
    platform, token = ref.strip().lower().split(":", 1)
    return platform, token


def board_ref_from_url(url: str):
    """Extract the board ref from a posting URL, or None when the URL does
    not carry a visible board token (career pages, aggregators, unknown
    platforms). Canonical machine rule for what discovery workers record
    by eye in the `ats_board` field."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip().lower()
    m = re.search(
        r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9][a-z0-9_-]*)/jobs/\d+",
        u)
    if m:
        return f"greenhouse:{m.group(1)}"
    m = re.search(r"jobs\.lever\.co/([a-z0-9][a-z0-9_-]*)/[a-z0-9-]+", u)
    if m:
        return f"lever:{m.group(1)}"
    m = re.search(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_-]*)/[a-z0-9-]+", u)
    if m:
        return f"ashby:{m.group(1)}"
    return None


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------
GREENHOUSE_BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"


def parse_greenhouse(url: str):
    """Extract (board_token, job_id) from a Greenhouse URL, or (None, None)."""
    m = re.search(r"boards\.greenhouse\.io/([a-zA-Z0-9_]+)/jobs/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"job-boards\.greenhouse\.io/boards/([a-zA-Z0-9_]+)/jobs/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"[?&]for=([a-zA-Z0-9_]+).*?[?&]token=(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def greenhouse_job(board: str, job_id: str) -> dict:
    """Public metadata for a Greenhouse posting. NOTE: custom application
    questions are NOT exposed by the public API; enumerate them from the
    rendered form (browser.open) instead."""
    d = _get_json(GREENHOUSE_BOARD_API.format(board=board, job_id=job_id))
    return {
        "title": d.get("title"),
        "company": d.get("company_name"),
        "location": (d.get("location") or {}).get("name"),
        "departments": [x.get("name") for x in d.get("departments", [])],
        "offices": [x.get("name") for x in d.get("offices", [])],
        "absolute_url": d.get("absolute_url"),
        "first_published": d.get("first_published"),
        "questions_via_api": False,  # public API does not expose form questions
    }


# ---------------------------------------------------------------------------
# Lever — the one ATS whose public API exposes custom questions ("lists")
# ---------------------------------------------------------------------------
def parse_lever(url: str):
    m = re.search(r"lever\.co/([a-zA-Z0-9_-]+)/([a-zA-Z0-9-]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def lever_posting(org: str, posting_id: str) -> dict:
    d = _get_json(f"https://api.lever.co/v0/postings/{org}/{posting_id}")
    return {
        "title": d.get("text"),
        "categories": d.get("categories"),
        "country": d.get("country"),
        "workplace_type": d.get("workplaceType"),
        "apply_url": d.get("applyUrl"),
        # Custom screening questions with their exact option values, when defined.
        "custom_questions": [
            {"text": q.get("text"), "fields": q.get("fields")}
            for q in (d.get("lists") or [])
        ],
        "questions_via_api": True,
    }


# ---------------------------------------------------------------------------
# Ashby — metadata only via public API
# ---------------------------------------------------------------------------
def ashby_board_jobs(board: str) -> list:
    d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
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


def preflight(url: str) -> dict:
    """One-call pre-flight: resolve redirect chains, detect ATS, pull all public
    intelligence.

    Redirect-following can *hide* the ATS (e.g. boards.greenhouse.io redirects
    to the employer's own site with an embedded Greenhouse form), so detection
    runs on both the original and the resolved URL and prefers a known ATS.
    Aggregator pages (Jobicy etc.) that don't HTTP-redirect resolve at runtime
    inside the browser task — see Step 0 of the generated brief.

    Returns a dict the brief builder consumes. Never raises on unknown ATS —
    it degrades to {'ats': 'unknown'} and the brief falls back to the generic
    technique + rendered-form enumeration.
    """
    resolved = resolve_final_url(url)
    candidates = [url] if resolved == url else [url, resolved]
    detected = [(u, detect_ats(u)) for u in candidates]
    final_url, ats = next(
        ((u, a) for u, a in detected if a != "unknown"),
        detected[-1],
    )
    intel = {
        "ats": ats,
        "url": final_url,
        "original_url": url,
        "resolved_url": resolved,
        "aggregator": is_aggregator(url),
    }
    try:
        if ats == "greenhouse":
            board = job_id = None
            for u in candidates:  # original URL usually carries board+job tokens
                board, job_id = parse_greenhouse(u)
                if board and job_id:
                    break
            intel["board"], intel["job_id"] = board, job_id
            if board and job_id:
                intel["job"] = greenhouse_job(board, job_id)
        elif ats == "lever":
            org, posting_id = parse_lever(url)
            intel["org"], intel["posting_id"] = org, posting_id
            if org and posting_id:
                intel["posting"] = lever_posting(org, posting_id)
        elif ats == "ashby":
            m = re.search(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", url)
            if m:
                intel["board"] = m.group(1)
                intel["jobs"] = ashby_board_jobs(m.group(1))
    except Exception as e:  # public APIs are best-effort; never block the apply
        intel["api_error"] = str(e)[:200]
    return intel


def record_ats_url(role_id: str, ats_url: str) -> None:
    """Write the resolved final ATS URL back onto the queue entry so future
    briefs detect the ATS immediately instead of re-resolving."""
    import os

    from keel_paths import DATA  # noqa: E402
    queue = os.path.join(DATA, "queues", "standard-queue.json")
    d = json.load(open(queue))
    items = d if isinstance(d, list) else d.get("entries", d.get("items", []))
    for e in items:
        if e.get("role_id") == role_id:
            e["ats_url"] = ats_url
            break
    json.dump(d, open(queue, "w"), indent=2)


if __name__ == "__main__":
    import sys

    print(json.dumps(preflight(sys.argv[1]), indent=1)[:3000])
