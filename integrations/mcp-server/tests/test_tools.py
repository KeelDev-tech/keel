"""Tests for the Keel MCP tool modules (fixture-backed, no network except
the explicitly live-HTTP tools, which are tested only for contract shape
with a guaranteed-dead URL)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-server"))

import keel_bridge  # noqa: E402
from tools import discovery, scoring, prescreen_tool, pipeline  # noqa: E402


def test_search_roles_fixture_contract():
    r = discovery.keel_search_roles("operations")
    assert r["fixture"] is True
    assert r["count"] >= 1
    assert all("role_id" in x for x in r["roles"])


def test_search_roles_filters():
    r = discovery.keel_search_roles("", work_model="remote")
    assert all("remote" in x.get("work_model", "").lower() for x in r["roles"])


def test_search_roles_no_match():
    r = discovery.keel_search_roles("zzz-no-such-role")
    assert r["count"] == 0 and r["roles"] == []


def test_score_role_uses_real_scorer():
    role = {"title": "Ops Manager", "company": "Example Corp",
            "hard_requirements": [{"name": "3+ yrs ops", "status": "VERIFIED", "mandatory": True}]}
    out = scoring.keel_score_role(role)
    assert out["fit_score"] >= 0
    assert out["action_band"] in ("APPLY", "HOLD", "LOW-FIT")
    assert out["display_band"] in ("PRIORITY", "APPLY", "STRATEGIC", "SKIP")
    assert out["recommended_action"] in ("APPLY", "HOLD", "SKIP")


def test_score_role_missing_mandatory_caps_band():
    role = {"title": "X", "company": "Y",
            "hard_requirements": [{"name": "must have", "status": "MISSING", "mandatory": True}]}
    out = scoring.keel_score_role(role)
    assert out["fit_score"] <= 81
    assert out["action_band"] == "HOLD"
    assert not out["fit_eligible"]


def test_score_band_thresholds():
    assert scoring.keel_score_band(95)["band"] == "PRIORITY"
    assert scoring.keel_score_band(85)["band"] == "APPLY"
    assert scoring.keel_score_band(75)["band"] == "STRATEGIC"
    assert scoring.keel_score_band(10)["band"] == "SKIP"


def test_prescreen_parks_essay():
    brief = ("FORM INTEL\n"
             "- [text] Write a 500-word essay about your leadership philosophy*\n"
             "\nSTEP 3: build packet\n")
    out = prescreen_tool.keel_prescreen_packet(brief, "Example Corp")
    assert out["verdict"] == "PARK"
    assert any("essay" in r for r in out["reasons"])


def test_prescreen_clean_simple():
    brief = ("FORM INTEL\n"
             "- [dropdown] Are you authorized to work in the US?*\n"
             "\nSTEP 3: build packet\n")
    out = prescreen_tool.keel_prescreen_packet(brief, "Example Corp")
    assert out["verdict"] == "CLEAN"


def test_answer_lookup_example_only():
    out = pipeline.keel_answer_lookup("first_name")
    assert out["found"] is True and out["fixture"] is True
    assert pipeline.keel_answer_lookup("no_such_key")["found"] is False


def test_pipeline_status_contract():
    s = pipeline.keel_pipeline_status()
    assert s["fixture"] is True
    assert s["events_total"] > 0 and s["ledger_total"] > 0


def test_run_pipeline_composite():
    out = pipeline.keel_run_pipeline("operations", limit=3)
    assert out["fixture"] is True
    assert out["summary"]["discovered"] >= 1
    for s in out["stages"]:
        assert "fit_score" in s and "action_band" in s


def _supported_scoring_inputs():
    criterion = {"fact": "verified", "operator": "eq", "value": True,
                 "source_ref": "synthetic-posting:reviewed-v1"}
    role = {"role_id": "DEMO-FIT-1", "company": "Synthetic Corp", "title": "Operations",
            "requirements_reviewed": True, "requirements_source_ref": "synthetic-posting:reviewed-v1",
            "hard_requirements": [dict(criterion, id="mandatory", mandatory=True)],
            "scoring_criteria": {key: [dict(criterion)] for key in keel_bridge.score_mod.COMPONENTS
                                 if key != "hard_requirements"}}
    profile = {"scoring_facts": {"verified": {"value": True, "evidence_refs": ["synthetic-profile:reviewed-v1"]}}}
    return role, profile


def test_pipeline_high_fit_reaches_prescreen_without_authorizing_execution(monkeypatch):
    role, profile = _supported_scoring_inputs()
    monkeypatch.setattr(discovery, "keel_search_roles", lambda *args: {"roles": [role]})
    out = pipeline.keel_run_pipeline("operations", profile=profile)
    assert out["summary"]["apply_band"] == 1
    assert out["stages"][0]["fit_score"] == 100
    assert "prescreen" in out["stages"][0]
    assert out["execution_authorized"] is False
    assert out["stages"][0]["execution_authorized"] is False


def test_pipeline_high_fit_hold_does_not_reach_prescreen(monkeypatch):
    role, profile = _supported_scoring_inputs()
    role["holds"] = ["DO_NOT_CERTIFY"]
    monkeypatch.setattr(discovery, "keel_search_roles", lambda *args: {"roles": [role]})
    def forbidden(*args):
        raise AssertionError("held candidate must not advance")
    monkeypatch.setattr(prescreen_tool, "keel_prescreen_packet", forbidden)
    out = pipeline.keel_run_pipeline("operations", profile=profile)
    assert out["stages"][0]["display_band"] == "PRIORITY"
    assert out["stages"][0]["action_band"] == "HOLD"
    assert out["summary"]["apply_band"] == 0
    assert out["summary"]["parked_for_human"] == 1


def test_pipeline_default_placeholder_is_unknown_not_low_fit():
    out = pipeline.keel_run_pipeline("operations")
    assert out["summary"]["apply_band"] == 0
    assert all(row["action_band"] == "HOLD" and row["score_coverage_percent"] == 0 for row in out["stages"])
