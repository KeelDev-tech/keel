"""Discovery adapter for SmartRecruiters.

List endpoint (paginated):
    https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={off}
    -> {totalFound, limit, offset, content[]}

Detail endpoint (per posting, gated by ENRICH_DETAILS):
    https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}
    -> {postingUrl, applyUrl, active, jobAd}

NOTE: the company slug is case-sensitive ("Bosch" returned 0 results).
List items do NOT carry postingUrl, so per-posting detail fetches are
required for enriched output (ENRICH_DETAILS = True).
"""

import html as _html
import json
import re

try:
    from ats_discovery.fetcher import RateLimited
except ImportError:  # http.py not provisioned yet; keep the module importable
    class RateLimited(Exception):
        """Raised when the board API returns HTTP 429."""

PLATFORM = "smartrecruiters"
ATS_LABEL = "SmartRecruiters"
ENRICH_DETAILS = True

SKIP_LOG = []

_LIMIT = 100

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
    """Return the page-0 list URL for a SmartRecruiters company slug.

    Slug case is preserved exactly (case-sensitive API).
    """
    return _page_url(slug, 0)


def _page_url(slug, offset):
    return (
        "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=%d&offset=%d"
        % (slug, _LIMIT, offset)
    )


def _detail_url(slug, posting_id):
    return "https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (
        slug,
        posting_id,
    )


def fetch_board(slug, http_get):
    """Fetch every posting for a company slug (paginated). Returns list of raw dicts.

    Raw dict keys: id, company_name, name, location (raw dict), released_date,
    employment_type, function, industry, experience_level, ref, detail (dict or None).
    """
    results = []
    offset = 0
    total_found = None

    while True:
        status, _ctype, body = http_get(_page_url(slug, offset))
        if status == 429:
            raise RateLimited(_page_url(slug, offset))
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
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            SKIP_LOG.append({"slug": slug, "reason": "invalid-board-json"})
            return []

        if total_found is None:
            total_found = payload.get("totalFound")

        items = payload.get("content") or []
        for item in items:
            if isinstance(item, dict):
                results.append(_fetch_item(slug, item, http_get))

        if (
            not isinstance(total_found, int)
            or offset + _LIMIT >= total_found
            or not items
        ):
            break
        offset += _LIMIT

    return results


def _fetch_item(slug, item, http_get):
    loc = item.get("location") or {}
    company = item.get("company") or {}
    posting_id = item.get("id")

    raw = {
        "id": posting_id,
        "company_name": company.get("name"),
        "name": item.get("name"),
        "location": loc if isinstance(loc, dict) else {},
        "released_date": item.get("releasedDate"),
        "employment_type": item.get("typeOfEmployment"),
        "function": item.get("function"),
        "industry": item.get("industry"),
        "experience_level": item.get("experienceLevel"),
        "ref": item.get("ref"),
        "detail": None,
    }

    if ENRICH_DETAILS and posting_id:
        detail = _fetch_detail(slug, posting_id, http_get)
        raw["detail"] = detail
        if detail is None:
            # Detail fetch failed in a way that is not a 404 (already logged);
            # the list presence still stands as the liveness signal.
            raw["_detail_failed"] = True

    return raw


def _fetch_detail(slug, posting_id, http_get):
    url = _detail_url(slug, posting_id)
    status, _ctype, body = http_get(url)
    if status == 429:
        raise RateLimited(url)
    if status == 404:
        SKIP_LOG.append({"slug": slug, "reason": "posting-detail-not-found"})
        return {"_dead": True}
    if status != 200:
        SKIP_LOG.append({"slug": slug, "reason": "posting-detail-fetch-failed"})
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        SKIP_LOG.append({"slug": slug, "reason": "invalid-detail-json"})
        return None


def normalize(raw, slug, employer):
    """Normalize one fetch_board raw dict into the canonical lead shape."""
    detail = raw.get("detail") or {}
    loc = raw.get("location") or {}

    if detail.get("_dead"):
        dead = True
    elif "active" in detail:
        dead = not bool(detail["active"])
    else:
        # The list endpoint only returns active postings; never mark dead
        # on the absence of a detail signal.
        dead = False

    posting_id = raw.get("id")
    posting_url = (
        detail.get("postingUrl")
        or detail.get("applyUrl")
        or (
            "https://jobs.smartrecruiters.com/%s/%s" % (slug, posting_id)
            if posting_id
            else None
        )
    )
    if not posting_url:
        raise ValueError("cannot construct posting_url (no id or postingUrl)")

    remote = loc.get("remote")
    if remote is not None:
        remote = bool(remote)

    if ENRICH_DETAILS and not raw.get("_detail_failed"):
        liveness = {
            "source": "list-detail",
            "active": detail.get("active"),
            "has_apply_url": bool(detail.get("applyUrl")),
        }
    else:
        liveness = {"source": "list-only"}

    location_raw = (
        loc.get("fullLocation")
        or " ".join(
            str(loc.get(k) or "").strip()
            for k in ("city", "region", "country")
        ).strip()
    )

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": raw.get("company_name") or detail.get("company", {}).get("name") or employer,
        "title": raw.get("name") or "",
        "location": {
            "raw": location_raw,
            "city": loc.get("city") or "",
            "region": loc.get("region") or "",
            "country": loc.get("country") or "",
            "remote": remote,
        },
        "posting_url": posting_url,
        "source_job_id": posting_id or "",
        "description": strip_html(detail.get("jobAd")),
        "posted_date": _date_only(raw.get("released_date")),
        "compensation": "",
        "employment_type": raw.get("employment_type") or "",
        "remote": remote,
        "liveness": liveness,
        "dead": dead,
    }
