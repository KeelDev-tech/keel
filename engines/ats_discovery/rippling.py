"""Discovery adapter for Rippling.

List endpoint:
    https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
    items: {uuid, name, department{id,label}, url (full apply URL), workLocation{label}}
    (no dates or descriptions inline)

Detail endpoint (gated by ENRICH_DETAILS):
    https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}
    -> {description (HTML), createdOn, employmentType, workLocations,
        payRangeDetails, url, unlistedFromSearch, jsonLd}
"""

import html as _html
import json
import re

try:
    from .fetcher import RateLimited
except ImportError:  # http.py not provisioned yet; keep the module importable
    class RateLimited(Exception):
        """Raised when the board API returns HTTP 429."""

PLATFORM = "rippling"
ATS_LABEL = "Rippling"
ENRICH_DETAILS = True

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
    """Return the jobs list URL for a Rippling board slug."""
    return "https://api.rippling.com/platform/api/ats/v1/board/%s/jobs" % slug


def _detail_url(slug, uuid):
    return "https://api.rippling.com/platform/api/ats/v1/board/%s/jobs/%s" % (
        slug,
        uuid,
    )


def fetch_board(slug, http_get):
    """Fetch every job on the board. Returns list of raw dicts.

    Raw dict keys: uuid, name, department (raw), work_location_label, url,
    detail (dict or None).
    """
    url = board_list_url(slug)
    status, _ctype, body = http_get(url)
    if status == 429:
        raise RateLimited(url)
    if status != 200:
        SKIP_LOG.append(
            {
                "slug": slug,
                "reason": "board-not-found" if status == 404 else "board-fetch-failed",
            }
        )
        return []
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        SKIP_LOG.append({"slug": slug, "reason": "invalid-board-json"})
        return []

    if isinstance(payload, dict):
        items = payload.get("jobs") or payload.get("data") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        work_loc = item.get("workLocation") or {}
        results.append(
            {
                "uuid": item.get("uuid"),
                "name": item.get("name"),
                "department": item.get("department"),
                "work_location_label": work_loc.get("label")
                if isinstance(work_loc, dict)
                else work_loc,
                "url": item.get("url"),
                "detail": _fetch_detail(slug, item.get("uuid"), http_get)
                if (ENRICH_DETAILS and item.get("uuid"))
                else None,
            }
        )
    return results


def _fetch_detail(slug, uuid, http_get):
    url = _detail_url(slug, uuid)
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


def _compensation(detail):
    prd = (detail or {}).get("payRangeDetails")
    if not prd:
        return ""
    if isinstance(prd, str):
        return prd.strip()
    if isinstance(prd, dict):
        lo = None
        hi = None
        currency = None
        for key, value in prd.items():
            low = str(key).lower()
            if lo is None and ("min" in low or "low" in low) and not isinstance(
                value, (dict, list)
            ):
                lo = value
            elif hi is None and ("max" in low or "high" in low) and not isinstance(
                value, (dict, list)
            ):
                hi = value
            elif currency is None and "curr" in low and not isinstance(
                value, (dict, list)
            ):
                currency = value
        parts = [
            str(v).strip()
            for v in (lo, hi)
            if v is not None and str(v).strip() != ""
        ]
        if not parts:
            return ""
        text = " - ".join(parts)
        if currency and str(currency).strip():
            text = "%s %s" % (text, str(currency).strip())
        return text.strip()
    if isinstance(prd, list):
        texts = [str(p).strip() for p in prd if p]
        return "; ".join(texts)
    return ""


def normalize(raw, slug, employer):
    """Normalize one fetch_board raw dict into the canonical lead shape."""
    detail = raw.get("detail") or {}
    uuid = raw.get("uuid")

    if detail.get("_dead"):
        dead = True
    elif detail.get("unlistedFromSearch"):
        dead = True
    else:
        # Never mark dead on the absence of an explicit dead signal.
        dead = False

    posting_url = raw.get("url") or detail.get("url")
    if not posting_url or not str(posting_url).strip():
        raise ValueError("cannot construct posting_url (no url on item or detail)")
    posting_url = str(posting_url).strip()

    work_locations = detail.get("workLocations")
    if isinstance(work_locations, list) and work_locations:
        labels = []
        for wl in work_locations:
            if isinstance(wl, dict):
                labels.append(str(wl.get("label") or "").strip())
            elif wl:
                labels.append(str(wl).strip())
        location_raw = ", ".join(p for p in labels if p)
    else:
        location_raw = str(raw.get("work_location_label") or "").strip()

    department = raw.get("department")
    if isinstance(department, dict):
        employment_context = str(department.get("label") or "").strip()
    elif department:
        employment_context = str(department).strip()
    else:
        employment_context = ""

    employment_type = str(detail.get("employmentType") or "").strip()

    if ENRICH_DETAILS and detail:
        liveness = {
            "source": "list-detail",
            "unlistedFromSearch": bool(detail.get("unlistedFromSearch")),
            "createdOn": detail.get("createdOn") or "",
        }
    else:
        liveness = {"source": "list-only"}

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": employer,
        "title": raw.get("name") or "",
        "location": {
            "raw": location_raw,
            "city": "",
            "region": "",
            "country": "",
            "remote": None,
        },
        "posting_url": posting_url,
        "source_job_id": uuid or "",
        "description": strip_html(detail.get("description")),
        "posted_date": _date_only(detail.get("createdOn")),
        "compensation": _compensation(detail),
        "employment_type": employment_type,
        "remote": None,
        "liveness": liveness,
        "dead": dead,
    }
