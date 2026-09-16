"""Discovery adapter for Workable.

Primary account endpoint:
    https://www.workable.com/api/accounts/{slug}?details=true
    -> {name, description, jobs[]}

Fallback (only if the primary returns non-200):
    https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

Do NOT use /api/v3/ (confirmed dead 404).

Each job carries its inline full description; no per-posting detail
fetches are made (ENRICH_DETAILS = False).
"""

import html as _html
import json
import re

try:
    from ats_discovery.fetcher import RateLimited
except ImportError:  # http.py not provisioned yet; keep the module importable
    class RateLimited(Exception):
        """Raised when the board API returns HTTP 429."""

PLATFORM = "workable"
ATS_LABEL = "Workable"
ENRICH_DETAILS = False

SKIP_LOG = []

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*?>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(value):
    """Strip script/style blocks and tags, unescape entities, collapse whitespace."""
    if not value:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", str(value))
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _date_only(value):
    if not value:
        return ""
    return str(value)[:10]


def board_list_url(slug):
    """Return the primary account URL for a Workable account slug."""
    return "https://www.workable.com/api/accounts/%s?details=true" % slug


def _fallback_url(slug):
    return (
        "https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % slug
    )


def fetch_board(slug, http_get):
    """Fetch every job for an account slug (primary, then fallback).

    Returns list of raw job dicts passed through from the payload, each
    annotated with `_account_name` (the account display name, may be "").
    """
    url = board_list_url(slug)
    status, _ctype, body = http_get(url)
    if status == 429:
        raise RateLimited(url)

    if status == 200:
        payload = _decode_json(slug, body, "primary")
        if payload is None:
            return []
    else:
        SKIP_LOG.append({"slug": slug, "reason": "primary-endpoint-dead"})
        url = _fallback_url(slug)
        status, _ctype, body = http_get(url)
        if status == 429:
            raise RateLimited(url)
        if status != 200:
            SKIP_LOG.append(
                {
                    "slug": slug,
                    "reason": "board-not-found"
                    if status == 404
                    else "board-fetch-failed",
                }
            )
            return []
        payload = _decode_json(slug, body, "fallback")
        if payload is None:
            return []

    jobs = payload.get("jobs") or []
    account_name = str(payload.get("name") or "").strip()
    results = []
    for job in jobs:
        if isinstance(job, dict):
            job["_account_name"] = account_name
            results.append(job)
    return results


def _decode_json(slug, body, which):
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        SKIP_LOG.append({"slug": slug, "reason": "invalid-board-json-" + which})
        return None


def normalize(raw, slug, employer):
    """Normalize one job dict into the canonical lead shape."""
    shortcode = raw.get("shortcode")
    posting_url = raw.get("url") or raw.get("application_url")
    if not posting_url or not str(posting_url).strip():
        raise ValueError(
            "cannot construct posting_url (no url or application_url)"
        )
    posting_url = str(posting_url).strip()

    loc_parts = [raw.get("city"), raw.get("state"), raw.get("country")]
    location_raw = ", ".join(
        str(p).strip() for p in loc_parts if p and str(p).strip()
    )

    telecommuting = raw.get("telecommuting")
    remote = True if telecommuting is True else None

    employment_type = str(raw.get("employment_type") or "").strip()

    department = raw.get("department")
    if isinstance(department, dict):
        department_name = str(department.get("name") or department.get("label") or "").strip()
    elif department:
        department_name = str(department).strip()
    else:
        department_name = ""

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": raw.get("_account_name") or employer,
        "title": raw.get("title") or "",
        "location": {
            "raw": location_raw,
            "city": str(raw.get("city") or "").strip(),
            "region": str(raw.get("state") or "").strip(),
            "country": str(raw.get("country") or "").strip(),
            "remote": remote,
        },
        "posting_url": posting_url,
        "source_job_id": shortcode or "",
        "description": strip_html(raw.get("description")),
        "posted_date": _date_only(raw.get("published_on") or raw.get("created_at")),
        "compensation": "",
        "employment_type": employment_type,
        "remote": remote,
        "liveness": {
            "source": "feed-presence",
            "published_on": raw.get("published_on") or "",
            "created_at": raw.get("created_at") or "",
            "department": department_name,
        },
        # Presence in jobs[] is the only activity signal; never mark dead
        # on the absence of an explicit dead field (there is none).
        "dead": False,
    }
