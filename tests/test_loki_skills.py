from copy import deepcopy
import pytest
from keel_loki.skills import SkillWorkshop, demo


def setup_data():
    scope = {"workspace_id": "w", "origin": "https://fixture.invalid", "account_id": "a", "role_id": "r"}
    fields = [{"field_id": "name", "control": "text", "required": True, "enabled": True,
               "approved_ref": "b" * 64, "current_ref": None, "expected_ref": "b" * 64}]
    fixtures = [{"fixture_id": "f" + str(i), "split": "held_out", "scope": scope,
                 "form_revision": "a" * 64, "observed_at": 10, "fields": deepcopy(fields)} for i in range(2)]
    fixtures[1]["fields"][0]["current_ref"] = "c" * 64
    trace = {"trace_id": "trace", "scope": scope, "form_revision": "a" * 64, "observed_at": 10,
             "observations": [{"action": "fill_approved", "field_id": "name", "control": "text", "value_ref": "b" * 64, "readback_ref": "b" * 64}]}
    return scope, fixtures, trace


def ready(tmp_path):
    workshop = SkillWorkshop(tmp_path / "skills")
    scope, fixtures, trace = setup_data()
    pin = workshop.freeze_fixtures(fixtures)
    recipe = workshop.quarantine(trace, skill_id="skill", expires_at=100)
    replay = workshop.replay("skill", 1, fixtures_sha256=pin, now=11)
    return workshop, scope, fixtures, trace, recipe, replay


def test_demo_promotes_only_replay_then_rolls_back(tmp_path):
    result = demo(tmp_path / "workshop")
    assert result["replay"]["status"] == "PASS"
    assert result["active_before_rollback"] is True and result["active_after_rollback"] is False
    assert result["replay"]["rendered_browser_verified"] is False


def test_caller_pass_cannot_promote(tmp_path):
    workshop, scope, _, _, recipe, replay = ready(tmp_path)
    with pytest.raises(ValueError):
        workshop.promote("skill", 1, replay_sha256="a" * 64, now=12)
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=12) is None
    workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=12)["status"] == "PROMOTED"


@pytest.mark.parametrize("action", ["submit", "shell", "eval", "set_policy", "approve", "write_applicant_fact"])
def test_poisoned_steps_are_denied(tmp_path, action):
    _, _, trace = setup_data()
    trace["observations"][0]["action"] = action
    with pytest.raises(ValueError):
        SkillWorkshop(tmp_path / "skills").quarantine(trace, skill_id="poison", expires_at=100)


@pytest.mark.parametrize("change", ["scope", "form", "expiry"])
def test_applicability_is_exact_and_expiring(tmp_path, change):
    workshop, scope, _, _, _, replay = ready(tmp_path)
    workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    form, now = "a" * 64, 12
    if change == "scope": scope["account_id"] = "other"
    elif change == "form": form = "c" * 64
    else: now = 100
    assert workshop.active("skill", scope=scope, form_revision=form, now=now) is None


def test_bad_readback_prevents_promotion(tmp_path):
    workshop = SkillWorkshop(tmp_path / "skills")
    _, fixtures, trace = setup_data()
    fixtures[1]["fields"][0]["expected_ref"] = "c" * 64
    pin = workshop.freeze_fixtures(fixtures)
    workshop.quarantine(trace, skill_id="skill", expires_at=100)
    replay = workshop.replay("skill", 1, fixtures_sha256=pin, now=11)
    assert replay["status"] == "FAIL"
    with pytest.raises(ValueError):
        workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)


def test_changed_recipe_cannot_reuse_passing_replay_or_consumed_fixtures(tmp_path):
    workshop, _, _, trace, _, replay = ready(tmp_path)
    second = workshop.quarantine(trace, skill_id="skill", expires_at=101)
    assert second["version"] == 2
    with pytest.raises(ValueError):
        workshop.promote("skill", 2, replay_sha256=replay["replay_sha256"], now=12)
    with pytest.raises(ValueError, match="already_used"):
        workshop.replay("skill", 2, fixtures_sha256=replay["fixtures_sha256"], now=12)


def test_fixtures_must_be_frozen_before_candidate(tmp_path):
    workshop = SkillWorkshop(tmp_path / "skills")
    _, fixtures, trace = setup_data()
    workshop.quarantine(trace, skill_id="skill", expires_at=100)
    pin = workshop.freeze_fixtures(fixtures)
    with pytest.raises(ValueError, match="precede"):
        workshop.replay("skill", 1, fixtures_sha256=pin, now=11)


def test_duplicate_variants_cannot_inflate_replay(tmp_path):
    _, fixtures, _ = setup_data()
    fixtures[1]["fields"][0]["current_ref"] = None
    with pytest.raises(ValueError, match="variants_duplicate"):
        SkillWorkshop(tmp_path / "skills").freeze_fixtures(fixtures)


def test_unknown_required_field_is_failure(tmp_path):
    workshop = SkillWorkshop(tmp_path / "skills")
    _, fixtures, trace = setup_data()
    fixtures[1]["fields"].append({"field_id": "new", "control": "text", "required": True, "enabled": True,
        "approved_ref": "b" * 64, "current_ref": None, "expected_ref": "b" * 64})
    pin = workshop.freeze_fixtures(fixtures)
    workshop.quarantine(trace, skill_id="skill", expires_at=100)
    assert workshop.replay("skill", 1, fixtures_sha256=pin, now=11)["status"] == "FAIL"


def test_rollback_cannot_activate_unpromoted_version(tmp_path):
    workshop, _, _, _, _, _ = ready(tmp_path)
    with pytest.raises(ValueError, match="prior_promotion"):
        workshop.rollback("skill", to_version=1, now=12)


def test_tampering_and_symlink_home_rejected(tmp_path):
    workshop, scope, _, _, _, _ = ready(tmp_path)
    first = workshop.home / "event-00000000.json"
    first.write_text(first.read_text().replace('"sequence":0', '"sequence":9'))
    with pytest.raises(ValueError):
        workshop.active("skill", scope=scope, form_revision="a" * 64, now=12)
    link = tmp_path / "alias"
    link.symlink_to(workshop.home, target_is_directory=True)
    with pytest.raises(ValueError):
        SkillWorkshop(link)
