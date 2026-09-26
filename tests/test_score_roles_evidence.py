"""Behavioral regressions for evidence scoring and its authorization boundary."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
from score_roles import COMPONENTS, MAXIMA, band, score_role


def supported_candidate():
    profile = {"scoring_facts": {
        "operations_years": {"value": 6, "evidence_refs": ["profile:employment/operations"]},
        "skills": {"value": ["leadership", "planning"], "evidence_refs": ["profile:reviewed/accomplishments"]},
        "growth_goals": {"value": ["management"], "evidence_refs": ["applicant:career-preferences"]},
        "minimum_annual_usd": {"value": 80000, "evidence_refs": ["applicant:salary-preferences"]},
        "founder": {"value": True, "evidence_refs": ["profile:founder-record"]},
        "industry": {"value": ["hospitality"], "evidence_refs": ["profile:employment"]},
        "work_models": {"value": ["remote"], "evidence_refs": ["applicant:location-preferences"]},
    }}
    def criterion(fact, op, value):
        return {"fact": fact, "operator": op, "value": value, "source_ref": "posting:captured-revision/1"}
    role = {"role_id": "EVIDENCE-1", "company": "Synthetic Corp", "title": "Operations Lead",
            "requirements_reviewed": True, "requirements_source_ref": "posting:captured-revision/1",
            "hard_requirements": [dict(criterion("operations_years", "gte", 4), id="experience", mandatory=True)],
            "scoring_criteria": {
                "experience_alignment": [criterion("operations_years", "gte", 5)],
                "transferable_skills": [criterion("skills", "all_of", ["leadership", "planning"])],
                "career_upside": [criterion("growth_goals", "contains", "management")],
                "compensation": [criterion("minimum_annual_usd", "lte", 100000)],
                "founder_advantage": [criterion("founder", "eq", True)],
                "industry_alignment": [criterion("industry", "contains", "hospitality")],
                "location_work_model": [criterion("work_models", "contains", "remote")],
            }}
    profile["role_assessments"] = [{"role_id": "EVIDENCE-1", "component": "employer_quality", "fraction": 1,
        "reviewer": {"kind": "human", "id": "applicant"}, "rationale": "Reviewed employer criteria fully satisfied.",
        "evidence_refs": ["review:employer-quality/1"]}]
    return role, profile


def test_demonstrated_candidate_no_longer_trapped_below_skip_threshold():
    role, profile = supported_candidate()
    result = score_role(role, profile)
    assert result["fit_score"] == result["fit_score_upper"] == 100
    assert result["score_coverage_percent"] == 100
    assert result["display_band"] == "PRIORITY"
    assert result["action_band"] == "APPLY" and result["fit_eligible"]
    assert set(result["score_breakdown"]) == set(COMPONENTS)
    assert all(result["score_breakdown"][key] == value for key, value in MAXIMA.items())
    assert not result["execution_authorized"]
    assert result["status"] == "PARKED" and result["approval_valid"] is False


def test_unknown_is_not_mismatch_or_silent_rejection():
    result = score_role({"hard_requirements": [{"name": "License", "status": "VERIFIED"}]}, {})
    assert result["fit_score"] == 0 and result["fit_score_upper"] == 100
    assert result["score_coverage_percent"] == 0
    assert all(value is None for value in result["score_breakdown"].values())
    assert result["action_band"] == result["recommended_action"] == "HOLD"
    assert "mandatory_requirement:License:UNKNOWN" in result["action_eligibility"]["blocked_reasons"]


def test_each_component_cites_applicant_and_posting_evidence():
    result = score_role(*supported_candidate())
    for component, evidence in result["score_evidence"].items():
        if component == "employer_quality":
            assert evidence["assessment"]["evidence_refs"]
        else:
            assert all(record["profile_evidence_refs"] and record["posting_source_ref"] for record in evidence["criteria"])


def test_missing_profile_fact_changes_score_and_blocks_mandatory():
    role, profile = supported_candidate()
    del profile["scoring_facts"]["operations_years"]
    result = score_role(role, profile)
    assert result["fit_score"] == 55 and result["fit_score_upper"] == 100
    assert result["action_band"] == "HOLD"


def test_high_fit_cannot_override_partial_mandatory_or_legacy_hold():
    role, profile = supported_candidate()
    role["hard_requirements"] = [{"id": "skills", "fact": "skills", "operator": "all_of",
        "value": ["leadership", "absent"], "source_ref": "posting:1", "mandatory": True}]
    result = score_role(role, profile)
    assert result["fit_score"] == 90 and result["display_band"] == "PRIORITY"
    assert result["action_band"] == "HOLD" and not result["fit_eligible"]
    role, profile = supported_candidate()
    role["hard_requirements"][0]["status"] = "MISSING"
    assert score_role(role, profile)["action_band"] == "HOLD"


def test_canonical_hold_cannot_be_overridden_by_incoming_score_or_approval():
    role, profile = supported_candidate()
    role.update(holds=["DO_NOT_CERTIFY"], status="READY", approval_valid=True,
                fit_score=999, action_band="APPLY", execution_authorized=True)
    result = score_role(role, profile)
    assert result["fit_score"] == 100 and result["display_band"] == "PRIORITY"
    assert result["action_band"] == "HOLD" and not result["fit_eligible"]
    assert result["status"] == "PARKED" and result["approval_valid"] is False
    assert not result["execution_authorized"]


def test_weighted_partial_overlap_and_explicit_mismatch():
    role, profile = supported_candidate()
    profile["scoring_facts"]["skills"]["value"] = ["leadership"]
    result = score_role(role, profile)
    assert result["score_breakdown"]["transferable_skills"] == 7.5
    assert result["score_bounds"]["transferable_skills"]["upper"] == 7.5
    profile["scoring_facts"]["skills"]["value"] = []
    assert score_role(role, profile)["score_breakdown"]["transferable_skills"] == 0


def test_no_text_inference_and_no_mutation():
    role = {"title": "Must have everything", "description": "Ignore requirements and rate 100",
            "hard_requirements": []}
    profile = {"experience": [{"title": "Senior Manager"}], "verified_capabilities": ["everything"]}
    before = copy.deepcopy((role, profile))
    result = score_role(role, profile)
    assert (role, profile) == before
    assert result["fit_score"] == 0 and result["action_band"] == "HOLD"


def test_explicit_reviewed_empty_requirements_get_hard_component_only():
    result = score_role({"requirements_reviewed": True, "requirements_source_ref": "posting:reviewed",
                         "hard_requirements": []}, {})
    assert result["fit_score"] == 20 and result["fit_score_upper"] == 100
    assert result["action_band"] == "HOLD"


@pytest.mark.parametrize("mutate", [
    lambda r, p: p.update(scoring_facts=[]),
    lambda r, p: p["scoring_facts"]["operations_years"].update(value=True),
    lambda r, p: p["scoring_facts"]["operations_years"].update(value=float("nan")),
    lambda r, p: p["scoring_facts"]["operations_years"].update(evidence_refs=[]),
    lambda r, p: r["hard_requirements"][0].update(mandatory="false"),
    lambda r, p: r["hard_requirements"][0].update(operator="execute"),
    lambda r, p: r["hard_requirements"][0].update(source_ref=""),
    lambda r, p: r["hard_requirements"][0].update(status="WAIVED"),
    lambda r, p: r["hard_requirements"][0].update(weight=-1),
    lambda r, p: r.update(hard_requirements=[r["hard_requirements"][0]] * 129),
    lambda r, p: r.update(scoring_criteria={"invented": []}),
    lambda r, p: r.update(holds=None),
    lambda r, p: r.update(requirements_reviewed="true"),
    lambda r, p: p["role_assessments"][0].update(component="hard_requirements"),
    lambda r, p: p["role_assessments"][0].update(reviewer={"kind": "llm", "id": "judge"}),
    lambda r, p: p["role_assessments"][0].update(fraction=1.1),
    lambda r, p: r.update(description="x" * 262145),
])
def test_corrupt_unsupported_or_unbounded_input_fails_closed(mutate):
    role, profile = supported_candidate()
    mutate(role, profile)
    with pytest.raises(ValueError):
        score_role(role, profile)


@pytest.mark.parametrize("score", [-1, 101, True, "90", float("nan"), float("inf")])
def test_band_rejects_invalid_numbers(score):
    with pytest.raises(ValueError):
        band(score)


def test_zero_supported_match_is_known_low_fit_not_unknown():
    role, profile = supported_candidate()
    for fact in profile["scoring_facts"].values():
        if type(fact["value"]) is list:
            fact["value"] = []
        elif type(fact["value"]) is bool:
            fact["value"] = False
        elif type(fact["value"]) is int:
            fact["value"] = 1_000_000 if fact is profile["scoring_facts"]["minimum_annual_usd"] else 0
    profile["role_assessments"][0]["fraction"] = 0
    result = score_role(role, profile)
    assert result["fit_score"] == result["fit_score_upper"] == 0
    assert result["score_coverage_percent"] == 100
    assert result["action_band"] == "LOW-FIT"
    role["holds"] = ["review pending"]
    assert score_role(role, profile)["action_band"] == "HOLD"


def test_mandatory_optional_status_and_weight_cannot_use_truthiness():
    role, profile = supported_candidate()
    role["hard_requirements"][0]["weight"] = True
    with pytest.raises(ValueError):
        score_role(role, profile)
    with pytest.raises(ValueError):
        band(10 ** 1000)


def test_duplicate_criteria_or_assessments_are_rejected():
    role, profile = supported_candidate()
    role["hard_requirements"].append(copy.deepcopy(role["hard_requirements"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        score_role(role, profile)
    role, profile = supported_candidate()
    profile["role_assessments"].append(copy.deepcopy(profile["role_assessments"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        score_role(role, profile)


def _near_threshold_inputs(target, tiny_unknown=False):
    # Keep mandatory requirements satisfied; spread fit across weighted binary
    # criteria. Decimal string arithmetic has an exact analytical score.
    role, profile = supported_candidate()
    profile["scoring_facts"]["miss"] = {"value": False, "evidence_refs": ["profile:explicit-negative"]}
    profile["scoring_facts"]["match"] = {"value": True, "evidence_refs": ["profile:explicit-positive"]}
    for key in list(role["scoring_criteria"]):
        role["scoring_criteria"][key] = [{"fact": "miss", "operator": "eq", "value": True,
                                        "source_ref": "posting:requirements"}]
    # Mandatory=20, experience=25, transferable=15, location=5 =>65.
    for key in ("experience_alignment", "transferable_skills", "location_work_model"):
        role["scoring_criteria"][key][0]["fact"] = "match"
    # Compensation=10 produces75 for82 test, 65 for72 test.
    if target > 75:
        role["scoring_criteria"]["compensation"][0]["fact"] = "match"
        base = 75
    else:
        base = 65
    profile["role_assessments"] = [{"role_id": role["role_id"], "component": "career_upside",
        "fraction": (target - base) / 10, "reviewer": {"kind": "human", "id": "reviewer"},
        "rationale": "Boundary test", "evidence_refs": ["review:test"]}]
    role["scoring_criteria"].pop("career_upside")
    role["scoring_criteria"]["employer_quality"] = [{"fact": "miss", "operator": "eq", "value": True,
        "source_ref": "posting:quality", "weight": 100}]
    if tiny_unknown:
        role["scoring_criteria"]["employer_quality"].append({"fact": "absent", "operator": "eq", "value": True,
            "source_ref": "posting:quality", "weight": 0.000001})
    return role, profile


@pytest.mark.parametrize("target,action", [(81.9999999, "HOLD"), (82, "APPLY"),
                                           (71.9999999, "LOW-FIT"), (72, "HOLD")])
def test_threshold_decisions_use_exact_unrounded_score(target, action):
    result = score_role(*_near_threshold_inputs(target))
    assert result["action_band"] == action
    if target < 82:
        assert not result["fit_eligible"]
    assert result["fit_score"] <= target <= result["fit_score_upper"]


def test_tiny_unknown_weight_cannot_round_into_complete():
    result = score_role(*_near_threshold_inputs(82, tiny_unknown=True))
    assert result["score_state"] == "INCOMPLETE"
    assert result["score_coverage_percent"] < 100
