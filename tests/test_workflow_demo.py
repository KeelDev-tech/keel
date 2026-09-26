"""Run the real offline example and inspect its actual durable recovery evidence."""
import hashlib
import json
import sqlite3

import pytest

from keel_workflow import demo
from keel_workflow.recovery import evaluate_recovery
from keel_workflow.verification import validate_run


@pytest.fixture
def exercised(tmp_path):
    root = tmp_path / "exercise"
    report = demo.run_demo(root)
    assert report["status"] == "PASS", report.get("error", report)
    return root, report


def read(root, name):
    return json.loads((root / name).read_text())


def test_actual_workflow_and_restored_state_have_bound_evidence(exercised):
    root, report = exercised
    assert read(root, "report.json") == report
    assert report["synthetic"] and not report["submitted"] and not report["execution_authorized"]
    assert all(value == 0 for value in report["effects"].values())
    for evidence in report["evidence"]:
        assert hashlib.sha256((root / evidence["ref"]).read_bytes()).hexdigest() == evidence["sha256"]
    run = validate_run(read(root, "workflow-verification.json"))
    assert run["all_pass"] and run["counts"]["PASS"] == 6
    measured = read(root, "evidence/workflow-observations.json")
    assert measured["simulation"]["simulation"]["status"] == "SIMULATED_CONFIRMED"
    assert measured["restarted"]["simulation"] == measured["simulation"]["simulation"]
    assert measured["events_before_restart"] == measured["events_after_restart"]
    assert measured["timed_out"]["simulation"]["status"] == "UNKNOWN"
    assert measured["retry_denial"]["reason_matched"]
    assert measured["changed_bundle_denial"]["reason_matched"]
    assert measured["early_reveal"]["reason_matched"]
    assert not measured["real_model_evaluation"]
    assert all(not row["submitted"] for row in measured["phase_a_views"])
    for name in ("simulation", "restarted", "timed_out"):
        assert measured[name]["simulation"]["submitted"] is False
    assert root.stat().st_mode & 0o777 == 0o700


def test_restored_inventory_covers_exact_files_and_real_database_rows(exercised):
    root, report = exercised
    expected, observed = read(root, "recovery-expected.json"), read(root, "recovery-observed.json")
    actual_names = {path.relative_to(root / "restored").as_posix()
                    for path in (root / "restored").rglob("*") if path.is_file()}
    assert actual_names == {row["object_id"] for row in observed["objects"]}
    assert expected["objects"] == observed["objects"]
    for row in expected["objects"]:
        assert hashlib.sha256((root / "backup" / row["object_id"]).read_bytes()).hexdigest() == row["sha256"]
        assert hashlib.sha256((root / "restored" / row["object_id"]).read_bytes()).hexdigest() == row["sha256"]
    recovery_run = read(root, "recovery-verification.json")
    assert evaluate_recovery(expected, observed, recovery_run) == report["recovery"]
    assert report["recovery"]["ready"]
    restored = read(root, "evidence/restore-observations.json")
    assert restored["backup_sql"] == restored["restored_sql"]
    assert restored["changed_account_denial"]["reason_matched"]
    with sqlite3.connect(root / "restored" / "delivery.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM workflow_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM workflow_attempts").fetchone()[0] == "SIMULATED_CONFIRMED"
    with sqlite3.connect(root / "restored" / "reviews.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM review_commitments").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM review_audits").fetchone()[0] == 2


def test_healthy_empty_restore_is_measured_and_rejected(exercised):
    root, report = exercised
    empty = read(root, "evidence/empty-restore-observations.json")
    assert empty["sqlite"]["integrity_check"] == ["ok"]
    assert empty["sqlite"]["schema"] == [] and empty["sqlite"]["row_counts"] == {}
    observed = read(root, "empty-recovery-observed.json")
    assert observed["provider_healthy"] and observed["inventory_complete"] and observed["objects"] == []
    result = evaluate_recovery(read(root, "recovery-expected.json"), observed,
                               read(root, "empty-recovery-verification.json"))
    assert result == report["empty_restore"] and result["status"] == "NOT_READY"
    assert any(issue["code"] == "missing_object" for issue in result["issues"])


def test_existing_directory_is_never_overwritten(tmp_path):
    root = tmp_path / "existing"; root.mkdir()
    sentinel = root / "report.json"; sentinel.write_bytes(b"preserve me")
    with pytest.raises(FileExistsError):
        demo.run_demo(root)
    assert sentinel.read_bytes() == b"preserve me"
    assert list(root.iterdir()) == [sentinel]


def test_backup_failure_retains_incomplete_report_instead_of_pass(tmp_path, monkeypatch):
    def fail_backup(*_args):
        raise sqlite3.OperationalError("synthetic injected backup failure")
    monkeypatch.setattr(demo, "_backup", fail_backup)
    root = tmp_path / "failed-exercise"
    report = demo.run_demo(root)
    assert report["status"] == "INCOMPLETE"
    assert report["error"]["type"] == "OperationalError"
    assert read(root, "report.json") == report
    assert report["workflow"]["all_pass"]
    assert "recovery" not in report
    assert not report["execution_authorized"]


def test_restore_with_unexpected_object_cannot_be_marked_ready(tmp_path, monkeypatch):
    original = demo._objects
    def extra_object(directory, names=None):
        if directory.name == "restored":
            (directory / "unexpected.txt").write_text("unplanned object")
        return original(directory, names)
    monkeypatch.setattr(demo, "_objects", extra_object)
    root = tmp_path / "extra-object"
    report = demo.run_demo(root)
    assert report["status"] == "FAIL"
    assert not report["recovery"]["ready"]
    assert any(issue["code"] == "unexpected_object" for issue in report["recovery"]["issues"])
