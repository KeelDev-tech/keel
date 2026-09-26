#!/usr/bin/env python3
"""test_pulse_snapshot_refused_alias.py — regression tests for FIX 1 P3
(Pulse 682 ARM 4, J-20260921-1051-feed-4251).

refused_24h and writes_denied_24h counted one number twice. Canonical
name is refused_24h; writes_denied_24h stays as a DEPRECATED alias that
reads the canonical value (kept so existing dashboard/pulse consumers
of either key keep reading the same number).

Pins:
  1. on synthetic board-stats input, both keys equal the refused-row
     count (including the zero state);
  2. both keys stay emitted (no consumer breaks on a dropped key);
  3. the definition site documents refused_24h as the canonical name
     and writes_denied_24h as the deprecated alias;
  4. the alias tracks the canonical value when refusals move.

Run: python3 -m pytest test_pulse_snapshot_refused_alias.py
Stdlib + pytest only; no network, no credentials, no external mutation.
"""

import importlib.util
import inspect
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


def _write_board(tmp_path, n_refused, n_other=0, stale_refused=0):
    """Synthetic board-stats file: n_refused in-window refused rows,
    n_other in-window non-refused rows, stale_refused refused rows 30h
    old (outside the 24h window)."""
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(n_refused):
        rows.append(json.dumps({
            "ts": (now - timedelta(hours=2)).isoformat(),
            "board": "stripe", "role_id": f"R-{i}",
            "outcome": "refused", "detail": "renderer_employer_hosted"}))
    for i in range(n_other):
        rows.append(json.dumps({
            "ts": (now - timedelta(hours=1)).isoformat(),
            "board": "cleana", "role_id": f"OK-{i}",
            "outcome": "dry_run_ok"}))
    for i in range(stale_refused):
        rows.append(json.dumps({
            "ts": (now - timedelta(hours=30)).isoformat(),
            "board": "stripe", "role_id": f"STALE-{i}",
            "outcome": "refused"}))
    p = tmp_path / "board-stats.jsonl"
    p.write_text("\n".join(rows) + ("\n" if rows else ""))
    return p


def test_alias_equals_canonical_on_synthetic(snap, tmp_path, monkeypatch):
    """Both keys equal the refused-row count; stale rows excluded."""
    board = _write_board(tmp_path, n_refused=7, n_other=3, stale_refused=5)
    monkeypatch.setattr(snap, "API_DIRECT_STATS", str(board))
    stats = snap._api_direct_stats()
    assert stats["refused_24h"] == 7
    assert stats["writes_denied_24h"] == stats["refused_24h"]


def test_alias_zero_state(snap, tmp_path, monkeypatch):
    """Zero state: both keys present and equal (never a dropped key)."""
    board = _write_board(tmp_path, n_refused=0)
    monkeypatch.setattr(snap, "API_DIRECT_STATS", str(board))
    stats = snap._api_direct_stats()
    assert "refused_24h" in stats
    assert "writes_denied_24h" in stats
    assert stats["refused_24h"] == 0 == stats["writes_denied_24h"]


def test_alias_tracks_canonical_when_refusals_move(
        snap, tmp_path, monkeypatch):
    """Growing the refused count moves both keys together."""
    board = _write_board(tmp_path, n_refused=2)
    monkeypatch.setattr(snap, "API_DIRECT_STATS", str(board))
    before = snap._api_direct_stats()
    # append 3 more in-window refused rows and re-read
    now = datetime.now(timezone.utc)
    with open(str(board), "a") as f:
        for i in range(3):
            f.write(json.dumps({
                "ts": (now - timedelta(hours=1)).isoformat(),
                "board": "stripe", "role_id": f"NEW-{i}",
                "outcome": "refused"}) + "\n")
    after = snap._api_direct_stats()
    assert after["refused_24h"] == before["refused_24h"] + 3
    assert after["writes_denied_24h"] == after["refused_24h"]


def test_canonical_name_documented_at_definition_site(snap):
    """The definition site documents refused_24h as the canonical name and
    writes_denied_24h as the deprecated alias (FIX 1 P3 hygiene)."""
    src = inspect.getsource(snap._api_direct_stats)
    assert "refused_24h is the" in src and "canonical" in src.lower()
    assert "writes_denied_24h" in src and "DEPRECATED ALIAS" in src
    doc = snap._api_direct_stats.__doc__ or ""
    assert "refused_24h" in doc and "CANONICAL" in doc
    assert "writes_denied_24h" in doc and "DEPRECATED ALIAS" in doc


def test_alias_present_on_live_inputs(snap):
    """Live inputs: both consumers (dashboards reading either key) see
    the same number."""
    stats = snap._api_direct_stats()
    assert stats["writes_denied_24h"] == stats["refused_24h"]
    assert isinstance(stats["refused_24h"], int)
    assert isinstance(stats["writes_denied_24h"], int)
