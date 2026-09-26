"""Real SQLite + real process crashes with synthetic handlers, never providers."""
import copy
import multiprocessing
import os
from pathlib import Path

import pytest

from security.execution import ActionEnvelope, Boundary, Denied, Outcome
from security.execution.durable import DurableHostAdapter, HostValidation, SQLiteAuthority
from security.execution.host import Reservation
from keel_eval.trace_conformance import TracedHandler, capture_trace, check_trace, inspect_authority


def clock():
    return 1000


def operator(request, context, now):
    return "synthetic-operator" if context == "synthetic-session" else None


def envelope():
    return ActionEnvelope("worker", "role", "attempt", "approval", "browser.submit",
                          "https://jobs.example.com/apply", b"synthetic-content", (), 1100,
                          "nonce", "policy-v1")


def validate_host(db, action, phase, now):
    return HostValidation(action.actor, action.attempt_id, action.digest, action.policy_revision,
                          "synthetic-policy", now, now + 20, 1100, True, True, True,
                          False, False, False, "budget")


def validate_outcome(db, action, outcome, now):
    return outcome.evidence_id == "synthetic-receipt"


def reopen(path):
    authority = SQLiteAuthority(path, canonical_store_id="test-trace", operator_validator=operator, clock=clock)
    host = DurableHostAdapter(authority, host_validator=validate_host, outcome_validator=validate_outcome)
    return authority, host


def worker(path, calls_path, crash, start=None):
    authority, host = reopen(path)
    if start is not None:
        assert start.wait(5)
    if crash in ("reserve_before_commit", "dispatch_before_commit"):
        original_event = authority.event
        def event(db, kind, subject, now, detail):
            original_event(db, kind, subject, now, detail)
            if kind == ("attempt_reserved" if crash == "reserve_before_commit" else "dispatch_unknown"):
                os._exit(31)
        authority.event = event
    if crash == "after_reserve":
        original_reserve = host.reserve
        def reserve(*args):
            original_reserve(*args)
            os._exit(32)
        host.reserve = reserve
    if crash == "after_unknown":
        original_dispatch = host.begin_dispatch
        def dispatch(*args):
            original_dispatch(*args)
            os._exit(33)
        host.begin_dispatch = dispatch
    def handler(action):
        # One actual synthetic side-effect record, fsynced independently of DB.
        fd = os.open(calls_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"called\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        if crash == "inside_handler":
            os._exit(34)
        return Outcome("submitted", "synthetic-receipt")
    Boundary(host, {"browser.submit": TracedHandler(authority, handler, handler_id="synthetic-handler")},
             clock=clock).execute(envelope())


@pytest.fixture
def setup(tmp_path):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "authority.sqlite3"
    authority = SQLiteAuthority.create(path, canonical_store_id="test-trace", operator_validator=operator,
                                       operator_context="synthetic-session", clock=clock)
    authority.set_budget("budget", 3, operator_context="synthetic-session")
    _, host = reopen(path)
    host.grant_envelope(envelope(), operator_context="synthetic-session")
    return path, tmp_path / "calls", authority, host


def run_process(path, calls, crash):
    ctx = multiprocessing.get_context("fork")
    process = ctx.Process(target=worker, args=(str(path), str(calls), crash))
    process.start()
    try:
        process.join(8)
        assert not process.is_alive(), "bounded crash probe did not finish"
        assert process.exitcode == {"none": 0, "reserve_before_commit": 31, "dispatch_before_commit": 31,
                                    "after_reserve": 32, "after_unknown": 33, "inside_handler": 34}[crash]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)


def reservation_for(authority):
    row = authority.inspect_attempt("attempt")
    return Reservation(row["reservation_id"], row["envelope_digest"], row["attempt_id"])


def test_real_boundary_trace_is_consistent_and_scrubs_prose(setup):
    path, calls, authority, host = setup
    run_process(path, calls, "none")
    trace = capture_trace(authority)
    report = check_trace(trace)
    assert report["status"] == "CONFORMANT"
    assert report["handler_entry_probes"] == report["dispatch_commits"] == 1
    assert calls.read_text() == "called\n"
    assert "synthetic-content" not in str(trace)
    assert "jobs.example.com" not in str(trace)
    assert "synthetic-operator" not in str(trace)
    assert report["execution_authorized"] is False


@pytest.mark.parametrize("crash", ["reserve_before_commit", "after_reserve", "dispatch_before_commit",
                                  "after_unknown", "inside_handler"])
def test_actual_process_crash_and_restart_contract(setup, crash):
    path, calls, authority, host = setup
    run_process(path, calls, crash)
    authority, host = reopen(path)
    state = authority.inspect_attempt("attempt")
    assert inspect_authority(authority)["status"] == "CONFORMANT"
    if crash == "reserve_before_commit":
        assert state is None
        assert not calls.exists()
        assert host.reserve(envelope(), 1000).attempt_id == "attempt"
        return
    with pytest.raises(Denied):
        host.reserve(envelope(), 1000)
    reservation = reservation_for(authority)
    if crash in ("after_reserve", "dispatch_before_commit"):
        assert state["state"] == "RESERVED"
        assert not calls.exists()
        host.cancel_reserved(reservation, envelope(), reason="synthetic crash review", operator_context="synthetic-session")
        assert authority.inspect_attempt("attempt")["state"] == "NOT_SUBMITTED"
    else:
        assert state["state"] == "UNKNOWN"
        with pytest.raises(Denied):
            host.begin_dispatch(reservation, envelope(), 1000)
        with pytest.raises(Denied):
            host.cancel_reserved(reservation, envelope(), reason="must not retry unknown", operator_context="synthetic-session")
        with pytest.raises(Denied):
            host.record_outcome(reservation, envelope(), Outcome("not_submitted", "absence-is-not-proof"), 1000)
        if crash == "inside_handler":
            assert calls.read_text() == "called\n"
            host.record_outcome(reservation, envelope(), Outcome("submitted", "synthetic-receipt"), 1000)
        else:
            assert not calls.exists()
            # This validator attests only our controlled synthetic experiment.
            host.record_outcome(reservation, envelope(), Outcome("not_submitted", "synthetic-receipt"), 1000)
    assert inspect_authority(authority)["status"] == "CONFORMANT"
    with pytest.raises(Denied):
        host.reserve(envelope(), 1000)


def test_concurrent_real_workers_dispatch_once(setup):
    path, calls, authority, host = setup
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    children = [ctx.Process(target=worker, args=(str(path), str(calls), "none", start)) for _ in range(4)]
    for child in children:
        child.start()
    start.set()
    try:
        for child in children:
            child.join(8)
            assert not child.is_alive()
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(3)
    assert calls.read_text() == "called\n"
    report = inspect_authority(authority)
    assert report["status"] == "CONFORMANT"
    assert report["handler_entry_probes"] == report["dispatch_commits"] == 1


def test_wrapper_refuses_bypass_and_duplicate_entry(setup):
    path, calls, authority, host = setup
    hits = []
    traced = TracedHandler(authority, lambda a: hits.append(a), handler_id="synthetic")
    with pytest.raises(Denied):
        traced(envelope())
    reservation = host.reserve(envelope(), 1000)
    with pytest.raises(Denied):
        traced(envelope())
    host.begin_dispatch(reservation, envelope(), 1000)
    traced(envelope())
    with pytest.raises(Denied):
        traced(envelope())
    assert len(hits) == 1


def test_checker_detects_real_database_state_drift(setup):
    path, calls, authority, host = setup
    run_process(path, calls, "none")
    with authority.transaction() as (db, now):
        db.execute("UPDATE authority_attempts SET state='RESERVED',evidence_id=NULL")
    report = inspect_authority(authority)
    assert report["status"] == "BLOCKED"
    assert any(v["code"] == "event_state_disagrees_with_canonical_attempt" for v in report["violations"])


@pytest.mark.parametrize("mutation,code", [
    ("remove_unknown", "handler_before_committed_unknown"),
    ("duplicate_handler", "duplicate_handler_entry"),
    ("budget", "budget_dispatch_accounting_mismatch"),
    ("binding", "attempt_approval_binding_invalid"),
    ("source", "implementation_source_pin_mismatch"),
    ("historical_budget", "dispatch_over_historical_budget"),
    ("revocation", "admission_after_authority_revocation"),
])
def test_trace_mutants_detect_contract_violations(setup, mutation, code):
    path, calls, authority, host = setup
    run_process(path, calls, "none")
    trace = capture_trace(authority)
    if mutation == "remove_unknown":
        trace["events"] = [e for e in trace["events"] if e["kind"] != "dispatch_unknown"]
    elif mutation == "duplicate_handler":
        entry = copy.deepcopy(next(e for e in trace["events"] if e["kind"] == "trace_handler_entered"))
        entry["sequence"] = trace["events"][-1]["sequence"] + 1
        trace["events"].append(entry)
    elif mutation == "budget":
        trace["budgets"][0]["dispatched"] = 0
    elif mutation == "binding":
        trace["attempts"][0]["digest"] = "0" * 64
    elif mutation == "historical_budget":
        next(e for e in trace["events"] if e["kind"] == "budget_changed")["detail"]["maximum"] = 0
    elif mutation == "revocation":
        subject = trace["approvals"][0]["authority_subjects"][0]
        entry = {"sequence": 1, "kind": "revoked", "subject": subject["subject"], "at": 1000,
                 "detail": {"kind": subject["kind"]}}
        trace["events"].insert(1, entry)
        for n, event in enumerate(trace["events"], 1):
            event["sequence"] = n
    else:
        trace["source_pins"].clear()
    assert code in {v["code"] for v in check_trace(trace)["violations"]}


def test_sticky_rate_limit_and_pending_crash_recovery_observed(setup):
    path, calls, authority, host = setup
    reservation = host.reserve(envelope(), 1000)
    host.note_unknown(reservation, envelope(), 1000)
    host.no_dispatch_validator = lambda *args: True  # Controlled synthetic host proof only.
    host.resolve_pending_not_dispatched(reservation, envelope(), evidence_id="synthetic-proof",
                                         reason="instrumented synthetic experiment", operator_context="synthetic-session")
    authority.record_429(evidence_id="synthetic429", validate_evidence=lambda *args: True)
    assert inspect_authority(authority)["status"] == "CONFORMANT"
    trace = capture_trace(authority)
    trace["rate_limited"] = False
    assert "absolute_429_forgotten" in {v["code"] for v in check_trace(trace)["violations"]}
