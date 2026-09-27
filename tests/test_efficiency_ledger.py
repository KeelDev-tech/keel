"""Resource admission invariants across process races and interrupted dispatch."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
import os
import sqlite3

import pytest

from keel_efficiency.ledger import (
    BudgetExceeded, ConflictError, LedgerError, MAX_INTEGER, RESOURCES, ResourceLedger,
)


def vector(**values):
    return {name: values.get(name, 0) for name in RESOURCES}


@pytest.fixture
def ledger(tmp_path):
    result = ResourceLedger(tmp_path / "ledger.sqlite")
    result.create_scope("global", vector(calls=20, input_tokens=1000, output_tokens=1000, compute_ms=10000))
    result.create_scope("session", vector(calls=10, input_tokens=500, output_tokens=500, compute_ms=5000), "global")
    result.create_scope("task", vector(calls=5, input_tokens=200, output_tokens=200, compute_ms=2000), "session")
    return result


def test_hierarchy_atomic_reserve_settle_and_reopen(ledger):
    estimate = vector(calls=1, input_tokens=100, output_tokens=20, compute_ms=200)
    reservation = ledger.reserve("r1", "task", estimate, {"model": "local", "subject": "exact-input-hash"})
    assert reservation["state"] == "RESERVED"
    for scope in ("global", "session", "task"):
        assert ledger.snapshot(scope)["reserved"] == estimate
    ledger.mark_dispatched("r1")
    actual = vector(calls=1, input_tokens=50, output_tokens=12, compute_ms=111)
    assert ledger.settle("r1", actual)["state"] == "SETTLED"
    reopened = ResourceLedger(ledger.path)
    for scope in ("global", "session", "task"):
        snapshot = reopened.snapshot(scope)
        assert snapshot["reserved"] == vector()
        assert snapshot["used"] == actual
    assert json.loads(json.dumps(reopened.snapshot())) == reopened.snapshot()


def test_parent_shared_limit_blocks_sibling_without_partial_reservation(ledger):
    ledger.create_scope("other", {"calls": 20}, "session")
    ledger.reserve("r1", "other", {"calls": 9})
    before = ledger.snapshot()
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r2", "task", {"calls": 2})
    assert ledger.snapshot() == before
    ledger.reserve("r3", "task", {"calls": 1})
    assert ledger.snapshot("global")["reserved"]["calls"] == 10


def test_paid_default_zero_and_no_unit_conversion(ledger):
    with pytest.raises(BudgetExceeded):
        ledger.reserve("paid", "task", {"external_credit_micros": 1})
    assert ledger.snapshot("global")["used"] == vector()
    ledger.create_scope("explicit-paid-window", {"external_credit_micros": 500})
    ledger.reserve("approved-credit", "explicit-paid-window", {"external_credit_micros": 500})
    assert ledger.snapshot("explicit-paid-window")["reserved"]["external_credit_micros"] == 500


def test_partial_settlement_retains_unknown_then_reconciles(ledger):
    ledger.reserve("r", "task", vector(calls=1, input_tokens=100, output_tokens=20, compute_ms=200))
    ledger.mark_dispatched("r")
    partial = ledger.settle("r", {"calls": 1, "compute_ms": 100, "external_credit_micros": 0})
    assert partial["state"] == "UNKNOWN"
    assert partial["remaining"] == vector(input_tokens=100, output_tokens=20)
    before = ledger.snapshot()
    assert ledger.settle("r", {"calls": 1, "input_tokens": None})["state"] == "UNKNOWN"
    assert ledger.snapshot() == before
    with pytest.raises(ConflictError):
        ledger.cancel("r")
    reopened = ResourceLedger(ledger.path)
    completed = reopened.reconcile("r", {"input_tokens": 60, "output_tokens": 12})
    assert completed["state"] == "SETTLED"
    assert reopened.snapshot("global")["reserved"] == vector()
    assert completed["usage"] == vector(calls=1, input_tokens=60, output_tokens=12, compute_ms=100)
    before = reopened.snapshot()
    reopened.reconcile("r", {"input_tokens": 60})
    assert reopened.snapshot() == before
    with pytest.raises(ConflictError):
        reopened.reconcile("r", {"input_tokens": 61})
    assert reopened.snapshot() == before


def test_unknown_does_not_expire_or_release_budget(ledger):
    ledger.reserve("r", "task", {"calls": 5})
    ledger.mark_dispatched("r")
    ledger.mark_unknown("r", "transport timed out after dispatch")
    for _ in range(2):
        reopened = ResourceLedger(ledger.path)
        assert reopened.request("r")["state"] == "UNKNOWN"
        assert reopened.snapshot("task")["reserved"]["calls"] == 5
        with pytest.raises(BudgetExceeded):
            reopened.reserve("next", "task", {"calls": 1})


def test_overage_records_actual_locks_ancestors_and_prior_pending_dispatch(ledger):
    ledger.reserve("r", "task", {"calls": 1, "input_tokens": 10})
    ledger.reserve("pending", "task", {"calls": 1})
    ledger.mark_dispatched("r")
    result = ledger.settle("r", vector(calls=1, input_tokens=11))
    assert result["overages"] == {"input_tokens": 1}
    for scope in ("task", "session", "global"):
        snapshot = ledger.snapshot(scope)
        assert snapshot["used"]["input_tokens"] == 11
        assert snapshot["locked"]
        with pytest.raises(BudgetExceeded):
            ledger.reserve("no-" + scope, scope, {})
    with pytest.raises(BudgetExceeded):
        ledger.mark_dispatched("pending")
    ledger.cancel("pending")
    assert ledger.snapshot("global")["locked"]
    assert ledger.snapshot("global")["reserved"] == vector()


def test_actual_can_exceed_scope_limit_without_losing_truth(ledger):
    ledger.reserve("r", "task", {"calls": 5})
    ledger.mark_dispatched("r")
    ledger.settle("r", vector(calls=21))
    assert ledger.snapshot("global")["used"]["calls"] == 21
    assert ledger.snapshot("global")["available"]["calls"] == 0
    assert ledger.snapshot("global")["locked"]


def test_cancel_only_before_dispatch_and_request_identity_never_reused(ledger):
    first = ledger.reserve("r", "task", {"calls": 1}, {"input": "a"})
    assert ledger.reserve("r", "task", {"calls": 1}, {"input": "a"}) == first
    for args in (("task", {"calls": 2}, {"input": "a"}),
                 ("session", {"calls": 1}, {"input": "a"}),
                 ("task", {"calls": 1}, {"input": "b"})):
        with pytest.raises(ConflictError):
            ledger.reserve("r", *args)
    cancelled = ledger.cancel("r")
    assert cancelled["state"] == "CANCELLED"
    assert ledger.cancel("r") == cancelled
    assert ledger.reserve("r", "task", {"calls": 1}, {"input": "a"}) == cancelled
    with pytest.raises(ConflictError):
        ledger.mark_dispatched("r")
    assert ledger.snapshot("global")["reserved"] == vector()


def test_configuration_immutable_parent_no_reset(ledger):
    before = ledger.snapshot("global")
    assert ledger.create_scope("global", before["limits"]) == before
    with pytest.raises(ConflictError):
        ledger.create_scope("global", {"calls": 100000})
    with pytest.raises(ConflictError):
        ledger.create_scope("task", ledger.snapshot("task")["limits"], "global")
    with pytest.raises(LedgerError):
        ledger.create_scope("missing-child", {}, "missing")
    with pytest.raises(LedgerError):
        ledger.create_scope("self", {}, "self")
    assert ledger.snapshot("global") == before


@pytest.mark.parametrize("resources", [
    None, [], {"unknown": 1}, {"calls": True}, {"calls": -1}, {"calls": 1.0},
    {"calls": "1"}, {"calls": MAX_INTEGER + 1}, {"calls": float("nan")}, {"calls": None},
])
def test_resource_validation_has_no_mutation(ledger, resources):
    before = ledger.snapshot()
    with pytest.raises(LedgerError):
        ledger.reserve("bad", "task", resources)
    assert ledger.snapshot() == before


@pytest.mark.parametrize("metadata", [
    [], {1: "key"}, {"value": float("inf")}, {"value": 2**53}, {"value": {1, 2}},
    {"value": (1, 2)}, {"value": "x" * 16385}, {"value": b"bytes"},
])
def test_metadata_is_bounded_strict_json(ledger, metadata):
    before = ledger.snapshot()
    with pytest.raises(LedgerError):
        ledger.reserve("bad", "task", {}, metadata)
    assert ledger.snapshot() == before


def test_metadata_cycle_depth_and_aliasing(ledger):
    cycle = {}
    cycle["self"] = cycle
    deep = []
    for _ in range(20):
        deep = [deep]
    for metadata in (cycle, {"deep": deep}, {"many": [0] * 4097}):
        with pytest.raises(LedgerError):
            ledger.reserve("bad", "task", {}, metadata)
    original = {"items": [{"id": "a"}]}
    ledger.reserve("good", "task", {}, original)
    original["items"][0]["id"] = "mutated"
    returned = ledger.request("good")
    returned["metadata"]["items"][0]["id"] = "also-mutated"
    assert ledger.request("good")["metadata"]["items"][0]["id"] == "a"


def test_invalid_transitions_leave_accounting_unchanged(ledger):
    ledger.reserve("r", "task", {"calls": 1})
    before = ledger.snapshot()
    for call in (lambda: ledger.settle("r", vector(calls=1)),
                 lambda: ledger.reconcile("r", vector(calls=1)),
                 lambda: ledger.mark_unknown("r")):
        with pytest.raises(ConflictError):
            call()
    assert ledger.snapshot() == before
    ledger.mark_dispatched("r")
    before = ledger.snapshot()
    for call in (lambda: ledger.mark_dispatched("r"), lambda: ledger.cancel("r")):
        with pytest.raises(ConflictError):
            call()
    assert ledger.snapshot() == before


def _process_reserve(args):
    path, index = args
    ledger = ResourceLedger(path)
    try:
        ledger.reserve("process-" + str(index), "root", {"calls": 1})
        return True
    except BudgetExceeded:
        return False


def test_multiprocess_budget_admission_cannot_oversubscribe(tmp_path):
    ledger = ResourceLedger(tmp_path / "multi.sqlite")
    ledger.create_scope("root", {"calls": 7})
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_process_reserve, [(ledger.path, i) for i in range(30)]))
    assert sum(results) == 7
    assert ledger.snapshot("root")["reserved"]["calls"] == 7
    assert ledger.snapshot()["requests_by_state"] == {"RESERVED": 7}


def test_shared_request_only_one_dispatch_authorization(ledger):
    def attempt(_):
        ledger.reserve("shared", "task", {"calls": 1})
        try:
            ledger.mark_dispatched("shared")
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(24)))
    assert sum(outcomes) == 1
    assert ledger.snapshot("task")["reserved"]["calls"] == 1


def _crash_after_dispatch(path):
    ledger = ResourceLedger(path)
    ledger.reserve("crashed", "root", {"calls": 1, "input_tokens": 25})
    ledger.mark_dispatched("crashed")
    os._exit(23)


def _crash_uncommitted(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE efficiency_scopes SET reserved_json=? WHERE scope_id='root'", (json.dumps(vector(calls=999)),))
    os._exit(24)


def test_process_crash_after_dispatch_retains_hold_and_cannot_retry(tmp_path):
    path = tmp_path / "crash.sqlite"
    ledger = ResourceLedger(path)
    ledger.create_scope("root", {"calls": 1, "input_tokens": 25})
    process = multiprocessing.Process(target=_crash_after_dispatch, args=(str(path),))
    process.start()
    process.join(10)
    assert process.exitcode == 23
    reopened = ResourceLedger(path)
    assert reopened.request("crashed")["state"] == "DISPATCHED"
    assert reopened.snapshot("root")["reserved"] == vector(calls=1, input_tokens=25)
    with pytest.raises(ConflictError):
        reopened.mark_dispatched("crashed")
    with pytest.raises(ConflictError):
        reopened.cancel("crashed")
    with pytest.raises(BudgetExceeded):
        reopened.reserve("retry", "root", {"calls": 1})


def test_process_crash_inside_transaction_rolls_back(tmp_path):
    path = tmp_path / "rollback.sqlite"
    ledger = ResourceLedger(path)
    ledger.create_scope("root", {"calls": 1})
    process = multiprocessing.Process(target=_crash_uncommitted, args=(str(path),))
    process.start()
    process.join(10)
    assert process.exitcode == 24
    assert ResourceLedger(path).snapshot("root")["reserved"] == vector()


@pytest.mark.parametrize("path", [":memory:", "file:ledger.sqlite?mode=memory", "", "bad\x00path"])
def test_nonpersistent_or_invalid_paths_rejected(path):
    with pytest.raises(LedgerError):
        ResourceLedger(path)
