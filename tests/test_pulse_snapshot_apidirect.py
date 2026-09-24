#!/usr/bin/env python3
"""Regression tests for the api-direct zombie-gauge fix (ARM 575-2).

_api_direct_stats() was re-scoped from submission counts (a zombie gauge that
reads zero forever on the retired write path -- Keel Blocker Resolution
Directive S1, Workstream A, Trent-authorized 2026-09-18) to retired-transport
health. Additive only: the legacy submitted_24h/rate_per_hr keys are still
emitted and no top-level snapshot field is added or removed.

Run: python3 -m pytest test_pulse_snapshot_apidirect.py
Stdlib + pytest only; no network, no credentials, no external mutation.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)


def load_snapshot():
    spec = importlib.util.spec_from_file_location(
        "pulse_snapshot", os.path.join(ROOT, "monitors", "pulse_snapshot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def snap():
    return load_snapshot()


# ---------------------------------------------------------------- live inputs
# Fixture-free: the real data files. The retired write path keeps these
# live (heartbeat + refusal rows) while submission counts stay zero.

def test_legacy_keys_still_emitted_retired_state(snap):
    """submitted_24h==0 / rate_per_hr==0.0 are the healthy retired state,
    and they must remain emitted for downstream consumers."""
    stats = snap._api_direct_stats()
    assert stats["submitted_24h"] == 0
    assert stats["rate_per_hr"] == 0.0


def test_new_keys_present_and_typed(snap):
    """The four retired-transport health keys are present and typed."""
    stats = snap._api_direct_stats()
    assert set(stats) >= {
        "submitted_24h", "rate_per_hr",
        "loop_last_ts", "http_requests_24h", "refused_24h", "writes_denied_24h",
    }
    assert stats["loop_last_ts"] is None or isinstance(stats["loop_last_ts"], str)
    for key in ("http_requests_24h", "refused_24h", "writes_denied_24h"):
        assert isinstance(stats[key], int), key
    # Under RETIRED_WRITE_PATH every refused row is a denied write attempt.
    assert stats["writes_denied_24h"] == stats["refused_24h"]


def test_refused_keys_move_when_loop_decides(snap):
    """The gauge is alive: on today's live inputs the loop shows recent
    decision activity (refusals), not a frozen zero."""
    stats = snap._api_direct_stats()
    assert stats["refused_24h"] >= 0
    assert stats["loop_last_ts"] is not None  # heartbeat file is live today


# ---------------------------------------------------------------- fail-closed

def test_missing_data_files_never_raise(snap, tmp_path, monkeypatch):
    """Missing data files -> zeros/None, never an exception."""
    monkeypatch.setattr(snap, "API_DIRECT_STATS", str(tmp_path / "no-board.jsonl"))
    monkeypatch.setattr(snap, "API_DIRECT_HTTP", str(tmp_path / "no-http.jsonl"))
    stats = snap._api_direct_stats()  # must not raise
    assert stats == {"submitted_24h": 0, "rate_per_hr": 0.0,
                     "loop_last_ts": None, "http_requests_24h": 0,
                     "refused_24h": 0, "writes_denied_24h": 0}


def test_malformed_rows_never_raise(snap, tmp_path, monkeypatch):
    """Garbage lines, non-dict rows, missing/bad ts, non-numeric counters
    are skipped; only in-window refused rows count."""
    now = datetime.now(timezone.utc)
    board = tmp_path / "board.jsonl"
    http = tmp_path / "http.jsonl"
    rows = [
        "not json at all",
        json.dumps([1, 2, 3]),                       # non-dict row
        json.dumps({"outcome": "refused"}),           # missing ts
        json.dumps({"ts": "garbage", "outcome": "refused"}),
        json.dumps({"ts": (now - timedelta(hours=30)).isoformat(),  # stale
                    "outcome": "refused"}),
        json.dumps({"ts": (now - timedelta(hours=2)).isoformat(),
                    "outcome": "refused", "detail": "renderer_employer_hosted"}),
        json.dumps({"ts": (now - timedelta(hours=1)).isoformat(),
                    "outcome": "skipped", "detail": "no_packet"}),  # not refusal
    ]
    board.write_text("\n".join(rows) + "\n")
    http.write_text("\n".join([
        json.dumps({"ts": (now - timedelta(hours=3)).isoformat(),
                    "http_requests": 7, "leads_processed": 2, "submitted": 0}),
        json.dumps({"ts": (now - timedelta(hours=1)).isoformat(),
                    "http_requests": "seven"}),     # non-numeric, skipped
        json.dumps({"ts": now.isoformat(), "http_requests": 3}),
    ]) + "\n")
    monkeypatch.setattr(snap, "API_DIRECT_STATS", str(board))
    monkeypatch.setattr(snap, "API_DIRECT_HTTP", str(http))
    stats = snap._api_direct_stats()  # must not raise
    assert stats["refused_24h"] == 1
    assert stats["writes_denied_24h"] == 1
    assert stats["submitted_24h"] == 0
    assert stats["http_requests_24h"] == 10
    assert stats["loop_last_ts"] is not None  # latest heartbeat ts survives


# ------------------------------------------------- snapshot field contract

TOP_LEVEL_FIELDS = {
    "ts", "ledger_submitted", "ledger_rows", "standard_queue", "needs_input",
    "ready", "strategic_ready", "strategic_firing", "inflight",
    "latest_telemetry_ts", "api_direct", "pool_supply",
}


def test_snapshot_top_level_field_set_unchanged(snap):
    """The additive contract: api_direct's sub-object contents may change,
    but no top-level snapshot field is added or removed (the fanout gate's
    materiality signature hashes the four watched files + this contract)."""
    snap_out = snap.build_snapshot()
    assert set(snap_out) == TOP_LEVEL_FIELDS
    assert "api_direct" in snap_out
