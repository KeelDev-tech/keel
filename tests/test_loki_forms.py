import copy
import json
import os

import pytest

from keel_loki.forms import (FormError, bind_fixture, build_plan, demo_fixture, digest, expected_readback,
                             inventory_blockers, validate_contract, validate_plan, validate_readback)
from keel_loki.browser import fixture_html, run_fixture


def prepared():
    f = demo_fixture()
    return f, build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])


def test_complete_typed_fixture_covers_all_fields():
    f, plan = prepared()
    assert len(plan["fields"]) == 12
    assert sum(row["state"] == "prepared" for row in plan["fields"]) == 11
    assert {row["kind"] for row in plan["fields"]} == {"text", "select", "radio", "checkbox", "attestation", "attachment"}
    assert next(row for row in plan["fields"] if row["field_id"] == "onsite_city")["state"] == "inactive"
    assert validate_readback(f["contract"], plan, expected_readback(f["contract"], plan))["status"] == "MATCH"
    assert validate_readback(f["contract"], plan, expected_readback(f["contract"], plan))["rendered_browser_validated"] is False


def test_missing_values_reports_every_unresolved_field():
    f = demo_fixture()
    report = inventory_blockers(f["contract"], {}, [], now=f["now"])
    assert report["status"] == "BLOCKED"
    assert len(report["fields"]) == 12
    assert report["blocked_field_count"] == 12
    assert [row["reason"] for row in report["fields"]].count("condition_unresolved") == 2
    with pytest.raises(FormError):
        build_plan(f["contract"], {}, [], now=f["now"])


def test_diagnostic_complete_and_extra_fields():
    f = demo_fixture()
    assert inventory_blockers(f["contract"], f["values"], f["approvals"], now=f["now"])["status"] == "COMPLETE"
    f["values"]["submit"] = {"value": True}
    report = inventory_blockers(f["contract"], f["values"], f["approvals"], now=f["now"])
    assert report["status"] == "BLOCKED" and report["unknown_field_ids"] == ["submit"]


@pytest.mark.parametrize("field,value", [("recording", 0), ("accuracy", 1), ("region", "Unknown"),
                                          ("work_mode", "unknown"), ("full_name", ""), ("accuracy", False)])
def test_invalid_typed_values_rejected_even_with_matching_approval(field, value):
    f = demo_fixture()
    f["plain_values"][field] = value
    f = bind_fixture(f, now=f["now"])
    with pytest.raises(FormError):
        build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])


@pytest.mark.parametrize("change", ["value", "approval", "scope", "evidence", "actor", "expired", "revoked", "future"])
def test_values_cannot_authorize_themselves(change):
    f = demo_fixture()
    if change == "value":
        f["values"]["full_name"]["value"] = "Invented Person"
    elif change == "approval":
        f["values"]["full_name"]["approval_id"] = "unknown"
    elif change == "scope":
        f["contract"]["account_id"] = "other_account"
    elif change == "evidence":
        f["values"]["full_name"]["evidence_sha256"] = ["0" * 64]
    elif change == "actor":
        f["approvals"][0]["actor_kind"] = "model"
    elif change == "expired":
        f["approvals"][0]["expires_at"] = f["now"]
    elif change == "revoked":
        f["approvals"][0]["revoked"] = True
    else:
        f["approvals"][0]["issued_at"] = f["now"] + 1
    with pytest.raises(FormError):
        build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])


def test_inactive_field_cannot_be_filled():
    f = demo_fixture()
    f["values"]["onsite_city"] = copy.deepcopy(f["values"]["full_name"])
    with pytest.raises(FormError):
        build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])
    report = inventory_blockers(f["contract"], f["values"], f["approvals"], now=f["now"])
    assert next(row for row in report["fields"] if row["field_id"] == "onsite_city")["reason"] == "inactive_value_supplied"


def test_branch_change_requires_new_values_and_approval():
    f = demo_fixture()
    f["plain_values"]["work_mode"] = "Onsite"
    f = bind_fixture(f, now=f["now"])
    with pytest.raises(FormError):
        build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])
    f["plain_values"]["onsite_city"] = "Example City"
    f = bind_fixture(f, now=f["now"])
    plan = build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])
    assert len([row for row in plan["fields"] if row["state"] == "prepared"]) == 12


@pytest.mark.parametrize("policy", ["no_ai", "unaided"])
def test_no_ai_and_unaided_fields_do_not_get_automated_preparation(policy):
    f = demo_fixture()
    f["contract"]["fields"][0]["assistance"] = policy
    f = bind_fixture(f, now=f["now"])
    with pytest.raises(FormError):
        build_plan(f["contract"], f["values"], f["approvals"], now=f["now"])


@pytest.mark.parametrize("change", ["missing", "duplicate", "kind", "state", "value_type", "unknown_key", "inactive_active", "authorize", "origin", "revisions"])
def test_forged_incomplete_plan_never_matches_readback(change):
    f, plan = prepared()
    if change == "missing":
        plan["fields"].pop()
    elif change == "duplicate":
        plan["fields"][1] = copy.deepcopy(plan["fields"][0])
    elif change == "kind":
        plan["fields"][0]["kind"] = "submit"
    elif change == "state":
        plan["fields"][0]["state"] = "inactive"
    elif change == "value_type":
        plan["fields"][4]["value"] = 1
    elif change == "unknown_key":
        plan["fields"][0]["script"] = "submit()"
    elif change == "inactive_active":
        plan["fields"][6].update(state="prepared", value="Injected City", approval_id="fake", evidence_sha256=["a" * 64])
    elif change == "authorize":
        plan["submission_authorized"] = True
    elif change == "origin":
        plan["origin"] = "https://example.org"
    else:
        plan["revisions"]["policy"] = "a" * 64
    with pytest.raises(FormError):
        validate_readback(f["contract"], plan, {})


@pytest.mark.parametrize("change", ["bool_int", "missing", "duplicate", "unexpected", "submit", "origin", "attachment"])
def test_observed_readback_requires_exact_values_and_coverage(change):
    f, plan = prepared()
    observed = expected_readback(f["contract"], plan)
    if change == "bool_int":
        observed["fields"][4]["value"] = 1
    elif change == "missing":
        observed["fields"].pop()
    elif change == "duplicate":
        observed["fields"].append(observed["fields"][0])
    elif change == "unexpected":
        observed["unexpected_controls"] = ["added_button"]
    elif change == "submit":
        observed["submitted"] = True
    elif change == "origin":
        observed["origin"] = "https://example.org"
    else:
        observed["fields"][9]["value"]["sha256"] = "0" * 64
    assert validate_readback(f["contract"], plan, observed)["status"] == "MISMATCH"


def test_contract_cycles_unknown_fields_and_wrong_revisions_rejected():
    f = demo_fixture()
    for transform in (lambda c: c["fields"][0].update(condition={"field_id": "work_mode", "equals": "Onsite"}),
                      lambda c: c["fields"][0].update(kind="submit"),
                      lambda c: c["revisions"].pop("approval")):
        c = copy.deepcopy(f["contract"])
        transform(c)
        with pytest.raises(FormError):
            validate_contract(c)


def test_html_contains_all_controls_and_no_submit_button():
    fixture = demo_fixture()
    markup = fixture_html(fixture["contract"], "a" * 64).decode()
    assert markup.count('<fieldset ') == 12
    assert '<button' not in markup and 'type="submit"' not in markup
    assert 'e.preventDefault()' in markup


def test_offline_rehearsal_has_no_rendered_or_authentic_claim(tmp_path):
    report = run_fixture(tmp_path / "trial")
    assert report["status"] == "PARTIAL"
    assert report["rendered_browser"]["status"] == "NOT_RUN"
    assert report["field_count"] == 12 and report["typed_action_count"] == 11
    assert report["fixture_server_started"] is False
    assert report["whole_fixture_prepared"] is False
    assert report["authentic_application_validated"] is False
    assert report["submission_authorized"] is False
    assert json.loads((tmp_path / "trial" / "report.json").read_text()) == report
    assert os.stat(tmp_path / "trial").st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        run_fixture(tmp_path / "trial")


def test_missing_playwright_is_unavailable_without_opening_server(tmp_path, monkeypatch):
    import keel_loki.browser as browser
    monkeypatch.setattr(browser.importlib.util, "find_spec", lambda _: None)
    report = run_fixture(tmp_path / "trial", render=True)
    assert report["rendered_browser"]["status"] == "UNAVAILABLE"
    assert report["rendered_browser"]["reason"] == "playwright_unavailable"
    assert report["fixture_server_started"] is False


def test_trial_refuses_symlink_parent(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(FormError):
        run_fixture(tmp_path / "alias" / "trial")
