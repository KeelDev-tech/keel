"""Breezy discovery adapter.

Board endpoint: https://{slug}.breezy.hr/json
Shape: JSON list of
    {id, friendly_id, name, url (FULL posting URL), published_date,
     location{}, locations, type, salary, company, department}.
No description is inline, so the adapter enriches each posting by fetching its
posting URL (gated by ENRICH_DETAILS). The fetched detail is stashed on the
raw dict under the "_detail" key so normalize() keeps its contract signature.

Live-validated 2026-09-15.
"""

import html
import json
import re

try:
    from .fetcher import RateLimited
except ImportError:  # ats_discovery/http.py ships separately; local fail-safe
    class RateLimited(Exception):
        """Raised when the board endpoint answers HTTP 429."""

PLATFORM = "breezy"
ATS_LABEL = "Breezy"
ENRICH_DETAILS = True
SKIP_LOG = []


def board_list_url(slug: str) -> str:
    return "https://%s.breezy.hr/json" % slug


# ---------------------------------------------------------------- helpers

def _strip_html(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_page_text(body):
    """Main text of a posting page: drop nav/script/style chrome, collapse."""
    if not body:
        return ""
    text = re.sub(
        r"(?is)<(script|style|nav|footer|header|aside|noscript)[^>]*>.*?</\1>",
        " ", body)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def _compensation(salary):
    if salary is None or salary == "":
        return ""
    if isinstance(salary, dict):
        parts = [str(salary.get(k)) for k in ("min", "max")
                 if salary.get(k) not in (None, "")]
        cur = salary.get("currency") or ""
        s = " - ".join(parts)
        if cur:
            s = (s + " " + str(cur)).strip()
        return s or str(salary)
    if isinstance(salary, (list, tuple)):
        return ", ".join(str(x) for x in salary)
    return str(salary)


def _location_parts(raw):
    """Return (raw_str, city, region, country, remote_bool_or_None).

    Honest semantics: True when a location string says "remote";
    None when there is no location information at all; False otherwise.
    """
    loc = raw.get("location")
    locs = raw.get("locations") or []
    strings = []
    city = region = country = ""
    if isinstance(loc, dict):
        city = str(loc.get("city") or loc.get("name") or "")
        region = str(loc.get("state") or loc.get("region") or "")
        country = str(loc.get("country") or "")
        joined = ", ".join(p for p in (city, region, country) if p)
        if joined:
            strings.append(joined)
    elif loc:
        strings.append(str(loc))
    for l in locs:
        if isinstance(l, dict):
            name = l.get("name") or l.get("city") or l
            strings.append(str(name))
        elif l:
            strings.append(str(l))
    strings = [s for s in strings if s]
    raw_str = ", ".join(strings)
    if any("remote" in s.lower() for s in strings):
        remote = True
    elif not strings:
        remote = None
    else:
        remote = False
    return raw_str, city, region, country, remote


# ---------------------------------------------------------------- fetch

def _enrich(item, http_get):
    """Per-posting detail fetch. Mutates item["_detail"]."""
    detail = {"description_source": "skipped",
              "note": "ENRICH_DETAILS=False"}
    url = item.get("url")
    if ENRICH_DETAILS and url:
        status, _ctype, body = http_get(url)
        if status == 429:
            raise RateLimited("breezy posting %s rate limited" % url)
        if status == 404:
            detail = {"description_source": "per-posting-fetch",
                      "detail_http_status": 404,
                      "note": "posting page 404"}
            item["_detail_dead"] = True
        elif status == 200:
            detail = {"description_source": "per-posting-fetch",
                      "detail_http_status": 200}
            item["_detail_description"] = _extract_page_text(body)
        else:
            detail = {"description_source": "per-posting-fetch",
                      "detail_http_status": status,
                      "note": "detail fetch non-200"}
    item["_detail"] = detail


def fetch_board(slug, http_get):
    """Return raw posting dicts (enriched with _detail). 429 -> RateLimited;
    non-200 -> [] (dead board)."""
    url = board_list_url(slug)
    status, _content_type, body = http_get(url)
    if status == 429:
        raise RateLimited("breezy board %s rate limited" % slug)
    if status != 200:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        SKIP_LOG.append({"slug": slug, "reason": "json-parse-failure"})
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = None
        for key in ("positions", "jobs", "openings", "results"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None:
            SKIP_LOG.append({"slug": slug, "reason": "unexpected-json-shape"})
            return []
    else:
        SKIP_LOG.append({"slug": slug, "reason": "unexpected-json-shape"})
        return []
    items = [i for i in items if isinstance(i, dict)]
    for item in items:
        _enrich(item, http_get)
    return items


# ---------------------------------------------------------------- normalize

def normalize(raw, slug, employer):
    """Map one raw posting dict to the contract shape."""
    posting_url = raw.get("url") or ""
    if not posting_url:
        raise ValueError("cannot construct posting_url: item has no url")

    # Tenant-scoped id: the same numeric id can recur across Breezy tenants,
    # so namespace it with friendly_id.
    # Tenant-scoped ids (no stable global id on Breezy) — namespace them so
    # cross-tenant collisions are impossible in logs/dedupe keys.
    source_job_id = "breezy:%s:%s:%s" % (slug, raw.get("id"),
                                         raw.get("friendly_id"))

    raw_loc, city, region, country, remote = _location_parts(raw)
    detail = raw.get("_detail") or {}
    dead = bool(raw.get("_detail_dead"))

    company = raw.get("company") or employer

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": company,
        "title": raw.get("name") or "",
        "location": {
            "raw": raw_loc,
            "city": city,
            "region": region,
            "country": country,
            "remote": remote,
        },
        "posting_url": posting_url,
        "source_job_id": source_job_id,
        "description": raw.get("_detail_description") or "",
        "posted_date": _iso_date(raw.get("published_date")),
        "compensation": _compensation(raw.get("salary")),
        "employment_type": raw.get("type") or "",
        "remote": remote,
        "liveness": detail,
        "dead": dead,
    }
