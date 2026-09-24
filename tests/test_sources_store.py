"""Local source persistence tests; no network, model or application actions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3
from threading import Barrier

import pytest

from keel_agent.revisions import COMPONENTS, export_revisions
from keel_sources.store import SourceStore, SourceStoreError


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
SCOPE = {"workspace_id": "synthetic-workspace", "role_id": "synthetic-role",
         "application_id": "synthetic-application", "action": "submit"}


def stamp(offset=0):
    return (NOW + timedelta(seconds=offset)).isoformat()


def policy(*, version="v1", observed=-20, expires=600):
    return {"source_ref": "synthetic:policy", "source_version": version,
            "observed_at": stamp(observed), "expires_at": stamp(expires),
            "record": {"policy_id": "synthetic-policy", "rules": {
                "allowed_actions": ["submit"], "allowed_accounts": ["synthetic-account"],
                "allowed_destinations": ["https://apply.example.invalid/role"],
                "requires_human_approval": True, "holds": []}}}


@pytest.fixture
def store(tmp_path):
    value = SourceStore(tmp_path / "sources", SCOPE["workspace_id"], clock=lambda: NOW)
    value.register_scope(SCOPE)
    return value


def report(store, *, now=NOW):
    return export_revisions(store.export_snapshot(now=now), attachment_root=store.attachment_root, now=now)


def test_registered_scopes_have_known_missing_records_without_human_requests(store):
    snapshot = store.export_snapshot()
    sources = snapshot["roles"][0]["sources"]
    assert set(sources) == set(COMPONENTS)
    assert all(value == {"absence": {"kind": "SOURCE_RECORD_MISSING"}} for value in sources.values())
    result = report(store)
    assert result["system_root_cause_count"] == 7
    assert result["human_root_cause_count"] == 0
    assert result["ready_role_count"] == 0
    assert not result["execution_authorized"]


def test_scope_changes_advance_snapshot_version_and_reregister_is_idempotent(store):
    first = store.export_snapshot()["snapshot"]["source_version"]
    assert not store.register_scope(SCOPE)["created"]
    assert store.export_snapshot()["snapshot"]["source_version"] == first
    other = {**SCOPE, "role_id": "other-role"}
    store.register_scope(other)
    assert store.export_snapshot()["snapshot"]["source_version"] != first


def test_source_capture_is_durable_and_original_timestamps_are_not_refreshed(store):
    descriptor = policy()
    written = store.put_source(SCOPE, "policy", descriptor, expected_generation=0)
    assert written["generation"] == 1
    reopened = SourceStore(store.home, SCOPE["workspace_id"], clock=lambda: NOW + timedelta(seconds=30))
    snapshot = reopened.export_snapshot()
    assert snapshot["snapshot"]["observed_at"] == stamp(30)
    assert snapshot["roles"][0]["sources"]["policy"] == descriptor
    assert report(reopened, now=NOW + timedelta(seconds=601))["roles"][0]["components"]["policy"]["status"] == "STALE"


def test_export_and_events_are_byte_identical_read_only_operations(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    before = store.db_path.read_bytes()
    before_mtime = store.db_path.stat().st_mtime_ns
    identity_before = store.identity_path.read_bytes()
    store.export_snapshot(now=NOW + timedelta(seconds=300))
    store.events(now=NOW + timedelta(seconds=300))
    assert store.db_path.read_bytes() == before
    assert store.db_path.stat().st_mtime_ns == before_mtime
    assert store.identity_path.read_bytes() == identity_before
    assert not store.db_path.with_name(store.db_path.name + "-journal").exists()


def test_expired_record_can_be_preserved_but_never_becomes_ready(store):
    descriptor = policy(observed=-100, expires=-10)
    store.put_source(SCOPE, "policy", descriptor, expected_generation=0)
    assert store.export_snapshot()["roles"][0]["sources"]["policy"] == descriptor
    assert report(store)["roles"][0]["components"]["policy"]["reason"] == "source_expired"


def test_compare_and_swap_rejects_stale_writer_without_history_or_event_changes(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    before = store.db_path.read_bytes()
    with pytest.raises(SourceStoreError, match="generation_conflict"):
        store.put_source(SCOPE, "policy", policy(version="v2"), expected_generation=0)
    assert store.db_path.read_bytes() == before


def test_two_independent_writers_cannot_both_win_same_generation(store):
    other = SourceStore(store.home, SCOPE["workspace_id"], clock=lambda: NOW)
    barrier = Barrier(2)

    def write(which, version):
        barrier.wait(timeout=5)
        try:
            return which.put_source(SCOPE, "policy", policy(version=version), expected_generation=0)["generation"]
        except SourceStoreError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, store, "v1"), pool.submit(write, other, "v2")]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(outcomes, key=str) == [1, "generation_conflict"]
    assert len([event for event in store.events() if event["kind"] == "SOURCE_CAPTURED"]) == 1


def test_same_source_version_cannot_change_material_even_with_fresh_observation(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    changed = policy(observed=-10)
    changed["record"]["rules"]["holds"] = ["synthetic-dedupe-hold"]
    with pytest.raises(SourceStoreError, match="source_version_material_conflict"):
        store.put_source(SCOPE, "policy", changed, expected_generation=1)


def test_exact_replay_and_expiry_only_extension_are_rejected(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    with pytest.raises(SourceStoreError, match="source_replay"):
        store.put_source(SCOPE, "policy", policy(), expected_generation=1)
    with pytest.raises(SourceStoreError, match="source_reobservation_required"):
        store.put_source(SCOPE, "policy", policy(expires=1200), expected_generation=1)


def test_explicit_unchanged_reobservation_retains_material_revision(store):
    first = store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    renewed = store.put_source(SCOPE, "policy", policy(observed=-10, expires=900), expected_generation=1)
    assert renewed["generation"] == 2
    assert renewed["revision"] == first["revision"]


def test_superseded_version_and_observation_rollback_are_rejected(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    store.put_source(SCOPE, "policy", policy(version="v2", observed=-10), expected_generation=1)
    with pytest.raises(SourceStoreError, match="source_version_replay"):
        store.put_source(SCOPE, "policy", policy(version="v1", observed=-5), expected_generation=2)
    with pytest.raises(SourceStoreError, match="source_observation_rollback"):
        store.put_source(SCOPE, "policy", policy(version="v3", observed=-30), expected_generation=2)


def test_revocation_is_preserved_and_old_version_cannot_unrevoke(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    revoked = {**policy(), "revoked": True}
    store.put_source(SCOPE, "policy", revoked, expected_generation=1)
    assert report(store)["roles"][0]["components"]["policy"]["status"] == "REVOKED"
    with pytest.raises(SourceStoreError, match="source_revocation_rollback"):
        store.put_source(SCOPE, "policy", policy(observed=-10), expected_generation=2)


def test_clock_rollback_rejected_for_writes_and_reads(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    with pytest.raises(SourceStoreError, match="clock_rollback"):
        store.export_snapshot(now=NOW - timedelta(seconds=1))
    with pytest.raises(SourceStoreError, match="clock_rollback"):
        store.put_source(SCOPE, "policy", policy(version="v2"), expected_generation=1,
                         now=NOW - timedelta(seconds=1))


def test_future_source_observation_never_accepted(store):
    with pytest.raises(ValueError, match="source_observation_in_future"):
        store.put_source(SCOPE, "policy", policy(observed=1), expected_generation=0)


def test_failed_compound_transaction_rolls_back_sources_events_and_clock(store):
    before = store.db_path.read_bytes()
    with pytest.raises(RuntimeError, match="synthetic_abort"):
        with store.transaction(NOW + timedelta(seconds=10)) as connection:
            store.write_source(connection, SCOPE, "policy", policy(), expected_generation=0)
            raise RuntimeError("synthetic_abort")
    assert store.db_path.read_bytes() == before
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)


def test_approval_cannot_enter_public_put_api(store):
    with pytest.raises(SourceStoreError, match="approval_requires_decision_workflow"):
        store.put_source(SCOPE, "approval", policy(), expected_generation=0)


def test_known_missing_approval_only_becomes_human_request_by_explicit_workflow_write(store):
    pending = {"source_ref": "synthetic:request", "source_version": "request",
               "observed_at": stamp(), "expires_at": stamp(300),
               "absence": {"kind": "HUMAN_DECISION_REQUIRED", "decision_request_ref": "synthetic-request"}}
    with store.transaction() as connection:
        store.write_source(connection, SCOPE, "approval", pending, expected_generation=0, allow_approval=True)
    assert report(store)["human_root_cause_count"] == 1


def test_unregistered_and_cross_workspace_scopes_are_rejected(store):
    with pytest.raises(SourceStoreError, match="scope_not_registered"):
        store.put_source({**SCOPE, "role_id": "never-captured"}, "policy", policy(), expected_generation=0)
    with pytest.raises(SourceStoreError, match="workspace_mismatch"):
        store.register_scope({**SCOPE, "workspace_id": "other-workspace"})
    with pytest.raises(SourceStoreError, match="workspace_mismatch"):
        SourceStore(store.home, "other-workspace")


def test_private_files_and_symlinks_rejected(store, tmp_path):
    assert store.home.stat().st_mode & 0o777 == 0o700
    assert store.attachment_root.stat().st_mode & 0o777 == 0o700
    assert store.db_path.stat().st_mode & 0o777 == 0o600
    link = tmp_path / "alias"
    link.symlink_to(store.home, target_is_directory=True)
    with pytest.raises(SourceStoreError, match="directory_symlink"):
        SourceStore(link, SCOPE["workspace_id"])
    store.db_path.chmod(0o644)
    with pytest.raises(SourceStoreError, match="database_not_private"):
        store.export_snapshot()


@pytest.mark.parametrize("column", ["descriptor_json", "observed_at", "source_version", "revision"])
def test_database_record_or_metadata_corruption_fails_closed(store, column):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(f"UPDATE source_records SET {column}=?", ("tampered",))
    with pytest.raises(SourceStoreError, match="source_row_integrity_failed"):
        store.export_snapshot()


def test_database_head_rollback_cannot_select_older_valid_record(store):
    store.put_source(SCOPE, "policy", policy(), expected_generation=0)
    store.put_source(SCOPE, "policy", policy(version="v2"), expected_generation=1)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE source_heads SET generation=1")
    with pytest.raises(SourceStoreError, match="source_head_integrity_failed"):
        store.export_snapshot()


def test_attachment_bytes_are_rechecked_against_committed_revision(store):
    raw = b"synthetic attachment exact bytes\n"
    attachment = store.attachment_root / "sample.txt"
    attachment.write_bytes(raw)
    descriptor = {**policy(), "source_ref": "synthetic:attachment", "record": {"files": [{
        "path": "sample.txt", "purpose": "resume", "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw)}]}}
    store.put_source(SCOPE, "attachments", descriptor, expected_generation=0)
    attachment.write_bytes(b"changed attachment bytes\n")
    with pytest.raises(SourceStoreError, match="source_material_integrity_failed"):
        store.export_snapshot()


def test_audit_contains_identifiers_and_hashes_but_no_answer_values(store):
    secret = "SYNTHETIC-ANSWER-NOT-FOR-AUDIT"
    descriptor = {**policy(), "source_ref": "synthetic:answers", "record": {
        "fields": {"name": secret}, "provenance": {"name": {"source_ref": "synthetic:human-entry",
            "source_version": "v1", "origin": "human", "observed_at": stamp(-30), "evidence_refs": []}}}}
    store.put_source(SCOPE, "answers", descriptor, expected_generation=0)
    events = store.events()
    assert secret not in json.dumps(events)
    assert "synthetic:answers" in json.dumps(events)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE source_events SET event_sha256=? WHERE sequence=1", ("0" * 64,))
    with pytest.raises(SourceStoreError, match="audit_integrity_failed"):
        store.events()


def test_maximum_workspace_id_can_be_exported(tmp_path):
    workspace = "w" * 256
    value = SourceStore(tmp_path / "maximum", workspace, clock=lambda: NOW)
    value.register_scope({**SCOPE, "workspace_id": workspace})
    result = report(value)
    assert all(row["component"] != "snapshot" for row in result["root_causes"])


def test_existing_empty_database_is_not_silently_reinitialized(tmp_path):
    home = tmp_path / "broken"
    home.mkdir(mode=0o700)
    db = home / "sources.sqlite3"
    descriptor = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with pytest.raises(SourceStoreError, match="store_identity_missing"):
        SourceStore(home, SCOPE["workspace_id"])


@pytest.mark.parametrize("missing", ["db_path", "identity_path"])
def test_missing_persisted_state_is_not_silently_recreated(store, missing):
    path = getattr(store, missing)
    path.unlink()
    code = "source_database_missing" if missing == "db_path" else "store_identity_missing"
    with pytest.raises(SourceStoreError, match=code):
        SourceStore(store.home, SCOPE["workspace_id"])
    with pytest.raises(SourceStoreError, match=code):
        store.export_snapshot()
    assert not path.exists()


def test_corrupted_database_with_retained_identity_is_not_reinitialized(store):
    store.db_path.write_bytes(b"")
    with pytest.raises(SourceStoreError, match="database_schema_invalid"):
        SourceStore(store.home, SCOPE["workspace_id"])


def test_distinct_stores_have_distinct_snapshot_provenance_and_cannot_swap_identity(store, tmp_path):
    other = SourceStore(tmp_path / "other", SCOPE["workspace_id"], clock=lambda: NOW)
    assert store.export_snapshot()["snapshot"]["source_ref"] != other.export_snapshot()["snapshot"]["source_ref"]
    store.identity_path.write_bytes(other.identity_path.read_bytes())
    with pytest.raises(SourceStoreError, match="store_identity_mismatch"):
        store.export_snapshot()
    with pytest.raises(SourceStoreError, match="store_identity_mismatch"):
        SourceStore(store.home, SCOPE["workspace_id"])
