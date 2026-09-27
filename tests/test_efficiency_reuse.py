"""Concurrency, crash fencing, integrity and selective recomputation regressions."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import sqlite3
import threading

import pytest

from keel_efficiency.reuse import SingleFlightStore, build_reuse_key, plan_incremental
from keel_machine.common import MachineError


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


PIN = fingerprint("pure-parse-v1")


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


def key(**updates):
    arguments = dict(account_id="trent", scope="workspace", purpose="analysis",
                     operation="parse", implementation_sha256=PIN,
                     model_sha256=fingerprint("no-model"),
                     source_sha256=fingerprint("source-v1"),
                     policy_sha256=fingerprint("policy-v1"),
                     input_sha256=fingerprint("input-v1"),
                     dependencies={"evidence": fingerprint("evidence-v1")})
    arguments.update(updates)
    return build_reuse_key(**arguments)


def credentials(claim):
    return {name: claim[name] for name in ("owner_id", "lease_token", "fence")}


@pytest.fixture
def setup_store(tmp_path):
    tmp_path.chmod(0o700)
    clock = Clock()
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN}, clock=clock)
    return store, clock


def test_complete_and_reuse_are_inert_and_exact(setup_store):
    store, _ = setup_store
    claim = store.claim(key(), "worker1")
    assert claim["status"] == "CLAIMED"
    assert store.claim(key(), "worker2")["status"] == "WAIT"
    completed = store.complete(key(), **credentials(claim), value={"parsed": [1, 2]})
    assert completed["status"] == "STORED"
    hit = store.claim(key(), "worker3")
    assert hit["status"] == "HIT" and hit["value"] == {"parsed": [1, 2]}
    assert hit["execution_authorized"] is False
    assert "lease_token" not in hit


@pytest.mark.parametrize("field,value", [
    ("account_id", "someone_else"), ("scope", "other_scope"), ("purpose", "test"),
    ("model_sha256", fingerprint("other-model")),
    ("source_sha256", fingerprint("other-source")),
    ("policy_sha256", fingerprint("other-policy")),
    ("input_sha256", fingerprint("other-input")),
    ("dependencies", {"evidence": fingerprint("new-evidence")})])
def test_boundaries_prevent_cross_context_hits(setup_store, field, value):
    store, _ = setup_store
    claim = store.claim(key(), "worker1")
    store.complete(key(), **credentials(claim), value={"private": "result"})
    assert store.claim(key(**{field: value}), "worker2")["status"] == "CLAIMED"


@pytest.mark.parametrize("purpose", ["approval", "execution", "submit", "action"])
def test_external_actions_and_approvals_are_not_reusable(purpose):
    with pytest.raises(MachineError, match="reuse_purpose_not_pure"):
        key(purpose=purpose)


def test_only_host_registered_implementation_is_accepted(setup_store):
    store, _ = setup_store
    for candidate in (key(operation="submit"), key(implementation_sha256=fingerprint("new-code"))):
        with pytest.raises(MachineError, match="reuse_operation_not_registered"):
            store.claim(candidate, "worker")


def test_cross_worker_concurrent_claim_has_one_leader(tmp_path):
    tmp_path.chmod(0o700)
    workers = [SingleFlightStore(tmp_path / "shared.sqlite", operations={"parse": PIN})
               for _ in range(12)]
    barrier = threading.Barrier(len(workers))

    def acquire(number):
        barrier.wait()
        return workers[number].claim(key(), f"worker{number}")

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        reports = list(pool.map(acquire, range(len(workers))))
    leaders = [report for report in reports if report["status"] == "CLAIMED"]
    assert len(leaders) == 1
    assert sum(report["status"] == "WAIT" for report in reports) == 11
    workers[0].complete(key(), **credentials(leaders[0]), value={"calls": 1})
    assert all(worker.claim(key(), "reader")["status"] == "HIT" for worker in workers)


def test_crash_recovery_rejects_stale_completion_and_failure(setup_store):
    store, clock = setup_store
    old = store.claim(key(), "old", lease_seconds=10)
    clock.now += 10
    new = store.claim(key(), "new", lease_seconds=20)
    assert new["recovered"] is True and new["fence"] > old["fence"]
    assert store.complete(key(), **credentials(old), value="old")["status"] == "STALE"
    assert store.fail(key(), **credentials(old))["status"] == "STALE"
    assert store.heartbeat(key(), **credentials(old))["status"] == "STALE"
    assert store.complete(key(), **credentials(new), value="new")["status"] == "STORED"
    assert store.claim(key(), "reader")["value"] == "new"


def test_completed_result_survives_new_process_object(setup_store):
    store, clock = setup_store
    claim = store.claim(key(), "worker")
    store.complete(key(), **credentials(claim), value="persisted")
    reopened = SingleFlightStore(store.db.path, operations={"parse": PIN}, clock=clock)
    assert reopened.claim(key(), "reader")["value"] == "persisted"


def test_failure_releases_lease_without_aba_overwrite(setup_store):
    store, _ = setup_store
    old = store.claim(key(), "worker")
    assert store.fail(key(), **credentials(old))["status"] == "RELEASED"
    new = store.claim(key(), "worker")
    assert new["fence"] > old["fence"] and new["lease_token"] != old["lease_token"]
    assert store.complete(key(), **credentials(old), value="old")["status"] == "STALE"


def test_expired_artifact_is_recomputed(setup_store):
    store, clock = setup_store
    claim = store.claim(key(), "worker")
    store.complete(key(), **credentials(claim), value="old", ttl_seconds=10)
    clock.now += 10
    assert store.claim(key(), "worker")["status"] == "CLAIMED"


def test_heartbeat_never_extends_absolute_run_deadline(tmp_path):
    tmp_path.chmod(0o700)
    clock = Clock()
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN},
                              clock=clock, max_run_seconds=30)
    claim = store.claim(key(), "worker", lease_seconds=20)
    clock.now += 15
    renewed = store.heartbeat(key(), **credentials(claim), lease_seconds=30)
    assert renewed["lease_until"] == renewed["run_until"] == 1030
    clock.now = 1030
    assert store.heartbeat(key(), **credentials(claim))["status"] == "STALE"


def test_count_budget_pins_active_work_and_evicts_expired_pure_work(tmp_path):
    tmp_path.chmod(0o700)
    clock = Clock()
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN},
                              clock=clock, max_entries=1)
    first = store.claim(key(), "worker1", lease_seconds=10)
    second_key = key(input_sha256=fingerprint("second"))
    assert store.claim(second_key, "worker2")["reason"] == "REUSE_CAPACITY_HELD"
    clock.now += 10
    assert store.claim(second_key, "worker2")["status"] == "CLAIMED"
    assert store.complete(key(), **credentials(first), value="old")["status"] == "STALE"
    assert store.stats()["entries"] == 1


def test_byte_budget_rejects_oversized_artifact_without_publishing(tmp_path):
    tmp_path.chmod(0o700)
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN}, max_bytes=1200)
    claim = store.claim(key(), "worker")
    report = store.complete(key(), **credentials(claim), value="x" * 1000)
    assert report["status"] == "HELD" and report["reason"] == "REUSE_CAPACITY_HELD"
    assert store.claim(key(), "reader")["status"] == "WAIT"
    assert store.stats()["bytes"] <= 1200


def test_impossible_artifact_does_not_evict_existing_reusable_work(tmp_path):
    tmp_path.chmod(0o700)
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN}, max_bytes=2400)
    existing = store.claim(key(), "worker1")
    store.complete(key(), **credentials(existing), value="keep-me")
    other = key(input_sha256=fingerprint("other"))
    claim = store.claim(other, "worker2")
    assert store.complete(other, **credentials(claim), value="x" * 3000)["status"] == "HELD"
    assert store.claim(key(), "reader")["value"] == "keep-me"


def test_abandoned_run_is_pruned_after_absolute_retention_limit(tmp_path):
    tmp_path.chmod(0o700)
    clock = Clock()
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN},
                              clock=clock, max_run_seconds=30)
    claim = store.claim(key(), "worker")
    clock.now += 30
    assert store.stats()["entries"] == 0
    assert store.complete(key(), **credentials(claim), value="too-late")["status"] == "STALE"


def test_lru_eviction_preserves_recently_accessed_artifact(tmp_path):
    tmp_path.chmod(0o700)
    store = SingleFlightStore(tmp_path / "reuse.sqlite", operations={"parse": PIN}, max_entries=2)
    keys = [key(input_sha256=fingerprint(str(i))) for i in range(3)]
    for candidate in keys[:2]:
        claim = store.claim(candidate, "worker")
        store.complete(candidate, **credentials(claim), value="artifact")
    assert store.claim(keys[0], "reader")["status"] == "HIT"
    assert store.claim(keys[2], "worker")["status"] == "CLAIMED"
    assert store.claim(keys[0], "reader")["status"] == "HIT"
    assert store.claim(keys[1], "worker")["status"] == "CLAIMED"


def test_artifact_corruption_is_sticky_hold(setup_store):
    store, _ = setup_store
    claim = store.claim(key(), "worker")
    store.complete(key(), **credentials(claim), value={"answer": 1})
    with sqlite3.connect(store.db.path) as db:
        db.execute("UPDATE efficiency_reuse_entries SET value_json=?", (b'{"answer":2}',))
    assert store.claim(key(), "reader")["reason"] == "REUSE_ARTIFACT_INTEGRITY_MISMATCH"
    assert store.claim(key(), "reader")["status"] == "HELD"


def test_clock_rollback_and_config_drift_fail_closed(setup_store):
    store, clock = setup_store
    store.claim(key(), "worker")
    clock.now -= 1
    with pytest.raises(MachineError, match="reuse_clock_regressed_or_corrupt"):
        store.claim(key(), "reader")
    clock.now += 1
    with pytest.raises(MachineError, match="reuse_configuration_mismatch"):
        SingleFlightStore(store.db.path, operations={"parse": PIN}, clock=clock, max_entries=1)


def test_bad_lease_credentials_cannot_complete(setup_store):
    store, _ = setup_store
    claim = store.claim(key(), "worker")
    for updates in ({"owner_id": "other"}, {"lease_token": "f" * 64}, {"fence": claim["fence"] + 1}):
        creds = credentials(claim)
        creds.update(updates)
        assert store.complete(key(), **creds, value="intruder")["status"] == "STALE"


def node(label, *needs):
    return {"fingerprint": fingerprint(label), "needs": list(needs)}


def test_changed_branch_recomputes_only_descendants():
    previous = {"a": node("a"), "b": node("b", "a"), "c": node("c"),
                "d": node("d", "c"), "join": node("join", "b", "d")}
    current = copy.deepcopy(previous)
    current["a"] = node("a-v2")
    plan = plan_incremental(previous, current)
    assert plan["recompute"] == ["a", "b", "join"]
    assert plan["reuse_candidates"] == ["c", "d"]
    assert plan["execution_authorized"] is False
    assert previous["a"] == node("a")


def test_deleted_dependency_dirties_affected_branch():
    previous = {"a": node("a"), "b": node("b", "a"), "c": node("c", "b")}
    current = {"b": node("b"), "c": node("c", "b")}
    plan = plan_incremental(previous, current)
    assert plan["removed"] == ["a"] and plan["recompute"] == ["b", "c"]


def test_explicit_change_propagates_without_fingerprint_change():
    graph = {"a": node("a"), "b": node("b", "a"), "independent": node("x")}
    plan = plan_incremental(graph, graph, changed=["a"])
    assert plan["recompute"] == ["a", "b"]
    assert plan["reuse_candidates"] == ["independent"]


def test_unchanged_dependency_order_does_not_dirty_node():
    graph = {"a": node("a"), "b": node("b"), "c": node("c", "a", "b")}
    updated = copy.deepcopy(graph)
    updated["c"]["needs"].reverse()
    assert plan_incremental(graph, updated)["recompute"] == []


@pytest.mark.parametrize("graph,error", [
    ({"a": node("a", "b"), "b": node("b", "a")}, "incremental_cycle"),
    ({"a": node("a", "missing")}, "incremental_missing_dependency"),
    ({"a": node("a", "a")}, "incremental_cycle"),
    ({"a": node("a"), "b": node("b", "a", "a")}, "incremental_dependencies_invalid")])
def test_invalid_dependency_graph_is_rejected(graph, error):
    with pytest.raises(MachineError, match=error):
        plan_incremental({}, graph)


def test_graph_bounds_and_unknown_change_are_rejected():
    graph = {"a": node("a"), "b": node("b", "a")}
    with pytest.raises(MachineError, match="incremental_nodes_invalid"):
        plan_incremental({}, graph, max_nodes=1)
    with pytest.raises(MachineError, match="incremental_edges_exceeded"):
        plan_incremental({}, graph, max_edges=0)
    with pytest.raises(MachineError, match="incremental_changed_node_unknown"):
        plan_incremental(graph, graph, changed=["unknown"])


def test_empty_graph_and_long_chain_are_bounded_nonrecursive():
    assert plan_incremental({}, {})["order"] == []
    graph = {f"n{i}": node(str(i), *([f"n{i-1}"] if i else [])) for i in range(1500)}
    plan = plan_incremental(graph, graph, changed=["n0"])
    assert len(plan["recompute"]) == 1500 and plan["reuse_candidates"] == []
