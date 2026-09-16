"""Discovery adapter for Pinpoint.

Board endpoint (single call, no pagination):
    https://{slug}.pinpointhq.com/postings.json
    -> {data[]}

Each item carries its full posting URL (`url`), inline HTML description,
compensation fields, workplace_type, and deadline_at. The payload is heavy
(~3.3MB for 141 postings), so it is parsed exactly once and no per-posting
detail fetches are made (ENRICH_DETAILS = False).
"""

import html as _html
import json
import re

try:
    from ats_discovery.fetcher import RateLimited
except ImportError:  # http.py not provisioned yet; keep the module importable
    class RateLimited(Exception):
        """Raised when the board API returns HTTP 429."""

PLATFORM = "pinpoint"
ATS_LABEL = "Pinpoint"
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
    """Return the postings feed URL for a Pinpoint company slug."""
    return "https://%s.pinpointhq.com/postings.json" % slug


def fetch_board(slug, http_get):
    """Fetch all postings for a company slug. Returns list of raw item dicts.

    The feed already carries every field normalize() needs; items are passed
    through as-is (they must be dicts).
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

    items = payload.get("data") or []
    return [item for item in items if isinstance(item, dict)]


def _compensation(item):
    comp = item.get("compensation")
    if comp:
        return str(comp).strip()
    lo = item.get("min")
    hi = item.get("max")
    currency = item.get("currency")
    parts = []
    if lo is not None and lo != "":
        parts.append(str(lo))
    if hi is not None and hi != "":
        parts.append(str(hi))
    if not parts:
        return ""
    text = " - ".join(parts)
    if currency:
        text = "%s %s" % (text, str(currency).strip())
    return text.strip()


def normalize(raw, slug, employer):
    """Normalize one feed item into the canonical lead shape."""
    loc = raw.get("location") or {}
    if not isinstance(loc, dict):
        loc = {}

    workplace = str(raw.get("workplace_type") or "").lower()
    if "remote" in workplace:
        remote = True
    elif "on-site" in workplace or "onsite" in workplace or "office" in workplace:
        remote = False
    else:
        remote = None

    employment_type = str(raw.get("employment_type") or "").strip()
    if "hybrid" in workplace and "hybrid" not in employment_type.lower():
        employment_type = (employment_type + " (hybrid)").strip(" ")

    url = raw.get("url")
    if not url:
        raise ValueError("cannot construct posting_url (no url on feed item)")
    posting_url = str(url).strip()
    if not posting_url:
        raise ValueError("cannot construct posting_url (empty url on feed item)")

    loc_parts = [loc.get("city"), loc.get("province"), loc.get("country")]
    location_raw = ", ".join(str(p).strip() for p in loc_parts if p and str(p).strip())

    deadline = raw.get("deadline_at")
    benefits = raw.get("benefits")
    if benefits and isinstance(benefits, list):
        benefits = ", ".join(str(b).strip() for b in benefits if b)

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": employer,
        "title": raw.get("title") or "",
        "location": {
            "raw": location_raw,
            "city": str(loc.get("city") or "").strip(),
            "region": str(loc.get("province") or "").strip(),
            "country": str(loc.get("country") or "").strip(),
            "remote": remote,
        },
        "posting_url": posting_url,
        "source_job_id": posting_url,
        "description": strip_html(raw.get("description")),
        "posted_date": "",
        "compensation": _compensation(raw),
        "employment_type": employment_type,
        "remote": remote,
        "liveness": {
            "source": "feed-presence",
            "deadline_at": deadline or "",
            "benefits": benefits or "",
        },
        # Presence in the feed is the only activity signal; never mark dead
        # on the absence of an explicit dead field (there is none).
        "dead": False,
    }
