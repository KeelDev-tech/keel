"""Recruitee discovery adapter.

Board endpoint: https://{slug}.recruitee.com/api/offers/
Shape: {"offers": [...]}. Single call returns all offers (the `page` param
returns the same set), so this adapter deliberately does NOT paginate.

Live-validated 2026-09-15. Spec: richest inline item set of the four adapters.
"""

import html
import json
import re

try:
    from .fetcher import RateLimited
except ImportError:  # ats_discovery/http.py ships separately; local fail-safe
    class RateLimited(Exception):
        """Raised when the board endpoint answers HTTP 429."""

PLATFORM = "recruitee"
ATS_LABEL = "Recruitee"
ENRICH_DETAILS = False  # all needed fields are inline; no per-posting fetch
SKIP_LOG = []


def board_list_url(slug: str) -> str:
    return "https://%s.recruitee.com/api/offers/" % slug


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


def _iso_date(value):
    """Best-effort conversion to YYYY-MM-DD; '' when unparseable."""
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
    """Defensible stringification of the salary field."""
    if salary is None or salary == "":
        return ""
    if isinstance(salary, dict):
        parts = [str(salary.get(k)) for k in ("min", "max")
                 if salary.get(k) not in (None, "")]
        cur = salary.get("currency") or salary.get("salary_currency") or ""
        s = " - ".join(parts)
        if cur:
            s = (s + " " + str(cur)).strip()
        return s or str(salary)
    if isinstance(salary, (list, tuple)):
        return ", ".join(str(x) for x in salary)
    return str(salary)


# ---------------------------------------------------------------- fetch

def fetch_board(slug, http_get):
    """Return raw offer dicts. 429 -> RateLimited; non-200 -> [] (dead board)."""
    url = board_list_url(slug)
    status, _content_type, body = http_get(url)
    if status == 429:
        raise RateLimited("recruitee board %s rate limited" % slug)
    if status != 200:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        SKIP_LOG.append({"slug": slug, "reason": "json-parse-failure"})
        return []
    if isinstance(data, dict):
        offers = data.get("offers", [])
    elif isinstance(data, list):
        offers = data
    else:
        SKIP_LOG.append({"slug": slug, "reason": "unexpected-json-shape"})
        return []
    if not isinstance(offers, list):
        SKIP_LOG.append({"slug": slug, "reason": "unexpected-json-shape"})
        return []
    return [o for o in offers if isinstance(o, dict)]


# ---------------------------------------------------------------- normalize

def normalize(raw, slug, employer):
    """Map one raw offer dict to the contract shape."""
    offer_slug = raw.get("slug") or ""
    careers_url = raw.get("careers_url") or ""
    if careers_url:
        posting_url = careers_url
    elif offer_slug:
        posting_url = "https://jobs.%s.recruitee.com/o/%s" % (slug, offer_slug)
    else:
        raise ValueError("cannot construct posting_url: no slug and no careers_url")

    status = raw.get("status") or ""
    # dead ONLY on an explicit non-published status; never on absence.
    dead = bool(status) and str(status).lower() != "published"

    rv = raw.get("remote")
    if rv is not None:
        remote = bool(rv)
    else:
        remote = True if raw.get("hybrid") else None

    city = raw.get("city") or ""
    country = raw.get("country_code") or ""
    loc_label = raw.get("location") or ""
    raw_loc = loc_label or ", ".join(p for p in (city, country) if p)

    description = _strip_html(raw.get("description"))
    reqs = _strip_html(raw.get("requirements"))
    if reqs:
        description = (description + "\n\nRequirements:\n" + reqs).strip()

    posted = _iso_date(raw.get("published_at")) or _iso_date(raw.get("created_at"))

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": employer,  # feed carries no company field; employer is the best name
        "title": raw.get("title") or "",
        "location": {
            "raw": raw_loc,
            "city": city,
            "region": "",
            "country": country,
            "remote": remote,
        },
        "posting_url": posting_url,
        "source_job_id": str(offer_slug) if offer_slug else "",
        "description": description,
        "posted_date": posted,
        "compensation": _compensation(raw.get("salary")),
        "employment_type": "",
        "remote": remote,
        "liveness": {"status": status},
        "dead": dead,
    }
