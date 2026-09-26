"""Policy unit tests are separate from opt-in actual browser qualification."""
import copy
import os
import sys
import pytest

from keel_loki import browser_lab as lab
from keel_loki import playwright_adapter
from keel_loki.common import digest
from keel_loki.skills import SkillWorkshop


def candidate(tmp_path):
    workshop = SkillWorkshop(tmp_path / "workshop")
    recipe = workshop.quarantine(lab.demo_trace(10), skill_id="local-form", expires_at=100)
    return workshop, recipe


class DeterministicTransport:
    """A fake transport is only used to exercise _drive; never rendered proof."""
    def __init__(self, fault=None):
        self.fault = fault
        self.sequence = 0
        self.applied = []
        self.denied_requests = self.submission_attempts = 0
        self.first = None

    def open(self):
        return 429 if self.fault == "429" else 302 if self.fault == "redirect" else 200

    def snapshot(self, challenge):
        if self.fault == "disconnect" and self.applied:
            raise RuntimeError("private transport details")
        if self.fault == "stale" and self.first:
            return copy.deepcopy(self.first)
        self.sequence += 1
        result = {"challenge": challenge, "sequence": self.sequence, "nonce": "nonce",
                  "origin": "http://127.0.0.1:1234", "fields": copy.deepcopy(lab.FIELDS), "submitted": False}
        if self.fault == "schema":
            result["fields"][0]["control"] = "password"
        if self.fault == "origin":
            result["origin"] = "https://external.invalid"
        if self.fault == "nonce":
            result["nonce"] = "previous-run"
        if self.fault == "submitted":
            result["submitted"] = True
        self.first = copy.deepcopy(result)
        return result

    def apply(self, field, value, encoded):
        self.applied.append(field)
        if self.fault == "effect_exception":
            raise RuntimeError("could have filled before error")

    def readback(self):
        values = copy.deepcopy(lab.VALUES)
        if self.fault == "attachment":
            values["resume"]["sha256"] = "0" * 64
        return values


def drive(recipe, fault=None):
    transport = DeterministicTransport(fault)
    return lab._drive(transport, recipe, nonce="nonce", origin="http://127.0.0.1:1234"), transport


def test_exact_dom_and_attachment_readback_without_rendered_claim(tmp_path):
    _, recipe = candidate(tmp_path)
    report, transport = drive(recipe)
    assert report["status"] == "PREPARED"
    assert report["readback"] == lab.VALUES
    assert transport.applied == ["name", "resume"]
    assert "rendered_browser_verified" not in report
    assert report["automatic_retry"] is False


@pytest.mark.parametrize("fault,reason", [("429", "http_429"), ("stale", "stale_snapshot"),
    ("schema", "form_changed"), ("origin", "unexpected_navigation"),
    ("nonce", "unexpected_navigation"), ("submitted", "form_changed"), ("redirect", "unexpected_navigation")])
def test_faults_stop_before_effect(tmp_path, fault, reason):
    _, recipe = candidate(tmp_path)
    report, transport = drive(recipe, fault)
    assert report["status"] == "BLOCKED" and report["reason"] == reason
    assert report["action_attempts"] == 0 and transport.applied == []


@pytest.mark.parametrize("fault,count,reason", [("disconnect",1,"transport_disconnected"),
    ("effect_exception",1,"transport_disconnected"), ("attachment",2,"readback_mismatch")])
def test_uncertain_effect_and_bad_file_hash_never_retry(tmp_path, fault, count, reason):
    _, recipe = candidate(tmp_path)
    report, transport = drive(recipe, fault)
    assert report["status"] == "UNKNOWN" and report["reason"] == reason
    assert report["action_attempts"] == count and len(transport.applied) == count
    assert report["automatic_retry"] is False and "private" not in str(report)


def test_unexpected_request_or_post_does_not_pass(tmp_path):
    _, recipe = candidate(tmp_path)
    for attribute in ("denied_requests", "submission_attempts"):
        transport = DeterministicTransport()
        setattr(transport, attribute, 1)
        report = lab._drive(transport, recipe, nonce="nonce", origin="http://127.0.0.1:1234")
        assert report["status"] == "UNKNOWN"


@pytest.mark.parametrize("mutation", ["scope", "form", "value", "attestation", "extra_step", "expiry"])
def test_recipe_cannot_change_scope_credentials_values_or_attestations(tmp_path, mutation):
    _, recipe = candidate(tmp_path)
    if mutation == "scope": recipe["scope"]["origin"] = "https://employer.invalid"
    elif mutation == "form": recipe["form_revision"] = "0" * 64
    elif mutation == "value": recipe["steps"][0]["value_ref"] = digest("real applicant")
    elif mutation == "attestation": recipe["steps"][0]["action"] = "set_attestation"
    elif mutation == "extra_step": recipe["steps"].append(copy.deepcopy(recipe["steps"][0]))
    else: recipe["expires_at"] = 11
    with pytest.raises(ValueError): lab.validate_recipe(recipe, 11)


def test_source_revision_is_required_before_transport(tmp_path, monkeypatch):
    _, recipe = candidate(tmp_path)
    def no_transport(_):
        raise AssertionError("transport was reached")
    monkeypatch.setattr(playwright_adapter, "run_cases", no_transport)
    with pytest.raises(ValueError, match="source_revision_changed"):
        lab.run_rendered(recipe, now=11, expected_source_revision="0" * 64)


def test_missing_playwright_is_blocked_and_not_promotable(tmp_path, monkeypatch):
    workshop, recipe = candidate(tmp_path)
    monkeypatch.setitem(sys.modules, "playwright", None)
    report = workshop.qualify_rendered("local-form", 1, now=11, expected_source_revision=lab.source_revision())
    assert report["status"] == "BLOCKED" and report["rendered_browser_verified"] is False
    assert all(r["reason"] == "playwright_not_installed" for r in report["rows"])
    with pytest.raises(ValueError, match="rendered_qualification_required"):
        workshop.promote_rendered("local-form", 1, qualification_sha256=report["qualification_sha256"], now=12)
    assert workshop.active_rendered("local-form", scope=lab.LAB_SCOPE, form_revision=lab.FORM_REVISION, now=12) is None


def stub_rows(_):
    # Fabricated rows for control-flow tests, not actual-browser validation.
    return [{"case_id": case, "status": expected[0], "reason": expected[1],
             "action_attempts": expected[2], "browser_started": True} for case, expected in lab.CASES.items()]


def synthetic_promotion(tmp_path, monkeypatch):
    workshop, recipe = candidate(tmp_path)
    monkeypatch.setattr(playwright_adapter, "run_cases", stub_rows)
    report = workshop.qualify_rendered("local-form", 1, now=11, expected_source_revision=lab.source_revision())
    workshop.promote_rendered("local-form", 1, qualification_sha256=report["qualification_sha256"], now=12)
    return workshop, recipe, report


def test_promotion_consumes_bound_internal_event_only(tmp_path, monkeypatch):
    workshop, recipe = candidate(tmp_path)
    with pytest.raises(ValueError, match="rendered_qualification_required"):
        workshop.promote_rendered("local-form", 1, qualification_sha256="f" * 64, now=11)
    monkeypatch.setattr(playwright_adapter, "run_cases", stub_rows)
    report = workshop.qualify_rendered("local-form", 1, now=11, expected_source_revision=lab.source_revision())
    assert report["recipe_sha256"] == digest(recipe)
    workshop.promote_rendered("local-form", 1, qualification_sha256=report["qualification_sha256"], now=12)
    active = workshop.active_rendered("local-form", scope=lab.LAB_SCOPE, form_revision=lab.FORM_REVISION, now=12)
    assert active["synthetic"] is True and active["execution_authorized"] is False
    assert active["submission_authorized"] is False
    assert workshop.active("local-form", scope=lab.LAB_SCOPE, form_revision=lab.FORM_REVISION, now=12) is None


@pytest.mark.parametrize("change", ["scope", "form", "clock", "expiry", "source", "rollback"])
def test_rendered_promotion_is_exact_and_revocable(tmp_path, monkeypatch, change):
    workshop, _, _ = synthetic_promotion(tmp_path, monkeypatch)
    scope, form, now = copy.deepcopy(lab.LAB_SCOPE), lab.FORM_REVISION, 13
    if change == "scope": scope["account_id"] = "other"
    elif change == "form": form = "0" * 64
    elif change == "clock": now = 11
    elif change == "expiry": now = 100
    elif change == "source": monkeypatch.setattr(lab, "source_revision", lambda: "a" * 64)
    else: workshop.rollback("local-form", to_version=None, now=13)
    assert workshop.active_rendered("local-form", scope=scope, form_revision=form, now=now) is None


def test_failed_requalification_invalidates_old_pass_and_active(tmp_path, monkeypatch):
    workshop, _, old = synthetic_promotion(tmp_path, monkeypatch)
    rows = stub_rows(None)
    rows[0]["status"] = "UNKNOWN"
    monkeypatch.setattr(playwright_adapter, "run_cases", lambda _: rows)
    report = workshop.qualify_rendered("local-form", 1, now=13, expected_source_revision=lab.source_revision())
    assert report["status"] == "FAIL"
    assert workshop.active_rendered("local-form", scope=lab.LAB_SCOPE, form_revision=lab.FORM_REVISION, now=14) is None
    with pytest.raises(ValueError, match="rendered_qualification_required"):
        workshop.promote_rendered("local-form", 1, qualification_sha256=old["qualification_sha256"], now=14)


@pytest.mark.parametrize("fault", ["duplicate", "missing", "not_started", "wrong_effect_count"])
def test_incomplete_corpus_cannot_qualify(tmp_path, monkeypatch, fault):
    _, recipe = candidate(tmp_path)
    rows = stub_rows(None)
    if fault == "duplicate": rows[-1] = rows[0]
    elif fault == "missing": rows.pop()
    elif fault == "not_started": rows[0]["browser_started"] = False
    else: rows[0]["action_attempts"] = 99
    monkeypatch.setattr(playwright_adapter, "run_cases", lambda _: rows)
    assert lab.run_rendered(recipe, now=11, expected_source_revision=lab.source_revision())["status"] == "FAIL"


def test_source_change_during_render_invalidates_trial(tmp_path, monkeypatch):
    _, recipe = candidate(tmp_path)
    revisions = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(lab, "source_revision", lambda: next(revisions))
    monkeypatch.setattr(playwright_adapter, "run_cases", stub_rows)
    result = lab.run_rendered(recipe, now=11, expected_source_revision="a" * 64)
    assert result["status"] == "FAIL" and result["rendered_browser_verified"] is False


def test_regressed_qualification_and_promotion_time_rejected(tmp_path, monkeypatch):
    workshop, _, report = synthetic_promotion(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="time_regressed"):
        workshop.qualify_rendered("local-form", 1, now=11, expected_source_revision=lab.source_revision())
    with pytest.raises(ValueError, match="time_regressed"):
        workshop.promote_rendered("local-form", 1, now=11, qualification_sha256=report["qualification_sha256"])


@pytest.mark.skipif(os.getenv("KEEL_RENDERED_BROWSER_LAB") != "1", reason="actual rendered browser lab requires explicit opt-in and installed Playwright/Chromium")
def test_actual_rendered_corpus(tmp_path):
    workshop, _ = candidate(tmp_path)
    report = workshop.qualify_rendered("local-form", 1, now=11, expected_source_revision=lab.source_revision())
    assert report["status"] == "PASS", report
    assert report["rendered_browser_verified"] is True
    assert all(r["observation_provenance"] == "actual_local_playwright" for r in report["rows"])
