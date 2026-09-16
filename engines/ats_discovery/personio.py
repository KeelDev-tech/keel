"""Personio discovery adapter (MARGINAL platform).

Board endpoint: https://{slug}.jobs.personio.com/xml
Shape: XML <workzag-jobs><position>... (NOT the Google-Jobs XML format).
Position fields: id, name, department, recruitingCategory, employmentType,
seniority, schedule, yearsOfExperience, occupation, occupationCategory,
createdAt, jobDescriptions.

MARGINAL: two mandatory guards —
  GUARD 1: zero <position> nodes -> SKIP_LOG "empty-feed-opt-out", [].
            (Validated: some tenants with live jobs return an EMPTY feed —
            per-tenant opt-in.)
  GUARD 2: ANY position description contains "lorem ipsum"
            (case-insensitive) -> SKIP_LOG "test-data-feed", [] (whole feed
            suspect; validated: a feed carried 2017 test garbage).
XML parse failure -> SKIP_LOG "xml-parse-failure", [].

No location is present in position nodes; no compensation is in the feed.

Live-validated 2026-09-15.
"""

import html
import re
import xml.etree.ElementTree as ET

try:
    from ats_discovery.fetcher import RateLimited
except ImportError:  # ats_discovery/http.py ships separately; local fail-safe
    class RateLimited(Exception):
        """Raised when the board endpoint answers HTTP 429."""

PLATFORM = "personio"
ATS_LABEL = "Personio"
ENRICH_DETAILS = False
SKIP_LOG = []


def board_list_url(slug: str) -> str:
    return "https://%s.jobs.personio.com/xml" % slug


# ---------------------------------------------------------------- helpers

def _strip_markup(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _text(node, tag):
    child = node.find(tag)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _iso_date(value):
    if value is None or value == "":
        return ""
    s = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


_FIELDS = ("id", "name", "department", "recruitingCategory", "employmentType",
           "seniority", "schedule", "yearsOfExperience", "occupation",
           "occupationCategory", "createdAt", "jobDescriptions")


# ---------------------------------------------------------------- fetch

def fetch_board(slug, http_get):
    """Return raw position dicts. 429 -> RateLimited; non-200 -> [];
    guard failures -> skip-log + []."""
    url = board_list_url(slug)
    status, _content_type, body = http_get(url)
    if status == 429:
        raise RateLimited("personio board %s rate limited" % slug)
    if status != 200:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        SKIP_LOG.append({"slug": slug, "reason": "xml-parse-failure"})
        return []
    positions = root.findall(".//position")
    # GUARD 1: empty feed -> per-tenant opt-out, not a dead board; skip.
    if not positions:
        SKIP_LOG.append({"slug": slug, "reason": "empty-feed-opt-out"})
        return []
    raw_positions = []
    for pos in positions:
        raw = {f: _text(pos, f) for f in _FIELDS}
        raw_positions.append(raw)
    # GUARD 2: test-data feed -> the whole feed is suspect; drop it.
    for raw in raw_positions:
        if "lorem ipsum" in raw.get("jobDescriptions", "").lower():
            SKIP_LOG.append({"slug": slug, "reason": "test-data-feed"})
            return []
    return raw_positions


# ---------------------------------------------------------------- normalize

def normalize(raw, slug, employer):
    """Map one raw position dict to the contract shape."""
    job_id = raw.get("id") or ""
    if not job_id:
        raise ValueError("cannot construct posting_url: position has no id")
    posting_url = "https://%s.jobs.personio.com/job/%s" % (slug, job_id)

    return {
        "platform": PLATFORM,
        "ats_label": ATS_LABEL,
        "slug": slug,
        "employer": employer,
        "company": employer,  # feed carries no company field
        "title": raw.get("name") or "",
        "location": {
            "raw": "",
            "city": "",
            "region": "",
            "country": "",
            "remote": None,
        },
        "posting_url": posting_url,
        "source_job_id": str(job_id),
        "description": _strip_markup(raw.get("jobDescriptions")),
        "posted_date": _iso_date(raw.get("createdAt")),
        "compensation": "",
        "employment_type": raw.get("employmentType") or "",
        "remote": None,
        "liveness": {"source": "xml-feed"},
        "dead": False,  # no explicit dead signal is available from this feed
    }
