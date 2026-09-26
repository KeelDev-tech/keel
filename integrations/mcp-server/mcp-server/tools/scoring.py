"""Read-only evidence-based fit scoring; no queue writes or launch permission."""
from __future__ import annotations

import keel_bridge


def keel_score_role(role: dict, profile: dict | None = None) -> dict:
    """Assess explicit applicant facts against posting criteria.

    Pass a trusted profile with scoring_facts (value + evidence_refs) and a
    role with scoring_criteria plus reviewed hard_requirements. See
    docs/SCORING_CONTRACT.md. Missing facts remain UNKNOWN. The default
    EXAMPLE profile has no qualifying evidence and cannot establish fit.
    display_band is a rank; action_band is APPLY/HOLD/LOW-FIT. Even APPLY
    means preparation eligibility only; execution_authorized is always false.
    """
    default_fixture = profile is None
    if default_fixture:
        profile = keel_bridge.load_fixture("applicant_profile.example.json")
    result = keel_bridge.score_mod.score_role(role, profile)
    meta = profile.get("_meta")
    result["profile_fixture"] = default_fixture or (
        type(meta) is dict and type(meta.get("note")) is str and meta["note"].startswith("EXAMPLE"))
    return result


def keel_score_band(score: float) -> dict:
    """Map finite 0–100 scores to display bands; this grants no action permission."""
    return {"score": score, "band": keel_bridge.score_mod.band(score), "execution_authorized": False}
