"""Matched SQLite work benchmark; no network, model calls, or credit estimates.

Run from the source root with ``PYTHONPATH=. python tests/benchmark_optimization_efficiency.py``.
The baseline room allocator is frozen from Keel 0.5.0. Both variants use the
same canonical keys/artifacts, capacity, and demanded reservation. SQLite VM
instructions are deterministic within a SQLite build, not elapsed time claims.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from keel_efficiency.reuse import SingleFlightStore, build_reuse_key
from keel_machine.common import canonical


PIN = hashlib.sha256(b"benchmark-pure-parse").hexdigest()
NOW = 1000


def key(number):
    return build_reuse_key(account_id="fixture", scope="workspace", purpose="test",
                           operation="parse", implementation_sha256=PIN,
                           model_sha256=PIN, source_sha256=PIN, policy_sha256=PIN,
                           input_sha256=hashlib.sha256(str(number).encode()).hexdigest())


def legacy_room(store, db, now, *, extra_entries, extra_bytes, except_digest=None):
    """Frozen pre-optimization allocator; retain as a matched reference only."""
    pinned = db.execute("""SELECT COUNT(*),COALESCE(SUM(length(key_json)+COALESCE(length(value_json),0)),0)
        FROM efficiency_reuse_entries WHERE state='HELD' OR key_sha256=?
        OR (state='RUNNING' AND lease_until>?)""", (except_digest or "", now)).fetchone()
    if pinned[0] + extra_entries > store.max_entries or pinned[1] + extra_bytes > store.max_bytes:
        return False
    usage = store._usage(db)
    while usage["entries"] + extra_entries > store.max_entries or usage["bytes"] + extra_bytes > store.max_bytes:
        row = db.execute("""SELECT key_sha256 FROM efficiency_reuse_entries
            WHERE (state='READY' OR (state='RUNNING' AND lease_until<=?))
            AND key_sha256!=? ORDER BY accessed_sequence,key_sha256 LIMIT 1""",
                         (now, except_digest or "")).fetchone()
        if row is None:
            return False
        db.execute("DELETE FROM efficiency_reuse_entries WHERE key_sha256=?", (row[0],))
        usage = store._usage(db)
    return True


def make_fixture(directory, label, *, entries=256, legacy=False):
    directory = Path(directory) / label
    directory.mkdir(mode=0o700)
    leader = key(entries)
    leader_raw = canonical(leader)
    store = SingleFlightStore(directory / "reuse.sqlite", operations={"parse": PIN},
                              clock=lambda: NOW, max_entries=entries + 1,
                              max_bytes=entries * 1024 + len(leader_raw))
    with store.db.transaction() as db:
        for number in range(entries):
            raw = canonical(key(number))
            value = canonical("x" * (1024 - len(raw) - 2))
            db.execute("INSERT INTO efficiency_reuse_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (hashlib.sha256(raw).hexdigest(), raw, "READY", None, None,
                        number + 1, NOW, None, None, NOW + 3600, value,
                        hashlib.sha256(value).hexdigest(), number + 1, None))
        db.execute("INSERT INTO efficiency_reuse_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (hashlib.sha256(leader_raw).hexdigest(), leader_raw, "RUNNING", "worker",
                    "a" * 64, entries + 1, NOW, NOW + 60, NOW + 3600,
                    None, None, None, entries + 1, None))
        db.execute("UPDATE efficiency_reuse_meta SET sequence=?", (entries + 1,))
        if legacy:
            db.execute("DROP INDEX efficiency_reuse_ready_expiry")
            db.execute("DROP INDEX efficiency_reuse_run_expiry")
    return store, hashlib.sha256(leader_raw).hexdigest()


def measured(db, operation):
    counts = {"sqlite_vm_instructions": 0, "usage_aggregate_queries": 0}

    def instruction():
        counts["sqlite_vm_instructions"] += 1
        return 0

    def statement(sql):
        if "SELECT COUNT(*)" in sql and "SUM(length(key_json)" in sql:
            counts["usage_aggregate_queries"] += 1

    db.set_progress_handler(instruction, 1)
    db.set_trace_callback(statement)
    try:
        result = operation()
    finally:
        db.set_progress_handler(None, 0)
        db.set_trace_callback(None)
    return result, counts


def run_benchmark(directory, *, entries=256, evict=192):
    assert 1 <= evict <= entries
    results = {}
    snapshots = {}
    for name in ("baseline", "optimized"):
        store, leader = make_fixture(directory, name, entries=entries, legacy=name == "baseline")
        with store.db.transaction() as db:
            def prune():
                db.execute("DELETE FROM efficiency_reuse_entries WHERE state='READY' AND expires_at<=?", (NOW,))
                db.execute("DELETE FROM efficiency_reuse_entries WHERE state='RUNNING' AND run_until<=?", (NOW,))

            _, expiry = measured(db, prune)
            allocate = (lambda: legacy_room(store, db, NOW, extra_entries=0,
                                           extra_bytes=evict * 1024, except_digest=leader)) if name == "baseline" else (
                lambda: store._room(db, NOW, extra_entries=0,
                                    extra_bytes=evict * 1024, except_digest=leader))
            accepted, allocation = measured(db, allocate)
            rows = [tuple(row) for row in db.execute(
                "SELECT key_sha256,key_json,state,value_json FROM efficiency_reuse_entries ORDER BY key_sha256")]
            snapshots[name] = rows
            results[name] = {"accepted": accepted, "evicted": entries + 1 - len(rows),
                             "remaining_usage": store._usage(db),
                             "expiry_prune": expiry, "room_allocation": allocation}
    equal = snapshots["baseline"] == snapshots["optimized"]
    assert equal and all(result["accepted"] and result["evicted"] == evict for result in results.values())
    return {"schema": "keel.optimization.singleflight-benchmark.v1",
            "sqlite_version": sqlite3.sqlite_version,
            "fixture": {"ready_entries": entries, "live_leaders": 1, "bytes_per_ready_entry": 1024,
                        "extra_bytes_requested": evict * 1024},
            "results": results, "identical_remaining_rows": equal,
            "network_requests": 0, "model_calls": 0, "measured_credit_savings": None,
            "limitations": "Synthetic cache workload; VM instruction counts depend on SQLite version. No production latency or credit savings claim."}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="keel-reuse-benchmark-") as temporary:
        print(json.dumps(run_benchmark(temporary), indent=2, sort_keys=True))
