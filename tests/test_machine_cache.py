"""Persistent cache isolation, deterministic results, corruption, and bounds."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from keel_machine.cache import ComputationCache
from keel_machine.common import MachineError, canonical, digest


def key(**changes):
    return {"schema": "keel.machine.cache-key.v1", "account_id": "account-A", "scope": "workspace-A",
            "purpose": "analysis", "operation": "role-score", "implementation_sha256": "1" * 64,
            "config_sha256": "2" * 64, "inputs_sha256": "3" * 64,
            "dependencies": {"profile": "4" * 64}, "contract_sha256": "5" * 64, **changes}


@pytest.fixture
def cache(tmp_path):
    tmp_path.chmod(0o700)
    clock = [1000.0]
    instance = ComputationCache(tmp_path / "cache.sqlite", clock=lambda: clock[0])
    instance.test_clock = clock
    return instance


def test_miss_put_hit_clone_and_reopen(cache):
    value = {"score": 85, "reasons": ["explicit evidence"], "execution_authorized": False}
    assert cache.get(key())["status"] == "MISS"
    result = cache.put(key(), value)
    assert result["status"] == "STORED" and result["output_sha256"] == digest(value)
    value["score"] = 0
    hit = cache.get(key())
    assert hit["status"] == "HIT" and hit["value"]["score"] == 85
    hit["value"]["reasons"].append("caller changed copy")
    reopened = ComputationCache(cache.db.path, clock=cache.clock)
    assert reopened.get(key())["value"]["reasons"] == ["explicit evidence"]
    assert reopened.stats()["ready_entries"] == 1
    assert not reopened.stats()["execution_authorized"]


@pytest.mark.parametrize("field,value", [
    ("account_id", "account-B"), ("scope", "workspace-B"), ("purpose", "preparation"),
    ("operation", "other-pure-operation"), ("implementation_sha256", "a" * 64),
    ("config_sha256", "b" * 64), ("inputs_sha256", "c" * 64),
    ("dependencies", {"profile": "d" * 64}), ("contract_sha256", "e" * 64),
    ("dependencies", {"other-dependency": "4" * 64})])
def test_exact_cache_key_separates_scope_and_every_revision(cache, field, value):
    cache.put(key(), {"result": "original"})
    assert cache.get(key(**{field: value}))["status"] == "MISS"


def test_dictionary_order_has_no_effect_and_actual_input_value_is_not_inferred(cache):
    original = key(dependencies={"b": "a" * 64, "a": "b" * 64})
    reordered = dict(reversed(list(original.items())))
    reordered["dependencies"] = dict(reversed(list(original["dependencies"].items())))
    cache.put(original, [1, 2, 3])
    assert cache.get(reordered)["status"] == "HIT"
    assert cache.get(reordered)["key_sha256"] == digest(original)


@pytest.mark.parametrize("purpose", ["approval", "authorization", "execution", "submission", "payment", "", None, []])
def test_authority_or_effect_purposes_are_not_cached(cache, purpose):
    with pytest.raises(MachineError):
        cache.put(key(purpose=purpose), True)
    assert cache.stats()["entries"] == 0


@pytest.mark.parametrize("change", [
    {"extra": True}, {"schema": "wrong"}, {"account_id": "../account"}, {"scope": ""},
    {"inputs_sha256": "ABC"}, {"dependencies": {"bad/id": "a" * 64}},
    {"dependencies": {"profile": "not-a-digest"}}, {"dependencies": []},
    {"dependencies": {"d" + str(i): "a" * 64 for i in range(129)}}])
def test_malformed_keys_fail_without_mutation(cache, change):
    with pytest.raises(MachineError):
        cache.put(key(**change), {"ok": True})
    assert cache.stats()["entries"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"number": 2**64}, {"not_json": {1, 2}}, "x" * 262145])
def test_values_must_be_bounded_finite_json(cache, value):
    with pytest.raises(MachineError):
        cache.put(key(), value)
    assert cache.get(key())["status"] == "MISS"


def test_cycles_and_deep_values_fail_without_encoding_recursion(cache):
    cycle = []
    cycle.append(cycle)
    with pytest.raises(MachineError):
        cache.put(key(), cycle)
    deep = []
    for _ in range(34):
        deep = [deep]
    with pytest.raises(MachineError):
        cache.put(key(), deep)


@pytest.mark.parametrize("ttl", [0, -1, True, "60", float("nan"), float("inf"), 2592001, 10**1000])
def test_invalid_ttl(cache, ttl):
    with pytest.raises(MachineError):
        cache.put(key(), "value", ttl_seconds=ttl)
    assert cache.get(key())["status"] == "MISS"


def test_ttl_expires_at_boundary_and_purges_capacity(cache):
    cache.put(key(), {"ok": True}, ttl_seconds=10)
    cache.test_clock[0] = 1009.99
    assert cache.get(key())["status"] == "HIT"
    cache.test_clock[0] = 1010.0
    assert cache.get(key())["status"] == "MISS"
    assert cache.stats()["entries"] == cache.stats()["bytes"] == 0
    # Expired values are intentionally not a permanent historical ledger.
    assert cache.put(key(), {"ok": False})["status"] == "STORED"


def test_identical_result_refreshes_ttl_and_keeps_one_entry(cache):
    cache.put(key(), None, ttl_seconds=10)
    cache.test_clock[0] = 1005
    assert cache.put(key(), None, ttl_seconds=20)["status"] == "REFRESHED"
    cache.test_clock[0] = 1011
    assert cache.get(key())["status"] == "HIT"
    assert cache.get(key())["value"] is None
    assert cache.stats()["entries"] == 1


def test_different_output_for_same_key_is_sticky_nondeterminism_hold(cache):
    cache.put(key(), {"score": 80})
    original_digest = digest({"score": 80})
    result = cache.put(key(), {"score": 81})
    assert result["status"] == "HELD" and result["hold_reasons"] == ["NONDETERMINISTIC_OUTPUT"]
    assert result["output_sha256"] == original_digest and "value" not in result
    cache.test_clock[0] += 99999
    reopened = ComputationCache(cache.db.path, clock=cache.clock)
    assert reopened.get(key())["status"] == "HELD"
    assert reopened.put(key(), {"score": 80})["status"] == "HELD"
    assert reopened.stats()["held_entries"] == 1


def test_lru_is_deterministic_when_clock_values_are_equal(tmp_path):
    tmp_path.chmod(0o700)
    cache = ComputationCache(tmp_path / "lru.sqlite", clock=lambda: 1000, max_entries=2)
    ka, kb, kc = (key(operation=name) for name in ("a", "b", "c"))
    cache.put(ka, "a"); cache.put(kb, "b")
    cache.get(ka)
    assert cache.put(kc, "c")["evicted_entries"] == 1
    assert cache.get(ka)["status"] == "HIT"
    assert cache.get(kb)["status"] == "MISS"
    assert cache.get(kc)["status"] == "HIT"
    assert cache.stats()["entries"] == 2


def test_byte_budget_eviction_and_single_oversize_value(tmp_path):
    tmp_path.chmod(0o700)
    budget = len(canonical(key())) + len(canonical("first")) + 10
    cache = ComputationCache(tmp_path / "bytes.sqlite", clock=lambda: 1000, max_bytes=budget)
    cache.put(key(), "first")
    other = key(operation="other")
    assert cache.put(other, "second")["evicted_entries"] == 1
    assert cache.stats()["bytes"] <= budget
    with pytest.raises(MachineError, match="byte_budget"):
        cache.put(key(), "x" * budget)
    assert cache.get(other)["status"] == "HIT"


def test_held_entries_are_bounded_and_not_lru_evicted(tmp_path):
    tmp_path.chmod(0o700)
    cache = ComputationCache(tmp_path / "held.sqlite", clock=lambda: 1000, max_entries=1)
    cache.put(key(), 1)
    cache.put(key(), 2)
    result = cache.put(key(operation="other"), 3)
    assert result["status"] == "HELD" and result["hold_reasons"] == ["CAPACITY_HELD"]
    assert cache.get(key())["status"] == "HELD"
    assert cache.get(key(operation="other"))["status"] == "MISS"
    assert cache.stats()["entries"] == 1


@pytest.mark.parametrize("field,value,reason", [
    ("value_json", b'{"score":999}', "CACHE_OUTPUT_INTEGRITY_MISMATCH"),
    ("key_json", b'{}', "CACHE_KEY_INTEGRITY_MISMATCH"),
    ("output_sha256", "0" * 64, "CACHE_OUTPUT_INTEGRITY_MISMATCH"),
    ("created_at", 2000, "CACHE_ENTRY_METADATA_CORRUPT"),
    ("accessed_at", -1, "CACHE_ENTRY_METADATA_CORRUPT"),
    ("access_sequence", 0, "CACHE_ENTRY_METADATA_CORRUPT"),
    ("expires_at", "not-a-time", "CACHE_ENTRY_METADATA_CORRUPT")])
def test_corruption_never_returns_a_hit(cache, field, value, reason):
    cache.put(key(), {"score": 85})
    with sqlite3.connect(cache.db.path) as db:
        # Whitelisted test mutation column, never input-controlled SQL in API.
        db.execute(f"UPDATE computation_cache_entries SET {field}=?", (value,))
    result = cache.get(key())
    assert result["status"] == "HELD" and result["hold_reasons"] == [reason]
    assert "value" not in result
    assert cache.put(key(), {"score": 85})["status"] == "HELD"


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'NaN', b' {"a":1}', b'"\xff"'])
def test_invalid_or_noncanonical_cached_json_holds_even_when_digest_matches(cache, raw):
    cache.put(key(), {"a": 1})
    with sqlite3.connect(cache.db.path) as db:
        db.execute("UPDATE computation_cache_entries SET value_json=?,output_sha256=?", (raw, hashlib.sha256(raw).hexdigest()))
    result = cache.get(key())
    assert result["status"] == "HELD" and result["hold_reasons"] == ["CACHE_OUTPUT_ENCODING_INVALID"]


def test_oversized_corrupt_value_is_held_before_loading_payload(cache):
    cache.put(key(), "small")
    with sqlite3.connect(cache.db.path) as db:
        db.execute("UPDATE computation_cache_entries SET value_json=zeroblob(1000000)")
    result = cache.get(key())
    assert result["status"] == "HELD"
    assert cache.stats()["bytes"] == len(canonical(key()))


def test_clock_regression_rejected_across_process_reopen(cache):
    cache.put(key(), "value")
    cache.test_clock[0] = 1001
    assert cache.get(key())["status"] == "HIT"
    cache.test_clock[0] = 1000
    with pytest.raises(MachineError, match="clock_regressed"):
        cache.get(key())
    with pytest.raises(MachineError, match="clock_regressed"):
        ComputationCache(cache.db.path, clock=cache.clock)


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "1000", 10**1000])
def test_malformed_clock_fails_closed(cache, bad):
    cache.clock = lambda: bad
    with pytest.raises(MachineError, match="clock_invalid"):
        cache.stats()


def test_clock_regression_before_commit_rolls_back_value(cache):
    values = iter([1001, 1000])
    cache.clock = lambda: next(values)
    with pytest.raises(MachineError, match="clock_regressed"):
        cache.put(key(), "should roll back")
    cache.clock = lambda: 1001
    assert cache.get(key())["status"] == "MISS"


def test_reopen_config_is_explicit_and_namespace_is_pinned(cache):
    with pytest.raises(MachineError, match="configuration_mismatch"):
        ComputationCache(cache.db.path, clock=cache.clock, max_entries=2)
    with sqlite3.connect(cache.db.path) as db:
        db.execute("UPDATE computation_cache_meta SET schema='unknown-schema'")
    with pytest.raises(MachineError, match="configuration_mismatch"):
        cache.stats()


def test_concurrent_same_result_is_single_entry(cache):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.put(key(), {"score": 85}), range(24)))
    assert sum(r["status"] == "STORED" for r in results) == 1
    assert all(r["status"] in ("STORED", "REFRESHED") for r in results)
    assert cache.stats()["entries"] == 1


def test_concurrent_different_results_produce_hold(cache):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: cache.put(key(), value), ["first", "different"]))
    assert {r["status"] for r in results} == {"STORED", "HELD"}
    assert cache.get(key())["status"] == "HELD"


def test_replaced_private_database_is_not_accepted(cache):
    moved = cache.db.path.with_name("original.sqlite")
    cache.db.path.rename(moved)
    replacement = sqlite3.connect(cache.db.path)
    replacement.close()
    cache.db.path.chmod(0o600)
    with pytest.raises(MachineError, match="storage_replaced"):
        cache.stats()


def test_byte_budget_accounting_uses_actual_payload_lengths(cache):
    value = {"unicode": "日本語"}
    cache.put(key(), value)
    assert cache.stats()["bytes"] == len(canonical(key())) + len(canonical(value))


def test_capacity_hold_preserves_other_successful_entries(tmp_path):
    tmp_path.chmod(0o700)
    held_key = key(operation="held")
    ready_key = key(operation="ready")
    budget = len(canonical(held_key)) + len(canonical(ready_key)) + 20
    cache = ComputationCache(tmp_path / "capacity.sqlite", clock=lambda: 1000, max_entries=3, max_bytes=budget)
    cache.put(held_key, 1)
    cache.put(held_key, 2)
    cache.put(ready_key, "keep")
    # Fits on its own but cannot coexist with the sticky held identity.
    output = "x" * (budget - len(canonical(key(operation="large"))) - 5)
    result = cache.put(key(operation="large"), output)
    assert result["status"] == "HELD" and result["evicted_entries"] == 0
    assert cache.get(ready_key)["status"] == "HIT"


def test_malformed_held_metadata_stays_bounded_and_no_value_is_returned(cache):
    cache.put(key(), 1); cache.put(key(), 2)
    with sqlite3.connect(cache.db.path) as db:
        db.execute("UPDATE computation_cache_entries SET value_json=?,output_sha256=?", (b"unexpected", "x" * 1000))
    result = cache.get(key())
    assert result["status"] == "HELD" and result["output_sha256"] is None
    assert result["hold_reasons"] == ["CACHE_HOLD_METADATA_CORRUPT"]
    assert cache.stats()["bytes"] == len(canonical(key()))
