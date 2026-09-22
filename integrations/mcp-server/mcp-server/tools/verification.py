"""Verification + ATS-intel tools: live, read-only HTTP against public postings.

These wrap Keel's production verification path (verify_retry.check_live and
ats.preflight): resolve redirect chains, detect the ATS, and check liveness
via public ATS APIs / posting-page markers. HTTP GET only — nothing is
submitted, no accounts are created.
"""
from __future__ import annotations

import keel_bridge


def keel_verify_posting(url: str, title_hint: str = "") -> dict:
    """Check whether a job posting URL is still live.

    Conservative by design: "dead" only comes from an explicit signal
    (ATS API 404/410, dead markers on the page); "live" needs an ATS API
    job record or strong apply markers. Anything else is "ambiguous".
    Args:
        url: the posting URL to check.
        title_hint: optional job title to match against the live record.
    """
    verdict, detail = keel_bridge.verify_mod.check_live(url, title_hint)
    return {"url": url, "verdict": verdict, "detail": detail}


def keel_ats_intel(url: str) -> dict:
    """Detect the ATS behind a posting URL and pull public job intelligence.

    Resolves redirect chains, detects Greenhouse / Lever / Ashby (or
    "unknown"), and fetches the public job record where the ATS API allows.
    Never raises on unknown ATS — degrades to {"ats": "unknown"}.
    """
    return keel_bridge.ats_mod.preflight(url)
