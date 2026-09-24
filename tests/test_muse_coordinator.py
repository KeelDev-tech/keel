"""Real SQLite admission, concurrent workers, crash and stale-callback checks."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3
import threading

import pytest

from keel_agent.runtime import LocalAgent
from keel_muse.coordinator import Coordinator, CoordinatorError, demo, local_agent_handler
from keel_loki.common import digest


def task(identifier="job", *, scope="scope", account="account", resource=None, priority=0):
    return {"schema": "keel.muse.task.v1", "task_id": identifier, "workspace_id": "workspace", "scope_id": scope,
            "handler_id": "review", "account_id": account, "resource_id": resource or account,
            "dependencies": {"answers": "a" * 64}, "payload": {}, "priority": priority}


def event(identifier="source", *, kind="SOURCE", scope="scope", revision="a" * 64, previous=None):
    payload = {"source_id": "answers", "revision_sha256": revision, "expected_previous_sha256": previous} if kind == "SOURCE" else {"reason": "operator-hold"} if kind == "HOLD" else {}
    return {"schema": "keel.muse.event.v1", "event_id": identifier, "workspace_id": "workspace", "scope_id": scope,
            "kind": kind, "payload": payload}


def observed(document, now=100):
    return {"schema": "keel.muse.host-snapshot.v1", **{name: document[name] for name in
        ("workspace_id", "scope_id", "account_id", "resource_id", "dependencies")},
        "consent": True, "no_ai": False, "approval_current": True, "holds": [],
        "unknown_attempt": False, "rate_limited": False, "issued_at": now, "expires_at": now + 60}


def good(context):
    return {"status": "RECORDED", "receipt_sha256": digest(context)}


def broker(home, handler=good, provider=observed, clock=lambda: 100, **limits):
    return Coordinator(home, "workspace", {"review": handler}, provider, clock=clock, **limits)


def ready(home, handler=good, provider=observed, **limits):
    coordinator = broker(home, handler, provider, **limits)
    coordinator.ingest(event())
    coordinator.enqueue(task())
    return coordinator


def test_missing_sources_idle_never_invokes_provider_or_handler(tmp_path):
    def forbidden(*args):
        pytest.fail("blocked idle work must not invoke callbacks")
    coordinator = broker(tmp_path / "home", forbidden, forbidden)
    coordinator.enqueue(task())
    for _ in range(3):
        assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    assert coordinator.snapshot()["state"]["calls_reserved"] == 0


def test_duplicate_events_return_identical_receipt_without_reobserving(tmp_path):
    coordinator = broker(tmp_path / "home")
    first = coordinator.ingest(event())
    before = coordinator.snapshot()
    assert coordinator.ingest(event()) == first
    assert coordinator.snapshot() == before
    changed = event(revision="b" * 64)
    with pytest.raises(CoordinatorError, match="payload_conflict"):
        coordinator.ingest(changed)
    assert coordinator.snapshot() == before


def test_source_compare_and_swap_rejects_late_event(tmp_path):
    coordinator = ready(tmp_path / "home")
    coordinator.ingest(event("new", revision="b" * 64, previous="a" * 64))
    before = coordinator.snapshot()
    with pytest.raises(CoordinatorError, match="cursor_conflict"):
        coordinator.ingest(event("late", revision="c" * 64, previous="a" * 64))
    assert coordinator.snapshot() == before
    assert before["state"]["tasks"]["job"]["status"] == "BLOCKED"


def test_source_event_wakes_one_task_and_coalesces_redundant_rechecks(tmp_path):
    calls = []
    coordinator = broker(tmp_path / "home", lambda context: calls.append(context) or good(context))
    coordinator.enqueue(task())
    coordinator.ingest(event())
    for i in range(10):
        coordinator.ingest(event("wake-" + str(i), kind="WAKE"))
    assert coordinator.worker_once("worker")["status"] == "RECORDED"
    coordinator.ingest(event("after", kind="WAKE"))
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    assert len(calls) == 1
    assert coordinator.snapshot()["state"]["wakeups_coalesced"] >= 10


@pytest.mark.parametrize("name,value", [("approval_current", False), ("no_ai", True), ("consent", False),
    ("unknown_attempt", True), ("holds", ["hold"]), ("consent", 1), ("account_id", "other"),
    ("issued_at", 101), ("expires_at", 100)])
def test_host_gates_block_without_handler_invocation(tmp_path, name, value):
    calls = []
    def provider(document):
        result = observed(document); result[name] = value
        return result
    coordinator = ready(tmp_path / "home", lambda context: calls.append(context) or good(context), provider)
    result = coordinator.worker_once("worker")
    assert result["status"] == "BLOCKED" and result["handler_calls_attempted"] == 0
    assert calls == [] and coordinator.snapshot()["state"]["calls_reserved"] == 0


def test_second_canonical_read_can_block_after_intent_without_callback(tmp_path):
    reads, calls = [], []
    def provider(document):
        reads.append(1)
        result = observed(document)
        if len(reads) == 2:
            result["approval_current"] = False
        return result
    coordinator = ready(tmp_path / "home", lambda context: calls.append(1) or good(context), provider)
    result = coordinator.worker_once("worker")
    assert result["status"] == "BLOCKED" and not calls
    assert len(reads) == 2 and coordinator.snapshot()["state"]["calls_reserved"] == 1


def test_source_change_between_snapshot_and_admission_blocks_callback(tmp_path):
    reads, calls = [], []
    coordinator = None
    def provider(document):
        reads.append(1)
        if len(reads) == 2:
            coordinator.ingest(event("correction", revision="b" * 64, previous="a" * 64))
        return observed(document)
    coordinator = ready(tmp_path / "home", lambda context: calls.append(1) or good(context), provider)
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    assert not calls
    assert coordinator.snapshot()["state"]["tasks"]["job"]["status"] == "UNKNOWN"


def test_two_workers_cannot_invoke_same_task(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def handler(context):
        calls.append(context["intent_id"]); entered.set()
        assert release.wait(5)
        return good(context)
    coordinator = ready(tmp_path / "home", handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.worker_once, "a")
        assert entered.wait(5)
        second = pool.submit(coordinator.worker_once, "b")
        try:
            assert second.result(timeout=5)["handler_calls_attempted"] == 0
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "RECORDED"
    assert len(calls) == 1


@pytest.mark.parametrize("shared", ["account", "browser"])
def test_account_and_browser_resources_are_independently_exclusive(tmp_path, shared):
    entered, release = threading.Event(), threading.Event()
    def handler(context):
        entered.set(); assert release.wait(5)
        return good(context)
    coordinator = ready(tmp_path / "home", handler)
    coordinator.enqueue(task("second", account="account" if shared == "account" else "other",
                             resource="other-browser" if shared == "account" else "account"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(coordinator.worker_once, "first")
        assert entered.wait(5)
        try:
            assert coordinator.worker_once("second")["handler_calls_attempted"] == 0
        finally:
            release.set()
        active.result(timeout=5)


def test_concurrency_limit_does_not_serialize_unrelated_resources_unnecessarily(tmp_path):
    entered = threading.Barrier(3); release = threading.Event()
    def handler(context):
        entered.wait(timeout=5); assert release.wait(5)
        return good(context)
    coordinator = ready(tmp_path / "home", handler, max_running=2)
    coordinator.enqueue(task("second", account="other"))
    coordinator.enqueue(task("third", account="third"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(coordinator.worker_once, name) for name in ("a", "b")]
        entered.wait(timeout=5)
        try:
            assert coordinator.worker_once("c")["reason"] == "concurrency_limit"
        finally:
            release.set()
        assert all(worker.result(timeout=5)["status"] == "RECORDED" for worker in workers)


def test_actual_process_crash_after_intent_preserves_unknown_resource_hold(tmp_path):
    home = tmp_path / "crash"
    pid = os.fork()
    if pid == 0:
        try:
            coordinator = ready(home, lambda context: os._exit(73))
            coordinator.worker_once("dead-worker")
        except BaseException:
            os._exit(99)
        os._exit(98)
    _, result = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(result) == 73
    coordinator = broker(home)
    snapshot = coordinator.snapshot()["state"]
    assert snapshot["tasks"]["job"]["status"] == "UNKNOWN"
    assert snapshot["resources"]["account:account"]["uncertain"] is True
    coordinator.enqueue(task("another"))
    assert coordinator.worker_once("new-worker")["handler_calls_attempted"] == 0
    assert snapshot["calls_reserved"] == 1


def test_late_429_after_controller_restart_still_persists_global_stop(tmp_path):
    entered, release = threading.Event(), threading.Event()
    home = tmp_path / "home"
    def handler(context):
        entered.set(); assert release.wait(5)
        return {"status": "RATE_429", "receipt_sha256": None}
    original = ready(home, handler)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(original.worker_once, "old")
        assert entered.wait(5)
        restarted = broker(home)
        release.set()
        result = future.result(timeout=5)
    assert result["status"] == "UNKNOWN"
    assert restarted.snapshot()["state"]["rate_limited"] is True
    with pytest.raises(CoordinatorError, match="controller_fenced"):
        original.worker_once("old-again")


def test_valid_host_429_is_sticky_even_before_any_handler(tmp_path):
    calls = []
    def provider(document):
        return dict(observed(document), rate_limited=True)
    coordinator = ready(tmp_path / "home", lambda context: calls.append(1) or good(context), provider)
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    assert not calls and coordinator.snapshot()["state"]["rate_limited"]
    assert broker(tmp_path / "home").snapshot()["state"]["rate_limited"]


def test_exception_is_unknown_and_cannot_automatically_retry(tmp_path):
    def failure(context):
        raise RuntimeError("sensitive callback detail")
    coordinator = ready(tmp_path / "home", failure)
    result = coordinator.worker_once("worker")
    assert result["status"] == "UNKNOWN"
    assert "sensitive callback" not in json.dumps(result)
    coordinator.ingest(event("wake", kind="WAKE"))
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0


def test_handler_blocked_is_terminal_observation_not_silent_automatic_retry(tmp_path):
    coordinator = ready(tmp_path / "home", lambda context: {"status": "BLOCKED", "receipt_sha256": None})
    result = coordinator.worker_once("worker")
    assert result["status"] == "RECORDED" and result["outcome"]["status"] == "BLOCKED"
    coordinator.ingest(event("wake", kind="WAKE"))
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0


def test_budget_and_configuration_survive_restart(tmp_path):
    home = tmp_path / "home"
    coordinator = ready(home, max_calls=1)
    assert coordinator.worker_once("worker")["status"] == "RECORDED"
    coordinator.enqueue(task("second", account="another"))
    coordinator = broker(home, max_calls=1)
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    with pytest.raises(CoordinatorError, match="configuration_mismatch"):
        broker(home, max_calls=2)


def test_fairness_gives_unserved_account_a_turn(tmp_path):
    order = []
    coordinator = ready(tmp_path / "home", lambda context: order.append(context["task"]["task_id"]) or good(context))
    coordinator.enqueue(task("high", priority=10))
    coordinator.enqueue(task("other", account="another", priority=0))
    coordinator.worker_once("one"); coordinator.worker_once("two")
    assert order == ["high", "other"]


def test_queue_backpressure_and_task_id_payload_conflicts(tmp_path):
    coordinator = broker(tmp_path / "home", max_pending=1)
    coordinator.enqueue(task())
    assert coordinator.enqueue(task())["duplicate"] is True
    with pytest.raises(CoordinatorError, match="payload_conflict"):
        coordinator.enqueue(task(priority=1))
    with pytest.raises(CoordinatorError, match="backpressure"):
        coordinator.enqueue(task("another"))


def test_readonly_clients_preserve_active_controller_epoch(tmp_path):
    home = tmp_path / "home"
    coordinator = ready(home)
    before = coordinator.snapshot()
    reader = Coordinator.open_readonly(home, "workspace")
    assert reader.snapshot() == before
    with pytest.raises(CoordinatorError, match="read_only"):
        reader.enqueue(task("another"))
    assert coordinator.worker_once("worker")["status"] == "RECORDED"
    assert reader.snapshot()["state"]["tasks"]["job"]["status"] == "RECORDED"


@pytest.mark.parametrize("target", ["state", "history", "inbox"])
def test_unkeyed_tampering_blocks_snapshot_and_checkpoint_validation(tmp_path, target):
    home = tmp_path / "home"
    coordinator = ready(home)
    with sqlite3.connect(home / "coordinator.sqlite3") as db:
        if target == "state":
            document = json.loads(db.execute("SELECT payload FROM muse_state").fetchone()[0])
            document["calls_reserved"] = 0; document["rate_limited"] = True
            db.execute("UPDATE muse_state SET payload=?", (json.dumps(document),))
        elif target == "history":
            db.execute("UPDATE muse_journal SET sha256=? WHERE sequence=1", ("f" * 64,))
        else:
            db.execute("UPDATE muse_inbox SET payload_sha256=?", ("f" * 64,))
    with pytest.raises(CoordinatorError):
        coordinator.snapshot()


def test_existing_local_agent_worker_is_invoked_only_with_workspace_queue_binding(tmp_path):
    agent = LocalAgent(tmp_path / "agent", "workspace")
    adapter = local_agent_handler(agent, "worker")
    document = task(scope="local-agent:workspace", account="local-agent:workspace",
                    resource="local-agent:" + digest(str(agent.home)))
    assert adapter({"task": document})["status"] == "RECORDED"
    with pytest.raises(CoordinatorError, match="queue_scope"):
        adapter({"task": task()})


def test_demo_exercises_persistent_control_state_without_network(tmp_path):
    result = demo(tmp_path / "demo")
    assert result["status"] == "PASS" and all(result["checks"].values())
    assert result["model_calls"] == result["network_calls"] == result["external_actions"] == 0


def test_existing_recovery_journal_is_read_and_can_only_restrict_host_gates(tmp_path):
    from keel_loki.recovery import RecoveryJournal
    from keel_muse.coordinator import recovery_guarded_provider
    journal = RecoveryJournal(tmp_path / "recovery", "workspace", now=100)
    journal.register("job", "a" * 64, now=100)
    journal.approve("job", "a" * 64, "b" * 64, now=100)
    before = journal.snapshot()
    provider = recovery_guarded_provider(observed, RecoveryJournal.open_readonly(tmp_path / "recovery", "workspace"),
                                         {"scope": "job"}, revision_source_id="answers")
    assert provider(task())["approval_current"] is True
    assert journal.snapshot() == before
    journal.revoke("job", now=100)
    assert provider(task())["approval_current"] is False
    coordinator = ready(tmp_path / "broker", provider=provider)
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    journal.record_429(now=100)
    assert provider(task())["rate_limited"] is True
    with pytest.raises(CoordinatorError, match="scope_unbound"):
        provider(task(scope="wrong"))


def test_delayed_transaction_acquisition_rechecks_expired_lease_before_callback(tmp_path):
    """Reproduce admission waiting after its first clock sample, without sleeps."""
    from contextlib import contextmanager
    clock, calls = [100], []
    coordinator = ready(tmp_path / 'home', lambda context: calls.append(clock[0]) or good(context),
                        clock=lambda: clock[0])
    original = coordinator._local._transaction
    transactions = [0]
    @contextmanager
    def delayed(now):
        transactions[0] += 1
        with original(now) as value:
            if transactions[0] == 3:  # callback_admitted acquires its transaction
                clock[0] = 161
            yield value
    coordinator._local._transaction = delayed
    result = coordinator.worker_once('worker')
    assert result['handler_calls_attempted'] == 0
    assert calls == []
    assert result['status'] == 'UNKNOWN'
    state = coordinator.snapshot()['state']
    assert state['tasks']['job']['status'] == 'UNKNOWN'
    assert all(resource['uncertain'] for resource in state['resources'].values())
    assert coordinator.worker_once('other')['handler_calls_attempted'] == 0


def test_slow_admission_commit_rechecks_host_expiry_at_callback_boundary(tmp_path):
    """A valid lease cannot outlive a shorter canonical policy observation."""
    from contextlib import contextmanager
    clock, calls = [100], []
    coordinator = ready(tmp_path / 'home', lambda context: calls.append(clock[0]) or good(context),
                        clock=lambda: clock[0])
    original = coordinator._local._transaction
    transactions = [0]
    @contextmanager
    def delayed(now):
        transactions[0] += 1
        with original(now) as value:
            yield value
        if transactions[0] == 3:  # commit finishes after policy expiry
            clock[0] = 161
    coordinator._local._transaction = delayed
    result = coordinator.worker_once('worker', lease_seconds=300)
    assert result['handler_calls_attempted'] == 0
    assert calls == [] and result['status'] == 'UNKNOWN'
    assert coordinator.snapshot()['state']['tasks']['job']['status'] == 'UNKNOWN'


def test_slow_final_state_read_rechecks_expiry_after_read(tmp_path):
    clock, calls = [100], []
    coordinator = ready(tmp_path / 'home', lambda context: calls.append(clock[0]) or good(context),
                        clock=lambda: clock[0])
    original = coordinator.snapshot
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 161
        return result
    coordinator.snapshot = delayed
    result = coordinator.worker_once('worker', lease_seconds=300)
    assert result['handler_calls_attempted'] == 0
    assert result['status'] == 'UNKNOWN' and calls == []
