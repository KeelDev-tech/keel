from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import random
import sqlite3

import pytest

from keel_agent.models import ReviewerConfig
from keel_learning import ExperimentRegistry, LearningError, bounded_confidence_sequence, digest, matrix_plan, reliability_matrix, propose_route, validate_plan, source_budget_proposal

PINS = {k: c*64 for k,c in zip(("source_sha256", "config_sha256", "model_sha256", "calibration_sha256"), "abcd")}
NOW = "2026-09-24T12:00:00+00:00"


def plan(**changes):
    result = {"schema": "keel.learning.experiment.v1", "experiment_id": "sources-v1", "bindings": PINS,
        "actions": ["a", "b"], "strata": {"jobs": {"logging": {"a": .5, "b": .5}, "candidate": {"a": 1, "b": 0}, "baseline": {"a": 0, "b": 1}, "predictions": {"a": .8, "b": .2}}},
        "objective": "qualified-per-assigned-minute", "predictor_frozen_at": "2026-09-23T12:00:00Z",
        "predictor_training_sha256": "e"*64, "delta": .05, "min_samples": 1, "min_ess": 1,
        "min_improvement": 0, "synthetic": True}
    result.update(changes)
    return deepcopy(result)


def store(path=":memory:", validator=lambda e,p: p == "host"):
    return ExperimentRegistry(path, validator=validator, clock=lambda: NOW)


def add(reg, pin, i, *, reward=.8, draw=0, minutes=2):
    decision = reg.decide("sources-v1", expected_pin=pin, decision_id=f"d{i}", unit_id=f"unit{i}", stratum="jobs", current_bindings=PINS, proof="host", synthetic_draw=draw)
    reg.record_outcome(f"d{i}", reward=reward, human_minutes=minutes, executed_action=decision["action"], observation_sha256=digest(["obs",i]), current_bindings=PINS, proof="host")
    return decision


def evaluate(reg, pin, pins=PINS):
    return reg.evaluate("sources-v1", expected_pin=pin, current_bindings=pins, proof="host")


def test_dr_known_values_and_ess():
    r=store(); p=plan(); pin=r.register(p,proof="host")
    d=add(r,pin,0,reward=1,draw=0)
    add(r,pin,1,reward=0,draw=1)
    out=evaluate(r,pin)
    # a row: candidate .8+2*(1-.8)=1.2; baseline .2.
    # b row: candidate .8; baseline .2+2*(0-.2)=-.2.
    assert out["candidate_value"] == pytest.approx(1)
    assert out["baseline_value"] == pytest.approx(0)
    assert out["improvement_confidence_sequence"]["estimate"] == pytest.approx(1)
    assert out["effective_sample_size"] == {"candidate":1,"baseline":1}
    assert out["recorded_human_minutes"] == 4
    assert d["action_probability"] == .5 and not d["execution_authorized"]
    assert out["status"] == "HOLD" and "SYNTHETIC_ONLY" in out["hold_reasons"]


def test_frozen_correct_predictions_remove_residual():
    r=store(); pin=r.register(plan(),proof="host")
    add(r,pin,0,reward=.8,draw=0)
    add(r,pin,1,reward=.2,draw=1)
    out=evaluate(r,pin)
    assert out["candidate_value"] == pytest.approx(.8)
    assert out["baseline_value"] == pytest.approx(.2)


def test_wrong_predictions_unbiased_with_actual_propensities():
    p=plan(); p["strata"]["jobs"]["predictions"]={"a":0,"b":1}
    r=store(); pin=r.register(p,proof="host")
    add(r,pin,0,reward=1,draw=0); add(r,pin,1,reward=0,draw=1)
    out=evaluate(r,pin)
    assert out["candidate_value"] == 1 and out["baseline_value"] == 0


@pytest.mark.parametrize("change", [lambda p:p["strata"]["jobs"]["logging"].pop("b"), lambda p:p["strata"]["jobs"]["logging"].update(a=.2), lambda p:p["strata"]["jobs"]["predictions"].update(a=float("nan")), lambda p:p.update(delta=True), lambda p:p.update(min_samples=0)])
def test_invalid_probability_prediction_or_plan(change):
    p=plan(); change(p)
    with pytest.raises(LearningError): validate_plan(p)


def test_no_overlap_never_claims_improvement():
    p=plan(); p["strata"]["jobs"]["logging"]={"a":1,"b":0}
    r=store(); pin=r.register(p,proof="host"); add(r,pin,0,reward=1)
    out=evaluate(r,pin)
    assert "NO_OVERLAP" in out["hold_reasons"] and out["candidate_value"] is None


def test_repeated_unit_and_outcome_rejected():
    r=store(); pin=r.register(plan(),proof="host"); d=add(r,pin,0)
    with pytest.raises(LearningError,match="repeated_unit_cluster"):
        r.decide("sources-v1",expected_pin=pin,decision_id="other",unit_id="unit0",stratum="jobs",current_bindings=PINS,proof="host",synthetic_draw=1)
    with pytest.raises(LearningError,match="outcome_already_frozen"):
        r.record_outcome("d0",reward=1,human_minutes=1,executed_action=d["action"],observation_sha256="a"*64,current_bindings=PINS,proof="host")


def test_predictor_cannot_be_frozen_after_start_and_registry_cannot_be_refit():
    r=store()
    with pytest.raises(LearningError,match="predictor_not_frozen"):
        r.register(plan(predictor_frozen_at=NOW),proof="host")
    r.register(plan(),proof="host")
    changed=plan(); changed["strata"]["jobs"]["predictions"]["a"]=1
    with pytest.raises(LearningError,match="already_frozen"):
        r.register(changed,proof="host")


def test_authentication_and_no_retrospective_outcome():
    with pytest.raises(LearningError,match="host_validator_required"):
        ExperimentRegistry(":memory:",validator=None)
    r=store()
    with pytest.raises(LearningError,match="attestation_rejected"):
        r.register(plan(),proof={"verified":True})
    with pytest.raises(LearningError,match="decision_must_precede_outcome"):
        r.record_outcome("unknown",reward=1,human_minutes=1,executed_action="a",observation_sha256="a"*64,current_bindings=PINS,proof="host")


def test_production_draw_cannot_be_overridden():
    r=store(); pin=r.register(plan(synthetic=False),proof="host")
    with pytest.raises(LearningError,match="random_override"):
        r.decide("sources-v1",expected_pin=pin,decision_id="d",unit_id="u",stratum="jobs",current_bindings=PINS,proof="host",synthetic_draw=0)


def test_incomplete_outcomes_do_not_select_faster_later_rows():
    r=store(); pin=r.register(plan(),proof="host")
    r.decide("sources-v1",expected_pin=pin,decision_id="first",unit_id="first",stratum="jobs",current_bindings=PINS,proof="host",synthetic_draw=0)
    add(r,pin,1,reward=1)
    out=evaluate(r,pin)
    assert out["complete_prefix"] == 0 and "INCOMPLETE_OUTCOMES" in out["hold_reasons"]


@pytest.mark.parametrize("reward,minutes", [(float("nan"),1),(float("inf"),1),(-.1,1),(1,None),(1,-1),(1,float("inf"))])
def test_bad_outcomes_fail_closed(reward,minutes):
    r=store(); pin=r.register(plan(),proof="host")
    with pytest.raises(LearningError): add(r,pin,0,reward=reward,minutes=minutes)


def test_binding_drift_holds_evaluation():
    r=store(); pin=r.register(plan(),proof="host"); add(r,pin,0)
    pins=dict(PINS,source_sha256="f"*64)
    assert "BINDINGS_DRIFTED" in evaluate(r,pin,pins)["hold_reasons"]
    with pytest.raises(LearningError,match="bindings_drifted"):
        r.decide("sources-v1",expected_pin=pin,decision_id="next",unit_id="next",stratum="jobs",current_bindings=pins,proof="host")


def test_alpha_budget_and_plan_survive_restart(tmp_path):
    path=tmp_path/"registry.sqlite3"
    r=store(path); pin=r.register(plan(),proof="host"); add(r,pin,0); r.close()
    r=store(path)
    assert evaluate(r,pin)["complete_prefix"] == 1
    with pytest.raises(LearningError,match="budget_exhausted"):
        r.register(plan(experiment_id="second"),proof="host")
    r.close()
    with pytest.raises(LearningError,match="alpha_budget_changed"):
        ExperimentRegistry(path,validator=lambda e,p:True,alpha_budget=.1)


def test_all_failures_cannot_improve():
    p=plan(); p["strata"]["jobs"]["predictions"]={"a":0,"b":0}
    r=store(); pin=r.register(p,proof="host")
    for i in range(10): add(r,pin,i,reward=0,draw=i%2)
    out=evaluate(r,pin)
    assert out["candidate_value"] == 0 and "IMPROVEMENT_NOT_ESTABLISHED" in out["hold_reasons"]


def test_time_uniform_formula_and_optional_stopping_regression():
    out=bounded_confidence_sequence([1]*10,lower=0,upper=1,delta=.05)
    expected=math.sqrt(math.log(2*10*11/.05)/(2*10))
    assert out[-1]["radius"] == pytest.approx(expected)
    # Seeded modest Monte Carlo is a regression check, not a proof. Stop at
    # the first exclusion over 250 repeated looks in every experiment.
    rng=random.Random(12991); failures=0
    for _ in range(150):
        values=[int(rng.random()<.3) for _ in range(250)]
        sequence=bounded_confidence_sequence(values,lower=0,upper=1,delta=.1)
        failures += any(not r["lower"] <= .3 <= r["upper"] for r in sequence)
    assert failures <= 15


def matrix_setup(synthetic=True):
    config=asdict(ReviewerConfig("agent-a","ollama","http://127.0.0.1:11434/api/chat","local-model"))
    agents={"agent-a":{"config":config,"config_sha256":digest(config),"model_sha256":"c"*64}}
    p=matrix_plan(matrix_id="matrix",agents=agents,tasks=["review"],source_sha256="a"*64,calibration_sha256="d"*64,max_error=.15,min_coverage=.5,min_trials=30,synthetic=synthetic)
    b={"agent-a":dict(PINS,config_sha256=digest(config))}
    trials=[{"trial_id":f"t{i}","unit_id":f"u{i}","agent_id":"agent-a","task_id":"review","bindings":b["agent-a"],"observed":"PASS","expected":"PASS","latency_seconds":2,"human_correction_minutes":0,"observation_sha256":digest(["o",i]),"label_sha256":digest(["l",i])} for i in range(150)]
    return p,b,trials


def test_matrix_uses_real_labels_and_existing_route_builder():
    p,b,trials=matrix_setup(synthetic=False)
    out=propose_route(p,trials,task_id="review",expected_pin=digest(p),current_bindings=b,validator=lambda e,p:True,proof="host")
    assert out["status"] == "PROPOSAL_ONLY"
    assert out["policy"]["schema"] == "keel.loki.route-policy.v1"
    assert out["policy"]["small_config"] == p["agents"]["agent-a"]["config"]
    assert out["model_calls"] == 0 and not out["execution_authorized"]


@pytest.mark.parametrize("kind", ["abstain","failure","drift","few","synthetic"])
def test_matrix_holds_weak_or_drifted_evidence(kind):
    p,b,rows=matrix_setup(synthetic=kind=="synthetic")
    if kind=="abstain":
        for r in rows:r["observed"]="ABSTAIN"
    if kind=="failure":
        for r in rows:r["expected"]="FAIL"
    if kind=="drift":b["agent-a"]=dict(b["agent-a"],model_sha256="f"*64)
    if kind=="few":rows=rows[:2]
    out=propose_route(p,rows,task_id="review",expected_pin=digest(p),current_bindings=b,validator=lambda e,p:True,proof=None)
    assert out["status"] == "HOLD" and out["policy"] is None


def test_matrix_rejects_cluster_aliases_and_forged_labels():
    p,b,rows=matrix_setup()
    rows[1]["unit_id"]=rows[0]["unit_id"]
    with pytest.raises(LearningError,match="repeated_unit_cluster"):
        reliability_matrix(p,rows,expected_pin=digest(p),current_bindings=b,validator=lambda e,p:True,proof=None)
    with pytest.raises(LearningError,match="attestation_rejected"):
        reliability_matrix(p,[],expected_pin=digest(p),current_bindings=b,validator=lambda e,p:False,proof={"auth":True})


def test_evaluation_attestation_binds_snapshot_and_fresh_time():
    events=[]
    r=store(validator=lambda e,p: events.append(e) or True)
    pin=r.register(plan(),proof=None)
    before=evaluate(r,pin)
    event_before=events[-1]
    add(r,pin,0)
    after=evaluate(r,pin)
    event_after=events[-1]
    assert event_before["snapshot_sha256"] != event_after["snapshot_sha256"]
    assert before["snapshot_sha256"] == event_before["snapshot_sha256"]
    assert after["snapshot_sha256"] == event_after["snapshot_sha256"]
    assert event_after["observation_sha256s"] == [digest(["obs",0])]
    assert event_after["evaluated_at"] == NOW
    assert "fresh_nonreplayed_proof" in event_after["required_assertions"]
    r.validator=lambda event,proof: event["kind"] != "evaluate"
    with pytest.raises(LearningError,match="attestation_rejected"):evaluate(r,pin)


def test_private_database_identity_and_parent(tmp_path):
    public=tmp_path/"public"; public.mkdir(mode=0o755)
    with pytest.raises(LearningError,match="private_parent_required"):store(public/"db")
    private=tmp_path/"private"; private.mkdir(mode=0o700)
    path=private/"db"
    r=store(path); pin=r.register(plan(),proof="host")
    assert path.stat().st_mode & 0o777 == 0o600
    path.rename(private/"old"); path.write_bytes(b"replacement")
    with pytest.raises(LearningError,match="identity_changed"):evaluate(r,pin)
    r.close()


def test_reliability_validator_cannot_mutate_pinned_plan():
    p,b,rows=matrix_setup(synthetic=False)
    def mutate(event,proof):
        event["plan"]["agents"]["agent-a"]["config"]["model"]="replacement"
        return True
    out=propose_route(p,rows,task_id="review",expected_pin=digest(p),current_bindings=b,validator=mutate,proof=None)
    assert out["policy"]["small_config"]["model"] == "local-model"


def feedback():
    return {"schema":"keel.source_feedback.v1", "observed_at":"2026-09-24T11:00:00Z", "window_start":"2026-09-23T00:00:00Z", "window_end":"2026-09-24T00:00:00Z", "telemetry_complete":True, "objective":"qualified_leads", "horizon_days":1,
        "sources":[{"source_id":s,"permitted":True,"measurement_complete":True,"cap_minutes":8,"cooldown_until":None,"cooldown_verified":True,"evidence_ref":"ref"} for s in ("a","b")],
        "observations":[{"observation_id":s,"source_id":s,"opportunity_id":s,"observed_at":"2026-09-23T12:00:00Z","qualified":True,"human_minutes":1,"application_id":None,"evidence_ref":"ref"} for s in ("a","b")],"applications":[]}


def test_source_adapter_requires_binding_then_reuses_caps_and_holds():
    p=plan(synthetic=False)
    evaluation={"schema":"keel.learning.policy-evaluation.v1","experiment_pin":digest(p),"status":"PROPOSAL_ONLY","hold_reasons":[]}
    now=datetime(2026,9,24,12,tzinfo=timezone.utc)
    out=source_budget_proposal(feedback(),evaluation,budget_minutes=10,now=now)
    assert out["status"] == "HOLD" and out["allocated_minutes"] == 0
    events=[]
    def auth(e,p):events.append(e);return True
    out=source_budget_proposal(feedback(),evaluation,budget_minutes=10,now=now,plan=p,stratum="jobs",validator=auth)
    assert out["status"] == "PROPOSAL_ONLY" and out["allocated_minutes"] == 8
    assert out["unallocated_minutes"] == 2 and out["schedule_writes"] == 0
    assert events[-1]["budget_minutes"] == 10 and events[-1]["proposal_at"] == now.isoformat()
    blocked=feedback();blocked["sources"][0]["permitted"]=False
    out=source_budget_proposal(blocked,evaluation,budget_minutes=10,now=now,plan=p,stratum="jobs",validator=auth)
    assert out["status"] == "HOLD" and out["allocated_minutes"] == 0


def test_large_verified_improvement_is_proposal_only(monkeypatch):
    # Local deterministic RNG replacement belongs only to this test process.
    # Actual production API rejects caller-supplied draw overrides.
    counter=iter([0,1]*250)
    monkeypatch.setattr("keel_learning.controller.secrets.randbelow",lambda n:next(counter))
    p=plan(synthetic=False)
    p["strata"]["jobs"]["predictions"]={"a":1,"b":0}
    r=store();pin=r.register(p,proof="host")
    for i in range(500):
        d=r.decide("sources-v1",expected_pin=pin,decision_id=f"d{i}",unit_id=f"u{i}",stratum="jobs",current_bindings=PINS,proof="host")
        r.record_outcome(f"d{i}",reward=int(d["action"]=="a"),human_minutes=1,executed_action=d["action"],observation_sha256=digest(["o",i]),current_bindings=PINS,proof="host")
    out=evaluate(r,pin)
    assert out["status"] == "PROPOSAL_ONLY" and not out["hold_reasons"]
    assert out["improvement_confidence_sequence"]["lower"] > 0
    assert not out["execution_authorized"] and out["schedule_writes"] == 0


def test_changed_logged_propensities_and_reused_receipt_fail_closed():
    r=store();pin=r.register(plan(),proof="host")
    add(r,pin,0);add(r,pin,1,draw=1)
    r.db.execute("UPDATE decisions SET probabilities=? WHERE id='d0'",(json.dumps({"a":.9,"b":.1}),));r.db.commit()
    with pytest.raises(LearningError,match="logged_propensities_changed"):evaluate(r,pin)
    r.db.execute("UPDATE decisions SET probabilities=? WHERE id='d0'",(json.dumps({"a":.5,"b":.5}),))
    raw=json.loads(r.db.execute("SELECT outcome FROM outcomes WHERE decision='d1'").fetchone()[0])
    raw["observation_sha256"]=digest(["obs",0])
    r.db.execute("UPDATE outcomes SET outcome=? WHERE decision='d1'",(json.dumps(raw),));r.db.commit()
    assert "REPEATED_OBSERVATION" in evaluate(r,pin)["hold_reasons"]
