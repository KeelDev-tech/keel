"""Regression reproductions from the 2026-09-24 isolated Loki audit."""
from copy import deepcopy

import pytest

from keel_loki.recovery import RecoveryError, RecoveryJournal
from keel_loki.skills import SkillWorkshop
from keel_loki.questions import QuestionError, evaluate_policy


@pytest.mark.parametrize("operation", [
    "register", "approve", "revoke", "revise", "claim", "start", "complete",
    "set_hold", "escalate", "record_429", "recover_expired",
])
def test_previous_recovery_controller_cannot_mutate_new_generation(tmp_path, operation):
    home = tmp_path / "journal"
    old = RecoveryJournal(home, "workspace", now=100)
    old.register("job", "a" * 64, now=101)
    old.approve("job", "a" * 64, "b" * 64, now=102)
    lease = old.claim("job", "worker", now=103)
    current = RecoveryJournal(home, "workspace", now=104)
    before = current.snapshot()
    args = {
        "register": ("another", "a" * 64),
        "approve": ("job", "a" * 64, "b" * 64),
        "revoke": ("job",), "revise": ("job", "d" * 64),
        "claim": ("job", "worker-old"),
        "start": ("job", "worker", lease["fence"]),
        "complete": ("job", "worker", lease["fence"], "c" * 64),
        "set_hold": ("job",), "escalate": ("job",),
        "record_429": (), "recover_expired": (),
    }[operation]
    with pytest.raises(RecoveryError, match="stale_controller_generation"):
        getattr(old, operation)(*args, now=105)
    assert current.snapshot() == before
    # Existing readers remain useful without taking ownership of the journal.
    assert old.snapshot() == before
    fresh = current.claim("job", "worker-current", now=105)
    assert fresh["fence"] > lease["fence"]


def _workshop(tmp_path, *, required=True, expected="b" * 64):
    workshop = SkillWorkshop(tmp_path / "skills")
    scope = {"workspace_id": "w", "origin": "https://fixture.invalid",
             "account_id": "a", "role_id": "r"}
    field = {"field_id": "name", "control": "text", "required": required,
             "enabled": True, "approved_ref": "b" * 64, "current_ref": None,
             "expected_ref": expected}
    fixtures = [{"fixture_id": "f" + str(i), "split": "held_out", "scope": scope,
                 "form_revision": "a" * 64, "observed_at": 10,
                 "fields": [deepcopy(field)]} for i in range(2)]
    fixtures[1]["fields"][0]["current_ref"] = "d" * 64
    pin = workshop.freeze_fixtures(fixtures)
    trace = {"trace_id": "trace", "scope": scope, "form_revision": "a" * 64,
             "observed_at": 10, "observations": [{"action": "fill_approved",
                 "field_id": "name", "control": "text", "value_ref": "b" * 64,
                 "readback_ref": "b" * 64}]}
    workshop.quarantine(trace, skill_id="skill", expires_at=100)
    replay = workshop.replay("skill", 1, fixtures_sha256=pin, now=11)
    return workshop, scope, replay


@pytest.mark.parametrize("expected", [None, "c" * 64])
def test_optional_field_exact_readback_blocks_wrong_recipe_promotion(tmp_path, expected):
    workshop, scope, replay = _workshop(tmp_path, required=False, expected=expected)
    assert replay["status"] == "FAIL"
    assert all("optional_readback_mismatch" in row["errors"] for row in replay["rows"])
    with pytest.raises(ValueError, match="passing_bound_replay"):
        workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=12) is None


def test_optional_correct_reference_can_still_promote(tmp_path):
    workshop, scope, replay = _workshop(tmp_path, required=False)
    assert replay["status"] == "PASS"
    workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=12)


def test_skill_cannot_be_active_before_its_promotion(tmp_path):
    workshop, scope, replay = _workshop(tmp_path)
    workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=11) is None
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=12)


def test_backdated_promotion_cannot_reverse_later_rollback(tmp_path):
    workshop, scope, replay = _workshop(tmp_path)
    workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    workshop.rollback("skill", to_version=None, now=14)
    before = sorted(workshop.home.glob("event-*.json"))
    with pytest.raises(ValueError, match="control_time_regressed"):
        workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=13)
    assert sorted(workshop.home.glob("event-*.json")) == before
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=15) is None


def test_backdated_rollback_cannot_restore_a_later_promotion(tmp_path):
    workshop, scope, replay = _workshop(tmp_path)
    workshop.promote("skill", 1, replay_sha256=replay["replay_sha256"], now=12)
    with pytest.raises(ValueError, match="control_time_regressed"):
        workshop.rollback("skill", to_version=1, now=11)
    assert workshop.active("skill", scope=scope, form_revision="a" * 64, now=12)


def _policy_log(propensity, candidate_probability, count=3):
    log, candidate = [], {}
    for index in range(count):
        decision = "d" + str(index)
        log.append({"decision_id": decision, "selected_action": "a",
                    "logging_probabilities": {"a": propensity, "b": 1 - propensity},
                    "propensity": propensity, "reward": 1.0, "outcome_observed": True})
        candidate[decision] = {"a": candidate_probability, "b": 1 - candidate_probability}
    return log, candidate


@pytest.mark.parametrize("propensity,probability", [(1e-154, .7), (.5, 1e-250)])
def test_policy_effective_sample_size_handles_large_and_tiny_finite_weights(propensity, probability):
    log, candidate = _policy_log(propensity, probability)
    result = evaluate_policy(log, candidate)
    assert result["effective_sample_size"] == pytest.approx(3)
    assert result["snips_mean"] == pytest.approx(1)


def test_overflowing_total_policy_weight_is_controlled_rejection():
    log, candidate = _policy_log(1e-308, 1.0)
    with pytest.raises(QuestionError, match="unstable_weight"):
        evaluate_policy(log, candidate)
