from copy import deepcopy
import math
import random

import pytest

from keel_machine.common import MachineError,digest
from keel_machine.drift import FailureMonitor,failure_bounds

BINDINGS={k:c*64 for k,c in zip(("source_sha256","config_sha256","model_sha256","calibration_sha256"),"abcd")}


def baseline(**changes):
    result={"schema":"keel.machine.failure-baseline.v1","baseline_id":"baseline-v1","bindings":deepcopy(BINDINGS),"sample_count":10000,"failures":100,
            "tolerance":.05,"delta":.05,"min_samples":30,"max_samples":100000,"evidence_sha256":"e"*64}
    result.update(changes);return result


def monitor(tmp_path,**kw):return FailureMonitor(tmp_path/"drift.db",baseline=baseline(**kw),clock=lambda:1000)

def add(m,i,failed):return m.record(f"sample-{i}",failed,current_bindings=BINDINGS,provenance_sha256=digest(["actual-host-observation",i]))

def report(m):return m.report(current_bindings=BINDINGS)


def test_bounds_match_analytical_formula():
    result=failure_bounds(baseline_count=1000,baseline_failures=20,sample_count=100,failures=70,delta=.05)
    upper=.02+math.sqrt(math.log(40)/2000)
    lower=.7-math.sqrt(math.log(2*100*101/.05)/200)
    assert result["baseline_upper"]==pytest.approx(upper)
    assert result["failure_lower"]==pytest.approx(lower)
    assert result["increase_lower"]<=lower-upper


def test_insufficient_then_no_alarm_never_proves_no_drift(tmp_path):
    m=monitor(tmp_path)
    assert report(m)["status"]=="INSUFFICIENT_DATA"
    for i in range(30):add(m,i,False)
    out=report(m)
    assert out["status"]=="NO_ALARM" and out["sample_count"]==30
    assert out["no_drift_proven"] is False and not out["execution_authorized"] and out["route_writes"]==0


def test_confirmed_increase_sticky_across_recovery_and_restart(tmp_path):
    m=monitor(tmp_path)
    for i in range(100):add(m,i,True)
    out=report(m)
    assert out["status"]=="HOLD" and out["alarm_sequence"] is not None
    assert out["bounds"]["increase_lower"]>.05
    alarm=out["alarm_sequence"]
    for i in range(100,600):add(m,i,False)
    reopened=monitor(tmp_path)
    assert report(reopened)["alarm_sequence"]==alarm
    assert "FAILURE_RATE_INCREASE_CONFIRMED" in report(reopened)["hold_reasons"]


def test_exact_replay_deduplicates_conflicts_reject(tmp_path):
    m=monitor(tmp_path);add(m,1,True)
    assert add(m,1,True)["idempotent"]
    assert report(m)["sample_count"]==1
    with pytest.raises(MachineError,match="sample_conflict"):add(m,1,False)
    with pytest.raises(MachineError,match="repeated_observation"):
        m.record("renamed",True,current_bindings=BINDINGS,provenance_sha256=digest(["actual-host-observation",1]))
    assert report(m)["sample_count"]==1


def test_frozen_baseline_cannot_be_replaced(tmp_path):
    m=monitor(tmp_path);add(m,1,True)
    with pytest.raises(MachineError,match="baseline_changed"):monitor(tmp_path,failures=101)
    assert report(m)["sample_count"]==1


def test_current_binding_change_holds_without_mixing_measurements(tmp_path):
    m=monitor(tmp_path)
    changed=dict(BINDINGS,model_sha256="f"*64)
    result=m.record("drifted",False,current_bindings=changed,provenance_sha256="f"*64)
    assert result["status"]=="HOLD" and result["hold_reasons"]==["BINDINGS_CHANGED"]
    assert not result["recorded"] and report(m)["sample_count"]==0


def test_capacity_hold_never_silently_resets_window(tmp_path):
    m=monitor(tmp_path,min_samples=1,max_samples=2)
    add(m,0,False);add(m,1,False)
    result=add(m,2,False)
    assert result["status"]=="HOLD" and not result["recorded"]
    assert result["sample_count"]==2 and "MEASUREMENT_CAPACITY_REACHED" in result["hold_reasons"]


@pytest.mark.parametrize("kwargs",[{"failures":True},{"sample_count":0},{"failures":10001},{"delta":float("nan")},{"tolerance":True},{"min_samples":0},{"min_samples":200,"max_samples":100},{"max_samples":100001}])
def test_invalid_baselines_rejected(tmp_path,kwargs):
    with pytest.raises(MachineError):monitor(tmp_path,**kwargs)


@pytest.mark.parametrize("failed",[0,1,None,"false",float("nan")])
def test_only_measured_boolean_failure_is_accepted(tmp_path,failed):
    m=monitor(tmp_path)
    with pytest.raises(MachineError,match="boolean_outcome_required"):add(m,0,failed)
    assert report(m)["sample_count"]==0


def test_clock_regression_and_report_mutation_cannot_clear_hold(tmp_path):
    now=[1000]
    m=FailureMonitor(tmp_path/"drift.db",baseline=baseline(),clock=lambda:now[0])
    add(m,1,False)
    out=report(m);out["bindings"]["model_sha256"]="f"*64
    assert report(m)["bindings"]==BINDINGS
    now[0]=999
    assert "CLOCK_REGRESSED" in report(m)["hold_reasons"]
    with pytest.raises(MachineError,match="clock_regressed"):add(m,2,False)


def test_optional_stopping_null_simulation_regression():
    # Seeded regression, not a statistical proof. Check every look, allowing
    # a deliberately adverse stopping rule at its first false alarm.
    rng=random.Random(515)
    alarms=0
    for _ in range(200):
        base_fail=sum(rng.random()<.2 for _ in range(200))
        live_fail=0
        for n in range(1,251):
            live_fail+=rng.random()<.2
            bound=failure_bounds(baseline_count=200,baseline_failures=base_fail,sample_count=n,failures=live_fail,delta=.1)
            if bound["increase_lower"]>0:
                alarms+=1;break
    assert alarms<=20


@pytest.mark.parametrize("observer",["report","replay","binding_hold","capacity_hold"])
def test_clock_observations_persist_on_every_successful_branch(tmp_path,observer):
    now=[1000]
    m=FailureMonitor(tmp_path/"drift.db",baseline=baseline(min_samples=1,max_samples=1 if observer=="capacity_hold" else 100),clock=lambda:now[0])
    add(m,1,False)
    now[0]=2000
    if observer=="report":report(m)
    elif observer=="replay":add(m,1,False)
    elif observer=="capacity_hold":add(m,2,False)
    else:
        m.record("changed",False,current_bindings=dict(BINDINGS,model_sha256="f"*64),provenance_sha256="f"*64)
    now[0]=1500
    assert "CLOCK_REGRESSED" in report(m)["hold_reasons"]
    with pytest.raises(MachineError,match="clock_regressed"):add(m,3,False)


def test_clock_regression_before_commit_rolls_back_measurement(tmp_path):
    now=[1000]
    m=FailureMonitor(tmp_path/"drift.db",baseline=baseline(),clock=lambda:now[0])
    samples=iter([2000,1999])
    m.clock=lambda:next(samples)
    with pytest.raises(MachineError,match="clock_regressed"):add(m,1,False)
    m.clock=lambda:1000
    assert report(m)["sample_count"]==0


def test_giant_integer_clock_is_content_free_machine_error(tmp_path):
    with pytest.raises(MachineError,match="clock_invalid"):
        FailureMonitor(tmp_path/"drift.db",baseline=baseline(),clock=lambda:2**10000)
