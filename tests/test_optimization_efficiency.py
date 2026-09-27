"""Exact capacity, migration, rollback and matched-work cache regressions."""
import hashlib
import random

import pytest

from benchmark_optimization_efficiency import NOW, PIN, key, legacy_room, make_fixture, run_benchmark
from keel_efficiency.reuse import SingleFlightStore
from keel_machine.common import canonical


def test_matched_fixture_reduces_sqlite_work_without_changing_victims(tmp_path):
    report = run_benchmark(tmp_path, entries=64, evict=48)
    baseline, optimized = (report["results"][name] for name in ("baseline", "optimized"))
    assert report["identical_remaining_rows"]
    assert baseline["room_allocation"]["usage_aggregate_queries"] == 50
    assert optimized["room_allocation"]["usage_aggregate_queries"] == 1
    for operation in ("expiry_prune", "room_allocation"):
        assert optimized[operation]["sqlite_vm_instructions"] < baseline[operation]["sqlite_vm_instructions"]


def test_existing_store_gets_indexes_without_replacing_artifacts(tmp_path):
    store, _ = make_fixture(tmp_path, "old", entries=4, legacy=True)
    before = store.claim(key(0), "reader")
    reopened = SingleFlightStore(store.db.path, operations={"parse": PIN}, clock=lambda: NOW,
                                  max_entries=store.max_entries, max_bytes=store.max_bytes)
    assert reopened.claim(key(0), "reader")["value"] == before["value"]
    with reopened.db.transaction() as db:
        for name, sql in (
            ("efficiency_reuse_ready_expiry", "DELETE FROM efficiency_reuse_entries WHERE state='READY' AND expires_at<=1000"),
            ("efficiency_reuse_run_expiry", "DELETE FROM efficiency_reuse_entries WHERE state='RUNNING' AND run_until<=1000"),
        ):
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql))
            assert name in plan and "SEARCH" in plan


def test_completion_evicts_once_and_accounts_exact_bytes(tmp_path):
    store, leader_digest = make_fixture(tmp_path, "public", entries=8)
    # Fixture leader is valid and owned; complete through the public API.
    report = store.complete(key(8), owner_id="worker", lease_token="a" * 64,
                            fence=9, value="x" * (5 * 1024 - 2))
    assert report["status"] == "STORED"
    assert store.claim(key(8), "reader")["value"] == "x" * (5 * 1024 - 2)
    stats = store.stats()
    assert stats["entries"] == 4 and stats["bytes"] == store.max_bytes
    assert stats["execution_authorized"] is False
    with store.db.transaction() as db:
        remaining = {row[0] for row in db.execute("SELECT key_sha256 FROM efficiency_reuse_entries")}
    assert leader_digest in remaining
    assert remaining == {hashlib.sha256(canonical(key(i))).hexdigest() for i in (5, 6, 7, 8)}


def test_impossible_request_preserves_all_rows_including_reusable_artifacts(tmp_path):
    store, leader = make_fixture(tmp_path, "blocked", entries=8)
    with store.db.transaction() as db:
        db.execute("UPDATE efficiency_reuse_entries SET state='HELD',expires_at=NULL WHERE accessed_sequence<=4")
        before = [tuple(row) for row in db.execute("SELECT * FROM efficiency_reuse_entries ORDER BY key_sha256")]
        assert not store._room(db, NOW, extra_entries=0, extra_bytes=5 * 1024, except_digest=leader)
        assert [tuple(row) for row in db.execute("SELECT * FROM efficiency_reuse_entries ORDER BY key_sha256")] == before


def test_transaction_rollback_restores_selected_victims(tmp_path):
    store, leader = make_fixture(tmp_path, "rollback", entries=8)
    before = store.stats()
    with pytest.raises(RuntimeError, match="abort"):
        with store.db.transaction() as db:
            assert store._room(db, NOW, extra_entries=0, extra_bytes=5 * 1024, except_digest=leader)
            raise RuntimeError("abort")
    assert store.stats() == before


@pytest.mark.parametrize("seed", range(20))
def test_reference_equivalence_for_mixed_pins_and_capacity(seed, tmp_path):
    rng = random.Random(seed)
    stores = [make_fixture(tmp_path, label, entries=32)[0] for label in ("reference", "candidate")]
    assignments = []
    for number in range(32):
        state = rng.choice(("READY", "READY", "HELD", "RUNNING"))
        # Include live leaders and expired recoverable pure work.
        lease = NOW + rng.choice((-10, 10)) if state == "RUNNING" else None
        assignments.append((state, lease, rng.randrange(1, 9), number + 1))
    entries_needed, bytes_needed = rng.randrange(0, 10), rng.randrange(0, 32 * 1024)
    results, snapshots = [], []
    for index, store in enumerate(stores):
        with store.db.transaction() as db:
            for state, lease, lru, fence in assignments:
                db.execute("UPDATE efficiency_reuse_entries SET state=?,lease_until=?,accessed_sequence=? WHERE fence=?",
                           (state, lease, lru, fence))
            leader = hashlib.sha256(canonical(key(32))).hexdigest()
            allocator = lambda: (legacy_room(store, db, NOW, extra_entries=entries_needed,
                                             extra_bytes=bytes_needed, except_digest=leader) if index == 0 else
                                  store._room(db, NOW, extra_entries=entries_needed,
                                              extra_bytes=bytes_needed, except_digest=leader))
            results.append(allocator())
            snapshots.append([tuple(row) for row in db.execute("SELECT * FROM efficiency_reuse_entries ORDER BY key_sha256")])
    assert results[0] == results[1]
    assert snapshots[0] == snapshots[1]
