from copy import deepcopy
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from keel_trust.common import digest
from keel_workbench.demo import make_demo,NOW
from keel_workbench.service import Workbench,Conflict
from test_workbench_model import with_sources


def app(doc=None,clock=None,root=None):
    return Workbench(doc or make_demo(),workspace_id="keel-demo",synthetic=True,host_clock=clock or (lambda:NOW),attachment_root=root)


def command(a,workflow="source-repair",options=None,roles=None,request_id="request-1"):
    return {"request_id":request_id,"workflow_id":workflow,"snapshot_sha256":digest(a.snapshot()),
            "role_ids":roles or [],"options":options or {}}


def test_source_plan_collapses_role_repetition_into_seven_system_tasks():
    a=app(); result=a.run(command(a))["result"]
    assert result["system_tasks"]==7 and result["human_tasks"]==0
    assert all(t["affected_roles"]==9 for t in result["tasks"])
    selected=a.run(command(a,roles=["role-2"],request_id="two"))["result"]
    assert all(t["role_ids"]==["role-2"] for t in selected["tasks"])


def test_daily_budget_never_infers_answers_or_overruns_budget():
    a=app(); r=a.run(command(a,"daily-brief",{"budget_minutes":2}))["result"]
    assert r["decisions"]["minutes"]<=2
    assert r["answers_generated"]==0 and len(r["decisions"]["selected"])==1
    assert r["decisions"]["selected"][0]["operator_reply_required"]
    assert not r["decisions"]["selected"][0]["resolved"]


def test_missing_trust_does_not_produce_a_ready_brief():
    doc=make_demo();doc["trust"]=None;a=app(doc)
    assert a.run(command(a,"daily-brief",{"budget_minutes":20}))["result"]["status"]=="TRUST_EXPORT_REQUIRED"


def test_material_review_scope_and_evidence_graph_are_explicit():
    a=app();r=a.run(command(a,"material-review",roles=["role-1"]))["result"]
    assert len(r["roles"])==1 and len(r["artifacts"])==3
    assert "workspace-wide" in r["boundary"]
    assert not any(x["release_authorized"] for x in r["artifacts"])


def test_twin_returns_actual_changed_metrics_without_mutating_input():
    a=app();original=a.snapshot()
    r=a.run(command(a,"twin-scenario",{"delay_minutes":10,"capacity_multiplier":1.5,"rate_hold_source":None,"invalidate_role":None}))["result"]
    assert r["baseline"]["runway_seconds"]==300
    assert r["scenario"]["runway_seconds"]==200
    assert r["scenario"]["first_refill_seconds"]==1800
    assert r["constraints_preserved"]["ready_floor"]==5
    assert r["forecast_calibration"]=="NOT_ESTABLISHED"
    assert not r["live_sync_connected"] and a.snapshot()==original


def test_packet_invalidation_reduces_readiness_in_twin():
    a=app();r=a.run(command(a,"twin-scenario",{"delay_minutes":0,"capacity_multiplier":1,"rate_hold_source":"source-a","invalidate_role":"role-1"}))["result"]
    assert r["scenario"]["executable_ready"]==0
    assert r["live_writes"]==0


def test_incident_replay_actually_evaluates_three_fixtures():
    a=app();r=a.run(command(a,"incident-replay"))["result"]
    assert r["status"]=="PASS" and r["cases_run"]==3
    assert not r["live_data_tested"]
    assert r["results"][1]["observed"]["forecast_state"]=="UNKNOWN"


@pytest.mark.parametrize("workflow,options,roles",[
    ("daily-brief",{"budget_minutes":True},[]),("daily-brief",{"budget_minutes":241},[]),
    ("daily-brief",{"budget_minutes":-1},[]),("daily-brief",{"budget_minutes":10},["role-1"]),
    ("source-repair",{"approve":True},[]),("source-repair",{},["unknown"]),
    ("source-repair",{},["role-1","role-1"]),("incident-replay",{},["role-1"]),
    ("not-allowed",{},[]),("twin-scenario",{},[]),
    ("twin-scenario",{"delay_minutes":0,"capacity_multiplier":0,"rate_hold_source":None,"invalidate_role":None},[]),
    ("twin-scenario",{"delay_minutes":0,"capacity_multiplier":1,"rate_hold_source":"unknown","invalidate_role":None},[]),
])
def test_unsupported_or_ambiguous_actions_are_rejected(workflow,options,roles):
    a=app()
    with pytest.raises(ValueError):a.run(command(a,workflow,options,roles))
    assert not a.history()["runs"]


def test_request_id_and_snapshot_hash_prevent_wrong_or_repeated_work():
    a=app();c=command(a);first=a.run(c)
    assert a.run(c)==first and len(a.history()["runs"])==1
    c["role_ids"]=["role-1"]
    with pytest.raises(Conflict,match="REQUEST_ID_CONFLICT"):a.run(c)
    c["snapshot_sha256"]="f"*64
    with pytest.raises(Conflict,match="SNAPSHOT_CHANGED"):a.run(c)


def test_concurrent_duplicate_requests_have_one_result():
    a=app();c=command(a)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:a.run(c),range(4)))
    assert all(r==results[0] for r in results) and len(a.history()["runs"])==1


@pytest.mark.parametrize("seconds",[-1,91])
def test_cached_report_rejects_clock_rollback_or_expiry(seconds):
    time=[NOW];a=app(clock=lambda:time[0]);c=command(a);a.run(c);time[0]+=timedelta(seconds=seconds)
    with pytest.raises(Conflict,match="REPORT_EXPIRED"):a.run(c)


def test_external_attachment_change_invalidates_cached_report(tmp_path):
    a=app(with_sources(tmp_path),root=tmp_path);c=command(a);a.run(c)
    (tmp_path/"resume.txt").write_bytes(b"new synthetic bytes")
    with pytest.raises(Conflict,match="REPORT_CONTEXT_CHANGED"):a.run(c)


def test_snapshot_compare_and_swap_is_atomic_and_session_only():
    a=app();old=a.snapshot();candidate=deepcopy(old);candidate["labels"][0]["title"]="Updated label"
    result=a.replace(candidate,previous_sha256=digest(old))
    assert result["canonical_writes"]==0 and a.snapshot()==candidate
    with pytest.raises(Conflict):a.replace(old,previous_sha256=digest(old))
    assert a.snapshot()==candidate
    bad=deepcopy(candidate);bad["flow"]["leads"][0]["fit_score"]=1000
    with pytest.raises(ValueError):a.replace(bad,previous_sha256=digest(candidate))
    assert a.snapshot()==candidate


def test_stale_import_never_changes_original_timestamps():
    d=make_demo();a=app(clock=lambda:NOW+timedelta(seconds=100))
    imported=a.replace(d,previous_sha256=digest(a.snapshot()))
    assert not imported["current"] and a.snapshot()==d


def test_session_history_is_bounded_and_restart_is_explicit():
    a=app()
    for n in range(103):a.run(command(a,request_id=f"test-{n}"))
    history=a.history();assert len(history["runs"])==100
    assert history["runs"][-1]["request_id"]=="test-3"
    assert history["persistence"]=="CURRENT_PROCESS_ONLY" and not app().history()["runs"]
