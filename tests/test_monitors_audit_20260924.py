"""Offline monitor regressions; all data, time and cadence are synthetic."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def load(name):
    spec = importlib.util.spec_from_file_location("audit_" + name, ROOT / "monitors" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(stamp):
    return {"ts": stamp.isoformat(), "event_type": "scan_summary", "source": "verify-retry", "details": {}}


@pytest.mark.parametrize("bad", [None, [], 7, {"ts": NOW.isoformat(), "details": ["invalid"]}])
def test_malformed_event_does_not_hide_later_worker_signal(tmp_path, bad):
    module = load("fanout_gate")
    path = tmp_path / "telemetry.jsonl"
    path.write_text(json.dumps(bad) + "\n" + json.dumps(event(NOW - timedelta(minutes=1))) + "\n")
    module.TELEMETRY = str(path)
    assert module.scan_telemetry_window(NOW - timedelta(minutes=10), until=NOW)[1] == 1
    assert module.seed_last_verify_retry_signal(until=NOW) == NOW - timedelta(minutes=1)


def test_future_worker_event_cannot_clear_drought(tmp_path):
    module = load("fanout_gate")
    path = tmp_path / "telemetry.jsonl"
    path.write_text(json.dumps(event(NOW + timedelta(hours=1))) + "\n")
    module.TELEMETRY = str(path)
    assert module.scan_telemetry_window(NOW - timedelta(minutes=10), until=NOW)[1] == 0
    assert module.seed_last_verify_retry_signal(until=NOW) is None


def test_calibrated_drought_survives_future_watermark_and_event(tmp_path):
    module = load("fanout_gate")
    module.DROUGHT_CALIBRATED, module.VERIFY_CADENCE_MINUTES, module.VERIFY_DROUGHT_RUNS = True, 120, 3
    queue, telemetry = tmp_path / "queue.json", tmp_path / "events.jsonl"
    queue.write_text(json.dumps([{"status": "PARKED-PENDING-VERIFICATION"}]))
    telemetry.write_text("\n".join(json.dumps(event(ts)) for ts in
                                  (NOW - timedelta(hours=3), NOW + timedelta(hours=1))) + "\n")
    module.QUEUE, module.TELEMETRY = str(queue), str(telemetry)
    snapshot = {"ready": 10, "ledger_submitted": 0, "needs_input": 0, "inflight": []}
    watermark = {"last_run_ts": (NOW - timedelta(minutes=10)).isoformat(),
                 "last_verify_retry_signal_ts": (NOW + timedelta(hours=1)).isoformat(),
                 "verify_drought_runs": 2}
    reasons, _, extra = module.evaluate(snapshot, watermark, NOW)
    assert "verify_drought:3_runs_no_verification" in reasons
    assert extra["drought"] == 3


def test_missing_queue_is_unknown_not_zero_backlog(tmp_path):
    module = load("fanout_gate")
    module.QUEUE = str(tmp_path / "missing.json")
    assert module.count_pending_verify() is None
    module.TELEMETRY = str(tmp_path / "events.jsonl")
    snapshot = {"ready": 10, "ledger_submitted": 0, "needs_input": 0, "inflight": []}
    reasons, signature, _ = module.evaluate(snapshot, {}, NOW)
    assert "pending_verify:unverified_queue" in reasons
    assert signature["pending_verify"] is None


def test_wrapped_queue_is_counted(tmp_path):
    module = load("fanout_gate")
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"entries": [{"status": "PARKED-PENDING-VERIFICATION"}]}))
    module.QUEUE = str(path)
    assert module.count_pending_verify() == 1


def test_repeatedly_unreadable_watched_files_never_quiet(tmp_path, monkeypatch, capsys):
    module = load("fanout_gate")
    snapshot = {"ready": 10, "ledger_submitted": 0, "needs_input": 0, "inflight": []}
    watermark = {"pulse_count": 1, "hashes": {"ledger": None}}
    module.WATCHED_FILES = {"ledger": str(tmp_path / "missing-ledger.json")}
    module.WATERMARK = str(tmp_path / "watermark.json")
    module.FORCED_SCAN_EVERY = 6
    monkeypatch.setattr(module, "load_json", lambda p: watermark if p == module.WATERMARK else snapshot)
    monkeypatch.setattr(module, "resolve_supply_reason", lambda *a: None)
    monkeypatch.setattr(module, "build_watermark", lambda *a: watermark)
    with pytest.raises(SystemExit):
        module.main()
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "FANOUT"
    assert result["reasons"] == ["fail_open:watched_files_unreadable:ledger"]


def test_malformed_counter_alarms_instead_of_crashing(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "monitors"))
    module = load("ewma_drift")
    verify = tmp_path / "verify.jsonl"
    verify.write_text(json.dumps({"ts": NOW.isoformat(), "live": True, "status": "ok",
                                  "counts": {"pool_size": 100, "scanned": float("inf")}}) + "\n")
    telemetry = tmp_path / "events.jsonl"
    telemetry.write_text("")
    ledger = tmp_path / "ledger.json"
    ledger.write_text("[]")
    module.VERIFY_LOG, module.TELEMETRY, module.LEDGER = map(str, (verify, telemetry, ledger))
    assert module.main(["--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "GATE"
    assert result["trigger"] == "input_invalid"
    assert result["fail_open"] is True
    assert result["gate_emitted"] is False
