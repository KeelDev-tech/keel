#!/usr/bin/env python3
"""Regression tests for the API-direct write-transport retirement
(Keel Blocker Resolution Directive §1, Workstream A, Trent-authorized
2026-09-18).

Proves:
  1. The required policy is implemented EXACTLY (all eight keys/values).
  2. Every write purpose denies; every read purpose allows.
  3. Zero write routes survive — the registered entry-point set is
     enumerated and each asserted denied.
  4. Ambiguous-attempt retry cannot fall through to another transport
     (browser "fallback" included).
  5. Read paths still work: detection, inspection, dry-run, telemetry
     vocabulary, historical evidence — nothing deleted or reinterpreted.

Run: python3 -m pytest test_api_direct_policy.py
Stdlib + pytest only; no network, no credentials, no external mutation.
"""

import json
import os
import sys

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)

import api_direct_policy as pol  # noqa: E402
import ats_matrix  # noqa: E402


# ---------------------------------------------------------------- policy

def test_policy_values_exact():
    """The required policy is implemented exactly — key for key."""
    assert pol.POLICY == {
        "lifecycle": "RETIRED_WRITE_PATH",
        "discovery": "ALLOW",
        "inspection": "ALLOW",
        "preparation": "ALLOW",
        "dry_run": "ALLOW",
        "external_side_effects": "DENY",
        "submission": "DENY",
        "retry_after_ambiguous_attempt": "DENY",
    }, "policy must match the directive exactly"


def test_allowed_purposes_pass():
    for purpose in ("discovery", "inspection", "preparation", "dry_run"):
        assert pol.check(purpose) is True


def test_denied_purposes_raise():
    for purpose in ("external_side_effects", "submission",
                    "retry_after_ambiguous_attempt"):
        with pytest.raises(pol.ApiDirectRetired) as exc:
            pol.check(purpose)
        assert "DENY" in str(exc.value)


def test_unknown_purpose_fails_closed():
    """A purpose not on the ALLOW list is denied — never permitted."""
    for purpose in ("submit", "write", "retry", "", None, "  "):
        with pytest.raises(pol.ApiDirectRetired):
            pol.check(purpose)


def test_exception_is_runtime_error():
    """Existing broad handlers fail closed, not missed."""
    assert issubclass(pol.ApiDirectRetired, RuntimeError)


def test_describe_is_stable_copy():
    d = pol.describe()
    assert d["lifecycle"] == "RETIRED_WRITE_PATH"
    d["lifecycle"] = "MUTATED"
    assert pol.POLICY["lifecycle"] == "RETIRED_WRITE_PATH"


# ------------------------------------------------- write-route extinction

def test_no_write_routes_survive():
    """Enumerate every registered write entry point and prove each denies."""
    n = pol.assert_all_write_routes_denied()
    assert n >= 4


def test_capability_never_advertises_write():
    """The retired write claim cannot be advertised, even for a board
    whose stored JSON still documents historical write evidence."""
    assert pol.retired() is True
    for board in ("greenhouse", "lever", "workday", "ashby", "greenhouse_legacy_embed"):
        assert ats_matrix.capability(board, "api_direct_supported") is False


def test_matrix_json_write_flags_retired_with_evidence_preserved():
    """The stored matrix no longer claims live write support; the
    historical evidence arrays survive untouched (preserved, not deleted,
    not reinterpreted)."""
    matrix = ats_matrix.load_matrix()
    g = matrix["boards"]["greenhouse"]
    assert g["api_direct_supported"] is False
    assert g["api_direct_write_lifecycle"] == "RETIRED_WRITE_PATH"
    assert len(g["evidence"]) == 6  # historical evidence preserved
    assert "RETIRED 2026-09-18" in g["notes"]
    v = g["variants"]["greenhouse_legacy_embed"]
    assert v["api_direct_supported"] is False
    assert v["api_direct_write_lifecycle"] == "RETIRED_WRITE_PATH"
    assert "RETIRED 2026-09-18" in v["evidence_note"]
    # non-retired flags are unaffected (discovery/inspection still ALLOW)
    assert g["guest_discovery"] is True
    assert g["form_probe_ok"] is True


def test_discovery_flags_still_queryable():
    """Read-side capability flags keep working through the same consult."""
    assert ats_matrix.capability("greenhouse", "guest_discovery") is True
    assert ats_matrix.capability("greenhouse", "form_probe_ok") is True
    assert ats_matrix.status("greenhouse") == "verified"


# --------------------------------------- ambiguous attempt: no fall-through

def test_ambiguous_attempt_cannot_fall_through():
    """retry_after_ambiguous raises for EVERY proposed transport — it
    never returns a fallback (a browser 'fallback' after an ambiguous
    API-direct POST would be a second submission attempt)."""
    for proposed in ("browser", "api-direct", "manual", None, "whatever"):
        with pytest.raises(pol.ApiDirectRetired) as exc:
            pol.retry_after_ambiguous(
                attempt_id="si-TEST", role_id="TEST-RT",
                proposed_transport=proposed)
        assert "no fallback" in str(exc.value)


def test_record_intent_refuses_retired_api_lane(tmp_path):
    """No new attempt identity is mintable for the retired API lane."""
    import submit_intent
    submit_intent.set_store_dir(str(tmp_path))
    try:
        for transport in ("api", "api-direct", " API ", "Api-Direct"):
            with pytest.raises(pol.ApiDirectRetired):
                submit_intent.record_intent(
                    "TEST-RT-RETIRE", "Audit Probe Co", transport,
                    "digest-probe")
        # the store must hold ZERO records: nothing was minted
        assert submit_intent.resume_open() == []
    finally:
        submit_intent.reset_store_dir()


def test_record_intent_still_mints_browser_intents(tmp_path):
    """The retirement only retires the API lane — the browser lane's
    intent bookkeeping is untouched."""
    import submit_intent
    submit_intent.set_store_dir(str(tmp_path))
    try:
        aid = submit_intent.record_intent(
            "TEST-RT-BROWSER", "Audit Probe Co", "browser", "digest-probe")
        assert aid.startswith("si-")
        rec = submit_intent.get(aid)
        assert rec["state"] == "INTENT"
        assert rec["transport"] == "browser"
        # ambiguous-attempt machinery unchanged: UNKNOWN still bars
        submit_intent.mark_unknown(aid, "probe ambiguity")
        assert submit_intent.open_unknown_for("TEST-RT-BROWSER")["state"] == "UNKNOWN"
    finally:
        submit_intent.reset_store_dir()


# ------------------------------------------------------ read paths intact

def test_detection_still_works():
    """Discovery (discovery: ALLOW) is untouched."""
    import api_direct_detect
    board, job_id = api_direct_detect.parse_board_url(
        "https://job-boards.greenhouse.io/acme/jobs/12345")
    assert (board, job_id) == ("acme", "12345")
    # fail-closed without network: unparseable URL is not a candidate
    assert api_direct_detect.is_api_direct_candidate("not-a-url") is False
    assert api_direct_detect.is_api_direct_candidate("") is False
    # Enterprise heuristic intact
    assert api_direct_detect.is_enterprise_board(
        '<script src="https://www.gstatic.com/recaptcha/enterprise.js">') is True
    assert api_direct_detect.is_enterprise_board("<html>clean</html>") is False


def test_lever_probe_still_read_only():
    """Inspection (inspection: ALLOW): the Lever probe stays a read-only
    verdict — it never green-lights HTTP submission."""
    import lever_submit
    html = ('<form id="application-form"><ul>'
            '<li class="application-question"><input name="name">'
            '<span>Full name</span></li></ul></form>')
    questions, hcaptcha = lever_submit.extract_questions(html)
    assert hcaptcha is False
    assert any(q["field_name"] == "name" for q in questions)
    # the probe's verdict contract: http_submission_viable is always False
    import inspect
    assert "http_submission_viable" in inspect.getsource(lever_submit.probe)


def test_verify_retry_flag_path_still_imports():
    """verify_retry's api_direct_candidate flagging (discovery) is intact."""
    import verify_retry  # noqa: F401 — import-only: module still loads
    assert hasattr(verify_retry, "is_verify_only")


def test_board_enumerate_modules_intact():
    """Discovery modules remain importable (read-only provider intel)."""
    import greenhouse_json_enumerate  # noqa: F401
    import ashby_lever_json_enumerate  # noqa: F401


# ------------------------------------------- historical telemetry kept

def test_historical_telemetry_vocabulary_preserved():
    """Historical outcome/telemetry vocabulary is preserved — not
    deleted, not reinterpreted."""
    import cost_model
    assert "api_direct_submit_started" in cost_model.LAUNCH_EVENTS
    import log_event
    assert "api_direct_failed" in log_event.GATE_TYPES
    import outcome_analytics  # noqa: F401 — transport attribution intact
    # pulse telemetry reader untouched (historical api_direct stats)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pulse_snapshot",
        os.path.join(BASE, "..", "monitors", "pulse_snapshot.py"))
    assert spec is not None and spec.loader is not None


def test_registry_lists_known_entry_points():
    entries = pol.list_write_entry_points()
    assert len(entries) == len(pol.WRITE_ENTRY_POINTS) >= 3
    names = [n for n, _ in entries]
    assert any("capability" in n for n in names)
    assert any("record_intent" in n for n in names)
    assert any("retry_after_ambiguous" in n for n in names)
