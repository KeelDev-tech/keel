"""Public CLI lifecycle, path bounds, and truthful exit status."""
from datetime import timedelta
import json
from pathlib import Path
import stat
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_workflow.__main__ import main
from keel_workflow.reviews import ReviewStore
from tools.make_workflow_demo import make_fixture, NOW


def fixture_input(tmp_path):
    bundle, flow, assurance, trust = make_fixture()
    doc = bundle.document
    for name, body in bundle.attachments:
        (tmp_path / name).write_bytes(body)
    doc.pop("schema")
    doc["attachments"] = {name: name for name, _ in bundle.attachments}
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps({"bundle": doc, "flow_export": flow, "assurance_export": assurance, "trust_export": trust}))
    return path


def call(capsys, args, expected=0):
    code = main(args)
    captured = capsys.readouterr()
    assert code == expected, captured.err
    return json.loads(captured.out) if captured.out else captured.err


def test_candidate_output_is_private_and_never_overwritten(tmp_path, capsys):
    path = fixture_input(tmp_path)
    output = tmp_path / "candidate-report.json"
    assert main(["candidate", str(path), "--now", NOW.isoformat(), "--out", str(output)]) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    original = output.read_bytes()
    assert main(["candidate", str(path), "--now", NOW.isoformat(), "--out", str(output)]) == 2
    assert output.read_bytes() == original


@pytest.mark.parametrize("relative", ["../outside.txt", "/tmp/outside.txt"])
def test_attachment_paths_cannot_escape_input_directory(tmp_path, capsys, relative):
    path = fixture_input(tmp_path)
    data = json.loads(path.read_text())
    data["bundle"]["attachments"]["resume.txt"] = relative
    path.write_text(json.dumps(data))
    assert main(["candidate", str(path), "--now", NOW.isoformat()]) == 2


@pytest.mark.parametrize("mode,status,exit_code", [("confirmed", "SIMULATED_CONFIRMED", 0),
                                                   ("timeout", "UNKNOWN", 3), ("not_sent", "NOT_SENT", 3)])
def test_cli_full_blind_review_then_simulation(tmp_path, capsys, mode, status, exit_code):
    path = fixture_input(tmp_path)
    review_db, delivery_db = tmp_path / "reviews.db", tmp_path / "delivery.db"
    at = ["--now", NOW.isoformat()]
    expiry = (NOW + timedelta(minutes=10)).isoformat()
    created = call(capsys, ["review-create", str(path), "--db", str(review_db), "--round-id", "round-1", "--expires", expiry, *at])
    sha = created["subject_sha256"]
    base = ["--db", str(review_db), "--round-id", "round-1", "--subject-sha256", sha, *at]
    first = "synthetic-source-check"
    second = "synthetic-adversarial-check"
    task = call(capsys, ["review-task", "--reviewer", first, "--phase", "A", *base])
    assert "commitments" not in task
    call(capsys, ["review-task", "--reviewer", first, "--phase", "B", *base], expected=2)
    judgment = tmp_path / "judgment.json"
    judgment.write_text(json.dumps({"verdict": "PASS", "covered_claim_ids": ["claim-1"], "findings": []}))
    for reviewer in (first, second):
        call(capsys, ["review-commit", str(judgment), "--reviewer", reviewer, *base])
    call(capsys, ["review-seal", *base])
    judgment.write_text(json.dumps({"verdict": "PASS", "findings": []}))
    for reviewer in (first, second):
        call(capsys, ["review-task", "--reviewer", reviewer, "--phase", "B", *base])
        call(capsys, ["review-audit", str(judgment), "--reviewer", reviewer, *base])
    assert call(capsys, ["review-status", *base])["state"] == "READY_FOR_HUMAN_REVIEW"
    approved = call(capsys, ["simulation-approve", str(path), "--db", str(delivery_db), "--approver", "fixture-owner",
                            "--authority-ref", "fixture:authority", "--expires", expiry, *at])
    result = call(capsys, ["simulate", str(path), "--review-db", str(review_db), "--delivery-db", str(delivery_db),
                          "--round-id", "round-1", "--approval-id", approved["approval_id"], "--idempotency-key", "once",
                          "--account-id", "synthetic-account", "--authority-ref", "fixture:authority", "--mode", mode, *at], expected=exit_code)
    assert result["simulation"]["status"] == status
    assert not result["simulation"]["submitted"] and not result["execution_authorized"]


def test_malformed_duplicate_json_keys_fail(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"spec": {}, "spec": {}}')
    assert main(["verify", str(path)]) == 2
