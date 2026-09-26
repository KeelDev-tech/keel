"""BambooHR discovery adapter (MARGINAL platform).

Board endpoint: https://{slug}.bamboohr.com/careers/list
Shape: {"meta": {"totalCount": N}, "result": [...]} with items
    {id, jobOpeningName, departmentLabel, employmentStatusLabel,
     atsLocation{country, state, city}, isRemote, locationType}.

MARGINAL: at least one validated tenant returns marketing HTML at this URL,
so fetch_board is wired BEHIND a mandatory content-type guard: if the
response content-type does not contain "json", the board is skipped
(SKIP_LOG reason "non-json-content-type") and [] is returned.

No description and no posted date are inline, so normalize() returns
description "" and posted_date "" with liveness noting the limitation.

Live-validated 2026-09-15.
"""

import json

try:
    from .fetcher import RateLimited
except ImportError:  # ats_discovery/http.py ships separately; local fail-safe
    class RateLimited(Exception):
        """Raised when the board endpoint answers HTTP 429."""

PLATFORM = "bamboohr"
ATS_LABEL = "BambooHR"
ENRICH_DETAILS = False
SKIP_LOG = []


def board_list_url(slug: str) -> str:
    return "https://%s.bamboohr.com/careers/list" % slug


def fetch_board(slug, http_get):
    """Return raw posting dicts. 429 -> RateLimited; non-200 -> [];
    non-JSON content-type -> skip-log + [] (marginal-platform guard)."""
    url = board_list_url(slug)
    status, content_type, body = http_get(url)
    if status == 429:
        raise RateLimited("bamboohr board %s rate limited" % slug)
    if status != 200:
        return []
    if "json" not in (content_type or "").lower():
        SKIP_LOG.append({"slug": slug, "reason": "non-json-content-type"})
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        SKIP_LOG.append({"slug": slug, "reason": "json-parse-failure"})
        return []
    if isinstance(data, dict):
        items = data.get("result", [])
    elif isinstance(data, list):
        items = data
    else:
        SKIP_LOG.append({"slug": slug, "reason": "unexpected-json-shape"})
        return []
    if not isinstance(items, list):
        SKIP_LOG.append({"slug": slug, "reason": "unexpected-json-shape"})
        return []
    return [i for i in items if isinstance(i, dict)]


def normalize(raw, slug, employer):
    """Map one raw posting dict to the contract shape."""
    job_id = raw.get("id")
    if job_id in (None, ""):
        raise ValueError("cannot construct posting_url: item has no id")
    posting_url = "https://%s.bamboohr.com/careers/%s" % (slug, job_id)

    ats_loc = raw.get("atsLocation") or {}
    city = str(ats_loc.get("city") or "")
    region = str(ats_loc.get("state") or "")
    country = str(ats_loc.get("country") or "")
    raw_loc = ", ".join(p for p in (city, region, country) if p)

    is_remote = raw.get("isRemote")
    remote = bool(is_remote) if is_remote is not None else None

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": employer,  # feed carries no company field
        "title": raw.get("jobOpeningName") or "",
        "location": {
            "raw": raw_loc,
            "city": city,
            "region": region,
            "country": country,
            "remote": remote,
        },
        "posting_url": posting_url,
        # Tenant-local ids: the same id can recur across tenants, so namespace.
        "source_job_id": "bamboohr:%s:%s" % (slug, job_id),
        "description": "",
        "posted_date": "",
        "compensation": "",
        "employment_type": raw.get("employmentStatusLabel") or "",
        "remote": remote,
        "liveness": {"source": "feed-presence",
                     "note": "no-inline-description",
                     "location_type": raw.get("locationType") or ""},
        "dead": False,  # no explicit dead signal is available from this feed
    }
