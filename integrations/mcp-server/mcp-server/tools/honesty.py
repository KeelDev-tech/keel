"""Form-intel + truthfulness tools.

keel_probe_form wraps Keel's production HTTP form extractor
(form_intel.probe_url): Greenhouse embed API / Lever postings API, graceful
empty dict for unknown ATS. Read-only HTTP.

keel_truthfulness_check wraps resume_tailor.truthfulness_check: given a
caller-supplied profile and role, it lists hard requirements the profile
cannot support. The tailor REFUSES to invent bridging copy — a gap is
information (skip the role, or supply real evidence), never embellishment.
This is the "refuses to lie" guarantee as a tool.
"""
from __future__ import annotations

import keel_bridge


def keel_probe_form(url: str) -> dict:
    """Extract a job application's form questions over HTTP.

    Returns {"ats", "questions": [{label, options}], "form_url"}.
    Unknown ATS degrades gracefully to an empty intel dict.
    Args:
        url: the application/posting URL to probe.
    """
    return keel_bridge.form_intel_mod.probe_url(url)


def keel_truthfulness_check(profile: dict, role: dict) -> dict:
    """List role requirements the given profile cannot honestly support.

    Pure function on caller-supplied data — no profiles are read from disk.
    Args:
        profile: applicant profile dict; meaningful key is
            verified_capabilities (list of capability names).
        role: role dict; meaningful key is hard_requirements
            (list of {name, status, mandatory}).
    Returns {"gaps": [...], "gap_count": n}. An empty gap list means every
    hard requirement is either verified/partial or explicitly waived — it is
    NOT a claim the applicant should apply.
    """
    gaps = keel_bridge.tailor_mod.truthfulness_check(profile or {}, role or {})
    return {"gaps": gaps, "gap_count": len(gaps)}
