import contextlib
import io
import json
from pathlib import Path
import pytest
from tools.make_assurance_demo import make_envelope, NOW, refresh_reviews
from keel_assurance.__main__ import main


def run(tmp_path, command, data, *extra):
    path = tmp_path / "input.json"; path.write_text(json.dumps(data))
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        status = main([command, str(path), *extra])
    return status, json.loads(stream.getvalue()) if stream.getvalue() else None


def test_audit_cli(tmp_path):
    status, report = run(tmp_path, "audit", make_envelope(), "--now", NOW.isoformat())
    assert status == 0 and report["state"] == "CHECKS_PASSED" and not report["execution_authorized"]


def test_failed_audit_exit_is_not_success(tmp_path):
    e = make_envelope(); e["actions"][0]["authority"] = None; refresh_reviews(e)
    status, report = run(tmp_path, "audit", e, "--now", NOW.isoformat())
    assert status == 3 and report["state"] == "REVIEW_REQUIRED"


def test_audit_requires_clock(tmp_path):
    assert run(tmp_path, "audit", make_envelope())[0] == 2


def test_unknown_labels_do_not_get_synthetic_scores(tmp_path):
    status, report = run(tmp_path, "metrics", {"predictions": [], "challenges": [], "source_grounding": None})
    assert status == 0 and report["ecs"]["score"] is None


def test_metrics_cli(tmp_path):
    status, result = run(tmp_path, "metrics", {
        "predictions": [{"id": "p1", "probability": .8, "outcome": True}],
        "challenges": [{"id": "c1", "challenged": True, "revised": True,
                        "before_correct": False, "after_correct": True, "ground_truth_ref": "synthetic-label"}],
        "source_grounding": 1})
    assert status == 0 and result["ecs"]["score"] == pytest.approx((1+.8+1)/3)
    assert not result["ecs"]["empirically_validated"]


def test_conformal_cli(tmp_path):
    status, result = run(tmp_path, "conformal", {
        "calibration_records": [{"id": "c", "probability": .9, "outcome": True}],
        "test_records": [{"id": "t", "probability": .8, "outcome": None}], "training_ids": [], "alpha": .1})
    assert status == 0 and result["evaluation"]["predictions"][0]["labels"] == [False, True]


def test_conformal_cli_rejects_leakage(tmp_path):
    assert run(tmp_path, "conformal", {
        "calibration_records": [{"id": "c", "probability": .9, "outcome": True}],
        "test_records": [{"id": "c", "probability": .8, "outcome": None}], "training_ids": [], "alpha": .1})[0] == 2


def test_existing_output_never_overwritten(tmp_path):
    out = tmp_path / "out.json"; out.write_text("keep")
    status, _ = run(tmp_path, "metrics", {"predictions": [], "challenges": [], "source_grounding": None}, "--out", str(out))
    assert status == 2 and out.read_text() == "keep"


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "input.json"; path.write_text('{"predictions":[],"predictions":[]}')
    assert main(["metrics", str(path)]) == 2


def test_planner_cli_runs_supplied_fixture(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "sample_data/assurance-plan.example.json"
    status, report = run(tmp_path, "plan", json.loads(fixture.read_text()))
    assert status == 0 and report["status"] == "FEASIBLE"
    assert report["cost_units"] == 7 and report["execution_authorized"] is False
