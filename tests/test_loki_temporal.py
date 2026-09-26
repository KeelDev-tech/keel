"""Actual private SQLite persistence and temporal dependency regressions."""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from keel_loki.temporal import MemoryError, TemporalMemory, demo


def claim(revision="rev-1", value="2020-01", **changes):
    body = {"revision_id": revision, "subject_id": "person", "key": "employment-start", "value": value,
            "source": {"source_id": "employment-record", "sha256": "a" * 64,
                       "classification": "personal", "allowed_scopes": ["role-a"],
                       "permitted_uses": ["application_fact", "planning"]},
            "scope": "role-a", "permitted_uses": ["application_fact", "planning"],
            "valid_from": 10, "valid_until": None, "supersedes": []}
    body.update(changes)
    return body


def observation(identity="obs-1", revision="rev-1", verdict="verified", reviewer="reviewer", observed=100):
    return {"observation_id": identity, "revision_id": revision, "reviewer_id": reviewer,
            "verdict": verdict, "source_sha256": "a" * 64, "observed_at": observed}


def artifact(identity="packet-1", revisions=None):
    return {"artifact_id": identity, "subject_id": "person", "scope": "role-a", "purpose": "application_fact",
            "sha256": "b" * 64, "dependencies": revisions or ["rev-1"]}


def approval(identity="approval-1", **changes):
    value = {"approval_id": identity, "artifact_id": "packet-1", "artifact_sha256": "b" * 64,
             "reviewer_id": "reviewer", "source_record_sha256": "c" * 64,
             "observed_at": 100, "decision": "approved", "scope": "role-a"}
    value.update(changes)
    return value


@pytest.fixture
def memory(tmp_path):
    return TemporalMemory(tmp_path / "memory", clock=lambda: 100)


def resolve(memory, **changes):
    args = {"subject_id": "person", "key": "employment-start", "scope": "role-a",
            "purpose": "application_fact", "valid_at": 50}
    args.update(changes)
    return memory.resolve(**args)


def prepared(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_observation(observation(), "review-1")
    memory.register_artifact(artifact(), "artifact-1")
    memory.register_approval(approval(), "approval-1")


def test_private_persistence_reopen_and_exact_idempotency(memory):
    first = memory.record_claim(claim(), "claim-1")
    before = memory.db_path.read_bytes()
    reopened = TemporalMemory(memory.home, clock=lambda: 999)
    assert reopened.record_claim(claim(), "claim-1") == {**first, "created": False}
    assert reopened.db_path.read_bytes() == before
    assert reopened.history() == memory.history()
    assert os.stat(memory.home).st_mode & 0o777 == 0o700
    assert os.stat(memory.db_path).st_mode & 0o777 == 0o600


def test_idempotency_key_conflict_and_duplicate_identity_rejected(memory):
    memory.record_claim(claim(), "claim-1")
    with pytest.raises(MemoryError, match="idempotency_conflict"):
        memory.record_claim(claim(value="other"), "claim-1")
    with pytest.raises(MemoryError, match="duplicate_record_id"):
        memory.record_claim(claim(), "another-key")


def test_observation_is_required_and_does_not_authenticate_source(memory):
    memory.record_claim(claim(), "claim-1")
    assert resolve(memory)["status"] == "UNVERIFIED"
    assert resolve(memory)["value"] is None
    memory.record_observation(observation(), "review-1")
    result = resolve(memory)
    assert result["value"] == "2020-01"
    assert result["usable_observation"]
    assert not result["execution_authorized"]
    assert not result["source_truth_verified"]
    assert not result["reviewer_identity_authenticated"]


def test_late_correction_preserves_as_known_history_and_invalidates_approval(memory):
    prepared(memory)
    before = memory.db_path.read_bytes()
    assert memory.invalidation_projection(50)["stale_artifact_count"] == 0
    assert memory.db_path.read_bytes() == before
    memory.clock = lambda: 200
    memory.record_claim(claim("rev-2", "2020-02", supersedes=["rev-1"]), "claim-2")
    assert resolve(memory, known_at=100)["value"] == "2020-01"
    assert resolve(memory)["status"] == "UNVERIFIED"
    memory.record_observation(observation("obs-2", "rev-2", observed=200), "review-2")
    assert resolve(memory)["value"] == "2020-02"
    projection = memory.invalidation_projection(50)
    assert projection["stale_artifact_count"] == 1
    assert projection["artifacts"][0]["reasons"][0]["current_revision_ids"] == ["rev-2"]
    assert projection["approval_observations"][0]["status"] == "REAPPROVAL_REQUIRED"
    assert projection["canonical_writes"] == 0
    assert memory.invalidation_projection(50, known_at=100)["stale_artifact_count"] == 0


def test_valid_time_is_half_open_and_distinct_from_recorded_time(memory):
    memory.record_claim(claim(valid_from=20, valid_until=60), "claim-1")
    memory.record_observation(observation(), "review-1")
    assert resolve(memory, valid_at=19)["status"] == "NOT_FOUND"
    assert resolve(memory, valid_at=20)["status"] == "VERIFIED_OBSERVATION"
    assert resolve(memory, valid_at=59)["status"] == "VERIFIED_OBSERVATION"
    assert resolve(memory, valid_at=60)["status"] == "NOT_FOUND"
    assert resolve(memory, known_at=99)["status"] == "NOT_FOUND"


def test_disagreeing_revisions_preserve_conflict_until_explicit_correction(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_claim(claim("rev-2", "different"), "claim-2")
    memory.record_observation(observation(), "review-1")
    assert resolve(memory)["status"] == "CONFLICT"
    assert resolve(memory)["value"] is None
    memory.record_claim(claim("rev-3", "resolved", supersedes=["rev-1", "rev-2"]), "claim-3")
    memory.record_observation(observation("obs-3", "rev-3"), "review-3")
    assert resolve(memory)["value"] == "resolved"


def test_equal_value_independent_revisions_are_ambiguous_not_silently_merged(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_claim(claim("rev-2"), "claim-2")
    assert resolve(memory)["status"] == "AMBIGUOUS_REVISIONS"


def test_transitive_supersession_does_not_resurrect_ancestor(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_claim(claim("rev-2", valid_from=60, supersedes=["rev-1"]), "claim-2")
    memory.record_claim(claim("rev-3", "final", supersedes=["rev-2"]), "claim-3")
    memory.record_observation(observation("obs-3", "rev-3"), "review-3")
    assert resolve(memory, valid_at=50)["value"] == "final"


def test_restrictive_correction_never_reveals_old_fact_through_old_purpose(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_observation(observation(), "review-1")
    memory.record_claim(claim("rev-2", "private", permitted_uses=["planning"], supersedes=["rev-1"]), "claim-2")
    result = resolve(memory)
    assert result["status"] == "NOT_FOUND"
    assert result["revision_ids"] == []
    assert result["value"] is None


def test_restricted_parallel_claim_does_not_silently_hide_conflict(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_observation(observation(), "review-1")
    memory.record_claim(claim("rev-2", "private", permitted_uses=["planning"]), "claim-2")
    assert resolve(memory)["status"] == "RESTRICTED_CONFLICT"
    assert resolve(memory)["value"] is None


def test_cross_scope_read_and_source_restriction_do_not_leak_values(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_observation(observation(), "review-1")
    assert resolve(memory, scope="role-b")["revision_ids"] == []
    with pytest.raises(MemoryError, match="source_use_forbidden"):
        memory.record_claim(claim("rev-2", scope="role-b"), "claim-2")
    value = claim("rev-2", permitted_uses=["review"])
    with pytest.raises(MemoryError, match="source_use_forbidden"):
        memory.record_claim(value, "claim-2")


def test_cross_scope_supersession_and_artifact_dependency_rejected(memory):
    memory.record_claim(claim(), "claim-1")
    value = claim("rev-2", scope="role-b", supersedes=["rev-1"])
    value["source"]["allowed_scopes"].append("role-b")
    with pytest.raises(MemoryError, match="cross_scope_supersession"):
        memory.record_claim(value, "claim-2")
    value = artifact()
    value["scope"] = "role-b"
    with pytest.raises(MemoryError, match="cross_scope_dependency"):
        memory.register_artifact(value, "artifact-1")


def test_conflicting_human_observations_remain_disputed(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_observation(observation(), "review-1")
    memory.record_observation(observation("obs-2", verdict="rejected", reviewer="other"), "review-2")
    assert resolve(memory)["status"] == "DISPUTED"
    assert resolve(memory)["value"] is None


def test_late_old_review_does_not_override_newer_review(memory):
    memory.record_claim(claim(), "claim-1")
    memory.record_observation(observation(verdict="rejected", observed=90), "review-1")
    memory.record_observation(observation("obs-2", observed=80), "review-2")
    assert resolve(memory)["status"] == "REJECTED"


def test_revoked_observation_supersedes_previous_approval_projection(memory):
    prepared(memory)
    memory.register_approval(approval("approval-2", decision="rejected"), "approval-2")
    records = memory.invalidation_projection(50)["approval_observations"]
    assert [item["status"] for item in records] == ["SUPERSEDED_OBSERVATION", "REJECTED_OBSERVATION"]
    assert all(not item["execution_authorized"] for item in records)


def test_approval_artifact_hash_and_scope_are_bound(memory):
    prepared(memory)
    for changes in ({"artifact_sha256": "d" * 64}, {"scope": "role-b"}):
        with pytest.raises(MemoryError, match="approval_binding_mismatch"):
            memory.register_approval(approval("approval-2", **changes), "approval-2")


def test_source_hash_and_future_observation_are_rejected(memory):
    memory.record_claim(claim(), "claim-1")
    value = observation()
    value["source_sha256"] = "b" * 64
    with pytest.raises(MemoryError, match="source_hash_mismatch"):
        memory.record_observation(value, "obs-1")
    with pytest.raises(MemoryError, match="future_observation"):
        memory.record_observation(observation(observed=101), "obs-1")


def test_clock_rollback_rejects_new_write_but_keeps_idempotent_retry(memory):
    memory.record_claim(claim(), "claim-1")
    memory.clock = lambda: 99
    assert not memory.record_claim(claim(), "claim-1")["created"]
    with pytest.raises(MemoryError, match="clock_regression"):
        memory.record_claim(claim("rev-2"), "claim-2")
    assert memory.verify()["event_count"] == 1


@pytest.mark.parametrize("changes", [
    {"valid_from": True}, {"valid_from": -1}, {"valid_until": 10},
    {"permitted_uses": ["approval"]}, {"permitted_uses": ["unaided"]},
    {"supersedes": ["rev-1"]}, {"supersedes": ["absent"]},
    {"value": float("nan")}, {"revision_id": "../bad"}, {"execution_authorized": True},
])
def test_invalid_records_fail_closed_without_appending(memory, changes):
    with pytest.raises(MemoryError):
        memory.record_claim(claim(**changes), "claim-1")
    assert memory.verify()["event_count"] == 0


def test_sql_update_is_blocked_and_bypassed_mutation_is_detected(memory):
    memory.record_claim(claim(), "claim-1")
    connection = sqlite3.connect(memory.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="immutable_event"):
        connection.execute("UPDATE events SET payload='{}'")
    connection.rollback()
    connection.execute("DROP TRIGGER events_no_update")
    payload = json.dumps(claim(value="changed"), sort_keys=True, separators=(",", ":"))
    connection.execute("UPDATE events SET payload=?", (payload,))
    connection.commit()
    connection.close()
    with pytest.raises(MemoryError, match="history_hash_mismatch"):
        memory.verify()


def test_tail_deletion_detected_by_head_and_external_checkpoint(memory):
    memory.record_claim(claim(), "claim-1")
    checkpoint = memory.verify()["head_sha256"]
    connection = sqlite3.connect(memory.db_path)
    connection.execute("DROP TRIGGER events_no_delete")
    connection.execute("DELETE FROM events")
    connection.commit()
    with pytest.raises(MemoryError, match="head_mismatch"):
        memory.verify()
    # An owner can coordinate a rewrite; a separately retained checkpoint helps
    # detect this specific truncation. The store does not claim tamper proofing.
    connection.execute("UPDATE head SET event_count=0, sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()
    assert memory.verify()["status"] == "PASS"
    with pytest.raises(MemoryError, match="trusted_checkpoint_mismatch"):
        memory.verify(expected_head_sha256=checkpoint)


def test_permissions_symlinks_and_replaced_storage_fail_closed(tmp_path, memory):
    os.chmod(memory.db_path, 0o644)
    with pytest.raises(MemoryError, match="storage_not_private"):
        memory.verify()
    os.chmod(memory.db_path, 0o600)
    original = memory.db_path.read_bytes()
    memory.db_path.rename(memory.home / "old.sqlite3")
    memory.db_path.write_bytes(original)
    os.chmod(memory.db_path, 0o600)
    with pytest.raises(MemoryError, match="storage_replaced"):
        memory.verify()
    link = tmp_path / "link"
    link.symlink_to(memory.home, target_is_directory=True)
    with pytest.raises(MemoryError, match="symlink_forbidden"):
        TemporalMemory(link)


def test_parent_symlink_is_rejected_before_creating_any_directory(tmp_path):
    real = tmp_path / "actual"
    real.mkdir()
    link = tmp_path / "parent-link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(MemoryError, match="symlink_forbidden"):
        TemporalMemory(link / "must-not-exist")
    assert not (real / "must-not-exist").exists()


def test_sidecar_symlink_cannot_redirect_database_writes(tmp_path, memory):
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    memory.db_path.with_name(memory.db_path.name + "-journal").symlink_to(outside)
    with pytest.raises(MemoryError, match="symlink_forbidden"):
        memory.record_claim(claim(), "claim-1")
    assert outside.read_text() == "unchanged"


@pytest.mark.parametrize("value", [{1: "coerced-key"}, ("coerced-tuple",), {"bytes": b"bad"}])
def test_nonjson_values_are_rejected_without_coercion(memory, value):
    with pytest.raises(MemoryError):
        memory.record_claim(claim(value=value), "claim-1")


def test_multiple_store_writers_serialize_complete_event_chain(memory):
    def write(index):
        store = TemporalMemory(memory.home, clock=lambda: 100)
        return store.record_claim(claim(f"rev-{index}", key=f"key-{index}"), f"claim-{index}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(8)))
    assert all(item["created"] for item in results)
    assert memory.verify()["event_count"] == 8


def test_read_only_queries_preserve_bytes_and_return_clones(memory):
    prepared(memory)
    before = memory.db_path.read_bytes()
    result = memory.history()
    result[0]["payload"]["value"] = "changed"
    resolve(memory)
    memory.invalidation_projection(50)
    memory.verify()
    assert memory.db_path.read_bytes() == before
    assert memory.history()[0]["payload"]["value"] == "2020-01"


def test_demo_and_close_are_explicit(tmp_path):
    result = demo(tmp_path / "demo")
    assert result["historical_view_preserved"]
    assert result["corrected_revision_requires_verification"]
    assert result["projection"]["stale_artifact_count"] == 1
    assert result["actual_human_decisions"] == 0
    memory = TemporalMemory(tmp_path / "closed")
    memory.close()
    with pytest.raises(MemoryError, match="store_closed"):
        memory.verify()
