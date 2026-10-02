"""Tests for the Keel MCP tool modules (fixture-backed, no network except
the explicitly live-HTTP tools, which are tested only for contract shape
with a guaranteed-dead URL)."""
import sys

import pytest
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


def test_prescreen_brief_only_cannot_claim_complete_screen():
    brief = ("FORM INTEL\n"
             "- [dropdown] Are you authorized to work in the US?*\n"
             "\nSTEP 3: build packet\n")
    out = prescreen_tool.keel_prescreen_packet(brief, "Example Corp")
    assert out["verdict"] == "PARK"
    assert any("Form-question evidence" in reason for reason in out["reasons"])
    assert any("Posting-text evidence" in reason for reason in out["reasons"])


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


def _complete_packet_evidence():
    url = "https://example.invalid/jobs/synthetic"
    return {"ats_url": url, "form_intel_complete": True,
            "form_intel": {"source_url": url, "extraction_complete": True,
                           "rendered_option_fetch_needed": [], "questions": []},
            "posting_text": "Remote operations role.", "posting_text_complete": True,
            "posting_text_source": "synthetic-fixture", "posting_text_url": url}


def test_prescreen_structured_evidence_reaches_complete_diagnostic():
    out = prescreen_tool.keel_prescreen_packet("", "Synthetic Corp", _complete_packet_evidence())
    assert out["verdict"] == "CLEAN"
    assert out["answer_bank"] == "example-fixture"
    assert out["execution_authorized"] is False


def test_prescreen_structured_evidence_parks_mismatched_posting():
    evidence = _complete_packet_evidence()
    evidence["posting_text_url"] = "https://example.invalid/jobs/another"
    out = prescreen_tool.keel_prescreen_packet("", packet_evidence=evidence)
    assert out["verdict"] == "PARK"
    assert any("Posting-text evidence" in reason for reason in out["reasons"])


def test_prescreen_structured_evidence_parks_missing_dropdown_choices():
    evidence = _complete_packet_evidence()
    evidence["form_intel"]["questions"] = [
        {"label": "Work authorization", "type": "dropdown", "required": True, "options": []}]
    assert prescreen_tool.keel_prescreen_packet("", packet_evidence=evidence)["verdict"] == "PARK"


def test_prescreen_does_not_infer_completeness():
    evidence = _complete_packet_evidence()
    del evidence["form_intel_complete"]
    assert prescreen_tool.keel_prescreen_packet("", packet_evidence=evidence)["verdict"] == "PARK"


def test_prescreen_evidence_cannot_inject_writeback_identity(monkeypatch):
    evidence = _complete_packet_evidence()
    evidence.update(role_id="REAL-ROLE", file="/tmp/packet.json", company="Injected")
    observed = {}
    def screen(packet, bank, employer_patterns):
        observed.update(packet)
        return {"verdict": "CLEAN", "reasons": []}
    monkeypatch.setattr(keel_bridge.prescreen_mod, "screen_packet", screen)
    prescreen_tool.keel_prescreen_packet("brief", "Synthetic Corp", evidence)
    assert "role_id" not in observed and "file" not in observed
    assert observed["company"] == "Synthetic Corp"


def test_prescreen_malformed_evidence_stays_parked():
    out = prescreen_tool.keel_prescreen_packet("", packet_evidence=[])
    assert out["verdict"] == "PARK" and out["execution_authorized"] is False


@pytest.mark.parametrize("ats,url", [
    ("greenhouse", "https://boards.greenhouse.io/synthetic/jobs/123"),
    ("lever", "https://jobs.lever.co/synthetic/123"),
])
def test_prescreen_accepts_probe_result_without_source_url(monkeypatch, ats, url):
    from tools import honesty
    probe = {"ats": ats, "form_url": url, "extraction_complete": True,
             "rendered_option_fetch_needed": [], "questions": []}
    monkeypatch.setattr(keel_bridge.form_intel_mod, "probe_url", lambda _: probe)
    evidence = _complete_packet_evidence()
    evidence.update(ats_url=url, posting_text_url=url,
                    form_intel=honesty.keel_probe_form(url))
    out = prescreen_tool.keel_prescreen_packet("", packet_evidence=evidence)
    assert out["verdict"] == "CLEAN"
    assert out["execution_authorized"] is False
    assert "source_url" not in probe
    assert "source_url" not in evidence["form_intel"]


@pytest.mark.parametrize("source_url", ["https://example.invalid/jobs/another", "", None])
def test_prescreen_preserves_explicit_invalid_form_binding(source_url):
    evidence = _complete_packet_evidence()
    evidence["form_intel"]["source_url"] = source_url
    out = prescreen_tool.keel_prescreen_packet("", packet_evidence=evidence)
    assert out["verdict"] == "PARK"
    assert out["execution_authorized"] is False
    assert evidence["form_intel"]["source_url"] == source_url


@pytest.mark.parametrize('state', ['supported', 'unknown', 'held'])
def test_pipeline_preserves_exact_scoring_evidence(monkeypatch, state):
    role, profile = _supported_scoring_inputs()
    if state == 'unknown':
        profile = {}
    elif state == 'held':
        role['holds'] = ['synthetic-review-required']
    expected = scoring.keel_score_role(role, profile)
    monkeypatch.setattr(discovery, 'keel_search_roles', lambda *args: {'roles': [role]})
    out = pipeline.keel_run_pipeline('operations', profile=profile)
    row = out['stages'][0]
    for field in ('score_evidence', 'score_breakdown', 'score_bounds'):
        assert row[field] == expected[field]
    assert row['action_band'] == expected['action_band']
    assert row['execution_authorized'] is out['execution_authorized'] is False
    if state != 'supported':
        assert 'prescreen' not in row
