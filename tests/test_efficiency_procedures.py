"""Fixed held-out qualification, revocation and source freshness tests."""
import math
import sqlite3

import pytest

from keel_efficiency.procedures import (EvaluationCase, ProcedureError, ProcedureRegistry,
                                       QualificationPolicy, TraceStep, binomial_error_upper)


@pytest.fixture
def registry(tmp_path):
    clock = [1000.0]
    instance = ProcedureRegistry(tmp_path / "procedures.sqlite", clock=lambda: clock[0])
    instance.test_clock = clock
    instance.test_path = tmp_path / "procedures.sqlite"
    yield instance
    instance.close()


BINDINGS = {"model": "m1", "policy": "p1", "source": "s1"}


def compile(registry, **changes):
    params = dict(name="repair", version="1", training_task_ids=["train-1"],
                  steps=[TraceStep("inspect", {"query": "race"}, ("trace://1",))],
                  source_bindings=BINDINGS, expires_at=2000, success=True, verified=True)
    params.update(changes)
    return registry.compile_trace(**params)["candidate_id"]


def qualify(registry, candidate_id, **changes):
    params = dict(evaluations=[EvaluationCase(f"held-{i}", True) for i in range(60)],
                  current_bindings=BINDINGS, independent_tasks=True)
    params.update(changes)
    return registry.qualify(candidate_id, **params)


def retrieve(registry, candidate_id, **changes):
    return registry.get_qualified(candidate_id, **{ "current_bindings": BINDINGS, "expected_version": "1", **changes})


def test_compile_promote_reopen_and_never_authorize(registry):
    candidate = compile(registry)
    assert retrieve(registry, candidate)["reason"] == "NOT_QUALIFIED"
    result = qualify(registry, candidate)
    assert result["status"] == "QUALIFIED" and result["error_upper"] < 0.05
    reopened = ProcedureRegistry(registry.test_path, clock=lambda: 1100)
    try:
        found = retrieve(reopened, candidate)
        assert found["status"] == "QUALIFIED"
        assert found["procedure"]["kind"] == "data_only"
        assert not found["execution_authorized"] and not found["approval_reusable"]
        assert found["fresh_authorization_required"]
        found["procedure"]["steps"].clear()
        assert retrieve(reopened, candidate)["procedure"]["steps"]
    finally:
        reopened.close()


def test_exact_binomial_bound_known_values_and_monotonicity():
    assert binomial_error_upper(0, 60) == pytest.approx(1 - 0.05 ** (1 / 60))
    assert binomial_error_upper(1, 10) == pytest.approx(0.3941633024365047, rel=1e-10)
    assert binomial_error_upper(10, 10) == 1
    assert binomial_error_upper(0, 120) < binomial_error_upper(0, 60)
    assert binomial_error_upper(1, 60) > binomial_error_upper(0, 60)
    assert binomial_error_upper(1, 60, 0.99) > binomial_error_upper(1, 60, 0.95)


@pytest.mark.parametrize("params", [{"success": False}, {"verified": False}, {"success": 1},
                                      {"expires_at": 900}, {"expires_at": math.inf},
                                      {"training_task_ids": ["x", "x"]}, {"steps": []}])
def test_invalid_or_unverified_traces_cannot_compile(registry, params):
    with pytest.raises(ProcedureError):
        compile(registry, **params)


def test_policy_pinned_name_version_immutable_and_no_code_execution(registry, tmp_path):
    marker = tmp_path / "must-not-exist"
    steps = [TraceStep("python", {"code": f"open({str(marker)!r},'w').close()"}, ("trace://1",))]
    candidate = compile(registry, steps=steps)
    assert not marker.exists()
    assert compile(registry, steps=steps) == candidate
    with pytest.raises(ProcedureError):
        compile(registry, policy=QualificationPolicy(max_error_upper=0.2))
    assert not marker.exists()


def test_train_overlap_duplicate_eval_and_missing_iid_are_held(registry):
    candidate = compile(registry)
    assert qualify(registry, candidate, independent_tasks=False)["reason"] == "IID_TASK_DECLARATION_REQUIRED"
    cases = [EvaluationCase(f"held-{i}", True) for i in range(59)] + [EvaluationCase("train-1", True)]
    assert qualify(registry, candidate, evaluations=cases)["reason"] == "TRAIN_EVALUATION_OVERLAP"
    cases[-1] = cases[0]
    assert qualify(registry, candidate, evaluations=cases)["reason"] == "DUPLICATE_EVALUATION_TASK"
    assert qualify(registry, candidate, evaluations=[EvaluationCase("single", True)])["reason"] == "INSUFFICIENT_EVALUATIONS"
    assert qualify(registry, candidate)["status"] == "QUALIFIED"


def test_failed_fixed_trial_cannot_be_retried_until_it_passes(registry):
    candidate = compile(registry)
    cases = [EvaluationCase(f"held-{i}", i > 10) for i in range(60)]
    failed = qualify(registry, candidate, evaluations=cases)
    assert failed["status"] == "HOLD" and failed["errors"] == 11
    assert retrieve(registry, candidate)["reason"] == "NOT_QUALIFIED"
    assert qualify(registry, candidate)["reason"] == "QUALIFICATION_TRIAL_ALREADY_CONSUMED"


def test_any_unsafe_success_blocks_promotion_even_under_lenient_bound(registry):
    candidate = compile(registry, policy=QualificationPolicy(max_error_upper=0.9))
    cases = [EvaluationCase(f"held-{i}", True, unsafe=(i == 0)) for i in range(60)]
    result = qualify(registry, candidate, evaluations=cases)
    assert result["status"] == "HOLD" and result["unsafe_results"] == 1
    assert result["error_upper"] < 0.9


def test_consumed_holdout_cannot_train_or_qualify_another_version(registry):
    candidate = compile(registry)
    assert qualify(registry, candidate)["status"] == "QUALIFIED"
    with pytest.raises(ProcedureError, match="held-out evaluation"):
        compile(registry, version="2", training_task_ids=["held-0"])
    next_candidate = compile(registry, version="2")
    assert qualify(registry, next_candidate)["reason"] == "EVALUATION_TASK_ALREADY_CONSUMED"


def test_cross_candidate_training_overlap_detected(registry):
    compile(registry, version="one", training_task_ids=["held-0"])
    candidate = compile(registry, version="two")
    assert qualify(registry, candidate)["reason"] == "TRAIN_EVALUATION_OVERLAP"


def test_binding_version_expiry_and_sticky_revocation(registry):
    candidate = compile(registry)
    assert qualify(registry, candidate, current_bindings={**BINDINGS, "model": "m2"})["reason"] == "STALE_BINDINGS"
    assert qualify(registry, candidate)["status"] == "QUALIFIED"
    assert retrieve(registry, candidate, expected_version="2")["reason"] == "VERSION_MISMATCH"
    assert retrieve(registry, candidate, current_bindings={"model": "m1"})["reason"] == "STALE_BINDINGS"
    registry.revoke(candidate, reason="New regression")
    assert retrieve(registry, candidate)["reason"] == "REVOKED"
    assert qualify(registry, candidate)["reason"] == "REVOKED"
    compile(registry)
    assert retrieve(registry, candidate)["reason"] == "REVOKED"
    expiring = compile(registry, version="2")
    registry.test_clock[0] = 2000
    assert retrieve(registry, expiring)["reason"] == "EXPIRED"


def test_scope_isolation_and_candidate_integrity(registry):
    candidate = compile(registry)
    other = ProcedureRegistry(registry.test_path, scope="other", clock=lambda: 1100)
    try:
        with pytest.raises(ProcedureError, match="unknown procedure"):
            retrieve(other, candidate)
    finally:
        other.close()
    registry.db.execute("UPDATE efficiency_procedures SET body=replace(body, 'inspect', 'execute') WHERE id=?", (candidate,))
    with pytest.raises(ProcedureError, match="integrity"):
        retrieve(registry, candidate)


@pytest.mark.parametrize("args", [(True, 2), (0, 0), (-1, 4), (4, 3), (0, 10001)])
def test_invalid_binomial_inputs(args):
    with pytest.raises(ProcedureError):
        binomial_error_upper(*args)


def test_private_database_and_final_symlinks_or_nonregular_paths_rejected(tmp_path):
    import os
    import stat
    path = tmp_path / "private.sqlite"
    instance = ProcedureRegistry(path)
    instance.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    instance = ProcedureRegistry(path)
    instance.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    link = tmp_path / "link.sqlite"
    link.symlink_to(path)
    with pytest.raises(ProcedureError):
        ProcedureRegistry(link)
    fifo = tmp_path / "fifo.sqlite"
    os.mkfifo(fifo)
    with pytest.raises(ProcedureError):
        ProcedureRegistry(fifo)


def test_strict_data_cannot_coerce_keys_or_encode_nan(registry):
    for value in ({1: "coerced"}, {"x": float("nan")}, {"x": {"set"}}):
        with pytest.raises(ProcedureError):
            compile(registry, steps=[TraceStep("inspect", value, ("trace://1",))])
    cycle = {}
    cycle["self"] = cycle
    with pytest.raises(ProcedureError):
        compile(registry, steps=[TraceStep("inspect", cycle, ("trace://1",))])


def test_maximum_binomial_trial_count_is_finite():
    upper = binomial_error_upper(2500, 10000)
    assert math.isfinite(upper) and 0.25 < upper < 0.27


def test_clock_regression_stays_held_across_reopen_and_later_clock_recovery(registry):
    candidate = compile(registry)
    assert qualify(registry, candidate)["status"] == "QUALIFIED"
    registry.test_clock[0] = 2000
    assert retrieve(registry, candidate)["reason"] == "EXPIRED"
    registry.test_clock[0] = 1500
    assert retrieve(registry, candidate)["reason"] == "CLOCK_REGRESSION"
    reopened = ProcedureRegistry(registry.test_path, clock=lambda: 2500)
    try:
        assert retrieve(reopened, candidate)["reason"] == "CLOCK_REGRESSION"
        assert qualify(reopened, candidate)["reason"] == "CLOCK_REGRESSION"
        with pytest.raises(ProcedureError, match="CLOCK_REGRESSION"):
            compile(reopened, version="later", expires_at=5000)
    finally:
        reopened.close()
