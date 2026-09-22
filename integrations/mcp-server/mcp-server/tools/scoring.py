"""Scoring tools: thin wrappers over Keel's real fit scorer.

score_roles.score_role is Keel's production hard-requirement classifier:
it deducts for missing/partial/unknown hard requirements and caps the band
when a mandatory requirement is clearly missing. band() maps 90/82/72 to
PRIORITY / APPLY / STRATEGIC / SKIP.
"""
from __future__ import annotations

import keel_bridge


def keel_score_role(role: dict, profile: dict | None = None) -> dict:
    """Score a role dict against an applicant profile with Keel's fit scorer.

    Args:
        role: role dict; meaningful keys are title, company, and
            hard_requirements (list of {name, status, mandatory} where status
            is VERIFIED | PARTIAL | UNKNOWN | MISSING).
        profile: applicant profile dict. Defaults to the bundled EXAMPLE
            profile fixture — pass your own to score for real.
    Returns the scored role including fit_score, score_breakdown,
    action_band, and recommended_action.
    """
    if profile is None:
        profile = keel_bridge.load_fixture("applicant_profile.example.json")
    result = keel_bridge.score_mod.score_role(role, profile)
    result["profile_fixture"] = profile is not None and profile.get("_meta", {}).get("note", "").startswith("EXAMPLE")
    return result


def keel_score_band(score: float) -> dict:
    """Map a numeric fit score to Keel's action band (PRIORITY/APPLY/STRATEGIC/SKIP)."""
    return {"score": score, "band": keel_bridge.score_mod.band(score)}
