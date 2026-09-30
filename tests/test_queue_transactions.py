"""Queue ownership is recoverable after every durable step; no live files."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from datetime import datetime, timezone

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "engines"
sys.path.insert(0, str(ENGINES))
import queue_io
import prescreen


@pytest.fixture
def queues(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(tmp_path / "queue.lock"))
    source, destination = tmp_path / "source.json", tmp_path / "destination.json"
    source.write_text(json.dumps({"entries": [{"role_id": "TEST-R", "status": "READY"}],
                                  "revision": 8, "metadata": {"origin": "fixture"}}))
    destination.write_text(json.dumps({"leads": [], "revision": 3}))
    return source, destination


def snapshots(paths):
    return {path: queue_io.read_snapshot(path) for path in paths}


def pending_journals():
    directory = Path(queue_io._transaction_dir())
    return list(directory.glob("*.json")) if directory.exists() else []


def assert_one_home(source, destination):
    s = queue_io.queue_entries(queue_io.read_snapshot(source))
    d = queue_io.queue_entries(queue_io.read_snapshot(destination))
    assert [row for row in s if row["role_id"] == "TEST-R"] == []
    assert len([row for row in d if row["role_id"] == "TEST-R"]) == 1


@pytest.mark.parametrize("unsafe", [datetime.now(timezone.utc), {1, 2}, object(),
                                     float("nan"), float("inf"), {1: "integer key"}])
def test_pre_serialization_refuses_before_any_write(queues, unsafe):
    source, destination = queues
    before = {path: path.read_bytes() for path in queues}
    with pytest.raises((TypeError, ValueError)):
        queue_io.commit_snapshot({source: [], destination: [{"bad": unsafe}]}, snapshots(queues))
    assert {path: path.read_bytes() for path in queues} == before
    assert not Path(queue_io.get_lock_path()).exists()
    assert not pending_journals()


def test_datetime_mutation_is_explicit_and_preserves_envelopes(queues):
    source, destination = queues
    stamp = datetime(2026, 9, 29, 12, 1, tzinfo=timezone.utc)
    result = queue_io.move_entry_atomic("TEST-R", source, destination,
                                       lambda row: dict(row, verified_at=stamp), op_id="fixture-op")
    assert result["status"] == "COMMITTED"
    assert result["record"]["verified_at"] == stamp.isoformat()
    assert queue_io.read_snapshot(source)["metadata"] == {"origin": "fixture"}
    assert queue_io.read_snapshot(source)["revision"] == 8
    assert queue_io.read_snapshot(destination)["revision"] == 3
    assert_one_home(source, destination)
    assert not pending_journals()


@pytest.mark.parametrize("key", ["entries", "items", "leads"])
def test_every_legacy_envelope_survives(queues, key):
    source, destination = queues
    source.write_text(json.dumps({key: [{"role_id": "TEST-R"}], "meta": ["keep"]}))
    destination.write_text(json.dumps({key: [], "other": {"keep": True}}))
    queue_io.move_entry_atomic("TEST-R", source, destination)
    assert queue_io.read_snapshot(source) == {key: [], "meta": ["keep"]}
    assert queue_io.read_snapshot(destination)["other"] == {"keep": True}


def test_missing_destination_is_created_during_commit(queues):
    source, destination = queues
    destination.unlink()
    queue_io.move_entry_atomic("TEST-R", source, destination)
    assert_one_home(source, destination)


@pytest.mark.parametrize("bad", [{"unexpected": []}, {"entries": [], "leads": []},
                                 {"entries": "not records"}, ["not a record"]])
def test_ambiguous_or_malformed_envelopes_fail_closed(queues, bad):
    source, destination = queues
    destination.write_text(json.dumps(bad))
    before = source.read_bytes(), destination.read_bytes()
    with pytest.raises(ValueError):
        queue_io.move_entry_atomic("TEST-R", source, destination)
    assert (source.read_bytes(), destination.read_bytes()) == before
    assert not pending_journals()


def test_full_snapshot_cas_checks_metadata_and_read_dependencies(queues, tmp_path):
    source, destination = queues
    dependency = tmp_path / "other.json"
    dependency.write_text("[]")
    expected = snapshots([*queues, dependency])
    dependency.write_text('[{"role_id":"TEST-OTHER"}]')
    before = source.read_bytes(), destination.read_bytes()
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.commit_snapshot({source: [], destination: []}, expected)
    assert (source.read_bytes(), destination.read_bytes()) == before
    assert not pending_journals()
    expected = snapshots(queues)
    changed = queue_io.read_snapshot(source)
    changed["revision"] += 1
    source.write_text(json.dumps(changed))
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.commit_snapshot({source: [], destination: []}, expected)
    assert queue_io.read_snapshot(source)["revision"] == 9


def test_cas_is_type_sensitive(queues):
    source, destination = queues
    expected = snapshots(queues)
    expected[source]["revision"] = 8.0
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.commit_snapshot({source: []}, expected)
    assert queue_io.read_snapshot(source)["revision"] == 8
    assert not pending_journals()


def test_source_and_destination_duplicates_are_refused(queues):
    source, destination = queues
    destination.write_text('[{"role_id":"TEST-R"}]')
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.move_entry_atomic("TEST-R", source, destination)
    destination.write_text("[]")
    source.write_text('[{"role_id":"TEST-R"},{"role_id":"TEST-R"}]')
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.move_entry_atomic("TEST-R", source, destination)
    assert not pending_journals()


def test_move_refuses_racing_writer_during_mutation(queues):
    source, destination = queues
    def race(row):
        changed = queue_io.read_snapshot(source)
        changed["entries"][0]["status"] = "SUBMITTED"
        source.write_text(json.dumps(changed))
        row["status"] = "PARKED"
        return row
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.move_entry_atomic("TEST-R", source, destination, race)
    assert queue_io.read_snapshot(source)["entries"][0]["status"] == "SUBMITTED"
    assert queue_io.queue_entries(queue_io.read_snapshot(destination)) == []


@pytest.mark.parametrize("step", ["prepared", "write:0", "write:1", "verified", "committed", "cleaned"])
def test_process_death_after_each_durable_step_recovers_on_next_lock(queues, step):
    source, destination = queues
    code = '''import os,sys
sys.path.insert(0,sys.argv[1])
import queue_io
queue_io.set_lock_path(sys.argv[2])
def die(step,journal):
    if step == sys.argv[5]: os._exit(86)
queue_io._transaction_step = die
queue_io.move_entry_atomic("TEST-R",sys.argv[3],sys.argv[4],
                          lambda row: dict(row,status="PARKED"),op_id="crash-fixture")
'''
    result = subprocess.run([sys.executable, "-c", code, str(ENGINES), queue_io.get_lock_path(),
                             str(source), str(destination), step], capture_output=True)
    assert result.returncode == 86, result.stderr.decode()
    with queue_io.queue_lock(owner="fixture-restart"):
        assert_one_home(source, destination)
        assert queue_io.queue_entries(queue_io.read_snapshot(destination))[0]["status"] == "PARKED"
    assert not pending_journals()


def test_write_exception_is_recovered_before_any_other_writer(queues, monkeypatch):
    source, destination = queues
    real_write = queue_io.atomic_write_json
    def fail_destination(path, content):
        if str(path) == str(destination):
            raise OSError("injected destination disk error")
        return real_write(path, content)
    with monkeypatch.context() as context:
        context.setattr(queue_io, "atomic_write_json", fail_destination)
        with pytest.raises(OSError):
            queue_io.move_entry_atomic("TEST-R", source, destination)
    assert pending_journals()
    assert queue_io.patch_entry(destination, "TEST-R", {"status": "NEEDS-INPUT"})
    assert_one_home(source, destination)
    assert queue_io.queue_entries(queue_io.read_snapshot(destination))[0]["status"] == "NEEDS-INPUT"
    assert not pending_journals()


def prepare_failed_transaction(queues, monkeypatch):
    def stop(step, journal):
        if step == "write:0":
            raise OSError("injected process interruption")
    with monkeypatch.context() as context:
        context.setattr(queue_io, "_transaction_step", stop)
        with pytest.raises(OSError):
            queue_io.move_entry_atomic("TEST-R", *queues)


def test_read_only_mode_refuses_recovery_without_changing_files(queues, monkeypatch):
    prepare_failed_transaction(queues, monkeypatch)
    paths = [*queues, *pending_journals()]
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(queue_io.QueueRecoveryRequired):
        with queue_io.queue_lock(recover=False):
            pytest.fail("read-only lock exposed partial queue state")
    with pytest.raises(queue_io.QueueRecoveryRequired):
        queue_io.read_snapshot_checked(queues[0])
    assert {path: path.read_bytes() for path in paths} == before
    queue_io.recover_transactions()
    assert_one_home(*queues)


def test_recovery_conflict_never_overwrites_unknown_content(queues, monkeypatch):
    prepare_failed_transaction(queues, monkeypatch)
    source, destination = queues
    source.write_text('[{"role_id":"TEST-R","status":"SUBMITTED"}]')
    before = source.read_bytes(), destination.read_bytes()
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.recover_transactions()
    assert (source.read_bytes(), destination.read_bytes()) == before
    assert pending_journals()


def test_corrupt_journal_image_refuses_recovery(queues, monkeypatch):
    prepare_failed_transaction(queues, monkeypatch)
    journal_path = pending_journals()[0]
    value = json.loads(journal_path.read_text())
    value["changes"][1]["after"] = [{"role_id": "TEST-OTHER"}]
    journal_path.write_text(json.dumps(value))
    with pytest.raises(queue_io.QueueTransactionConflict):
        queue_io.recover_transactions()
    assert queue_io.queue_entries(queue_io.read_snapshot(queues[1])) == []


def test_prescreen_parking_uses_transaction_and_preserves_metadata(queues, tmp_path):
    source, destination = queues
    standard = tmp_path / "standard-queue.json"
    needs_input = tmp_path / "needs_input-queue.json"
    source.rename(standard)
    destination.rename(needs_input)
    result = prescreen.park_lead("TEST-R", ["applicant travel answer needed"],
                                 queue_dir=str(tmp_path), backup=False)
    assert result["ok"]
    assert_one_home(standard, needs_input)
    assert queue_io.read_snapshot(standard)["revision"] == 8
    assert queue_io.read_snapshot(needs_input)["revision"] == 3
    assert queue_io.queue_entries(queue_io.read_snapshot(needs_input))[0]["unresolved"] == [
        "applicant travel answer needed"]


def test_one_thousand_bounded_randomized_moves_preserve_one_home(queues):
    source, destination = queues
    randomizer = random.Random(29)
    success = 0
    for index in range(1000):
        if index % 13 == 0:
            unsafe = {1, 2} if randomizer.randrange(2) else object()
            before = source.read_bytes(), destination.read_bytes()
            with pytest.raises(TypeError):
                queue_io.move_entry_atomic("TEST-R", source, destination,
                                           lambda row: dict(row, invalid=unsafe))
            assert (source.read_bytes(), destination.read_bytes()) == before
        else:
            queue_io.move_entry_atomic("TEST-R", source, destination,
                                       lambda row: dict(row, sequence=index,
                                                        stamp=datetime(2026, 9, 29)))
            assert_one_home(source, destination)
            source, destination = destination, source
            success += 1
    assert success == 923
    assert not pending_journals()


def test_patch_preserves_the_complete_queue_envelope(queues):
    source, _ = queues
    assert queue_io.patch_entry(source, "TEST-R", {"status": "PARKED"})
    value = queue_io.read_snapshot(source)
    assert value["revision"] == 8
    assert value["metadata"] == {"origin": "fixture"}
    assert value["entries"][0]["status"] == "PARKED"


def test_clean_read_only_lock_skips_diagnostic_writes(queues):
    lock = Path(queue_io.get_lock_path())
    lock.write_text("")
    before = {path.name for path in lock.parent.iterdir()}
    with queue_io.queue_lock(recover=False):
        assert queue_io.read_snapshot_checked(queues[0])["revision"] == 8
    assert {path.name for path in lock.parent.iterdir()} == before


def test_physical_path_alias_cannot_be_a_cross_file_transaction(queues, tmp_path):
    source, _ = queues
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        queue_io.move_entry_atomic("TEST-R", source, alias / source.name)
    assert queue_io.queue_entries(queue_io.read_snapshot(source))[0]["role_id"] == "TEST-R"
    assert not pending_journals()
