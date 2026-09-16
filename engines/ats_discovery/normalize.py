"""Shared normalization helpers for ats_discovery adapters.

Per the deep-dive plan: source job ID, canonical URL, normalized
company/title/location, description similarity as final dedupe fallback.
"""

import hashlib
import html
import re

_WS = re.compile(r"\s+")


def strip_html(raw):
    """HTML -> plain text. Never raises on weird input."""
    if not raw:
        return ""
    s = str(raw)
    s = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", s,
               flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def norm_text(s):
    return _WS.sub(" ", (s or "").strip())


def norm_company(s):
    return norm_text(s)


def norm_title(s):
    t = norm_text(s)
    # drop trailing seniority/location noise commonly appended in feeds
    t = re.sub(r"\s*[\(\[]\s*(remote|hybrid|onsite)[\s\-–]*[^\)\]]*[\)\]]\s*$",
               "", t, flags=re.I)
    return t.strip()


def description_hash(description):
    """Final-fallback dedupe key: sha256 of aggressively normalized text.

    Lowercased, punctuation stripped, whitespace collapsed — catches
    reposted roles whose URL and title drifted.
    """
    s = (description or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = _WS.sub(" ", s).strip()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def make_role_id(platform, employer, title, source_job_id, date_tag):
    """UPPER-KEBAB role_id: ATS8-<PLATFORM>-<EMPLOYER>-<TITLEWORDS>-<DATE>.

    source_job_id is tenant-scoped on some platforms, so it is NOT part of
    the role_id; the queue dedupe layer keys on posting_url + description
    hash separately.
    """
    def words(s, n):
        toks = re.sub(r"[^A-Za-z0-9 ]", "", (s or "").upper()).split()
        return "-".join(toks[:n]) or "NA"

    rid = "-".join(["ATS8", platform.upper(), words(employer, 3),
                    words(title, 4), date_tag])
    rid = re.sub(r"-{2,}", "-", rid).strip("-")
    if not re.match(r"^[A-Z0-9][A-Z0-9\-]*$", rid):
        rid = re.sub(r"[^A-Z0-9\-]", "", rid) or "ATS8-UNKNOWN"
    return rid[:90]


def comp_str(*parts):
    """Join non-empty compensation parts defensibly."""
    return " | ".join(norm_text(p) for p in parts if norm_text(p))
