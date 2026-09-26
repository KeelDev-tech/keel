#!/usr/bin/env python3
"""Keel mirror ports (2026-09-18): K21/K22 live_cache + K23 page_capture.

Mirrors the application-executor fixes into ~/workspace/keel/engines:
- K21: fresh_live requires a non-negative age within TTL (future-dated
  "live" stamps fail closed instead of short-circuiting re-verify).
- K22: _norm_url lowercases scheme + host only; path/query case is
  significant, so a case-changed application_url is re-checked.
- K23: load_capture enforces manifest integrity — captured=True, PDT age
  bound, capture_path contained in CAPTURE_DIR (symlinks resolved),
  basename == <sha256>.html, byte count + sha256 match, 4 MiB cap.

Run: python3 -m pytest test_mirror_k21k22k23.py (from ~/workspace/keel/tests)
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)

import live_cache as lc
import page_capture as pc


@pytest.fixture()
def iso_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "CACHE", str(tmp_path / "cache.json"))


@pytest.fixture()
def iso_captures(tmp_path, monkeypatch):
    d = str(tmp_path / "captures")
    monkeypatch.setattr(pc, "CAPTURE_DIR", d)
    monkeypatch.setattr(pc, "MANIFEST", os.path.join(d, "manifest.jsonl"))


def _write_cache_entry(role_id, url, verdict, ts):
    data = {role_id: {"verdict": verdict, "url": url, "ts": ts}}
    with open(lc.CACHE, "w") as f:
        json.dump(data, f)


def _iso(ts):
    return ts.astimezone(timezone.utc).isoformat()


# --- K21: future-dated stamps fail closed ---------------------------------

def test_future_dated_live_stamp_is_not_fresh(iso_cache):
    future = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
    _write_cache_entry("R1", "https://example.com/jobs/1", "live", future)
    assert lc.fresh_live("R1", "https://example.com/jobs/1") is False


def test_fresh_stamp_within_ttl_still_fresh(iso_cache):
    recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    _write_cache_entry("R1", "https://example.com/jobs/1", "live", recent)
    assert lc.fresh_live("R1", "https://example.com/jobs/1") is True


def test_expired_stamp_is_not_fresh(iso_cache):
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=3))
    _write_cache_entry("R1", "https://example.com/jobs/1", "live", old)
    assert lc.fresh_live("R1", "https://example.com/jobs/1") is False


# --- K22: path/query case preserved ----------------------------------------

def test_path_case_change_is_url_mismatch(iso_cache):
    lc.record("R1", "https://example.com/Jobs/ABC", "live")
    assert lc.fresh_live("R1", "https://example.com/jobs/abc") is False


def test_scheme_host_case_still_folds(iso_cache):
    lc.record("R1", "HTTPS://EXAMPLE.COM/jobs/1/", "live")
    assert lc.fresh_live("R1", "https://example.com/jobs/1") is True


# --- K23: capture integrity -------------------------------------------------

def _manifest(iso_captures, body: bytes, **over):
    d = pc.CAPTURE_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    path = os.path.join(d, digest + ".html")
    with open(path, "wb") as f:
        f.write(body)
    m = {"captured": True, "url": "https://example.com/listing/1",
         "capture_path": path, "sha256": digest, "bytes": len(body),
         "captured_at": pc.pdt_stamp()}
    m.update(over)
    return m


def test_round_trip_ok(iso_captures):
    body = b"<html>listing</html>"
    assert pc.load_capture(_manifest(iso_captures, body)) == body


def test_path_traversal_fails_closed(iso_captures):
    m = _manifest(iso_captures, b"x")
    m["capture_path"] = "/etc/passwd"
    m["sha256"] = "0" * 64
    assert pc.load_capture(m) is None


def test_tampered_bytes_fail_closed(iso_captures):
    body = b"<html>listing</html>"
    m = _manifest(iso_captures, body)
    with open(m["capture_path"], "wb") as f:
        f.write(b"<html>TAMPERED</html>")
    assert pc.load_capture(m) is None


def test_wrong_basename_fails_closed(iso_captures):
    body = b"<html>listing</html>"
    m = _manifest(iso_captures, body)
    bad = os.path.join(pc.CAPTURE_DIR, "renamed.html")
    os.rename(m["capture_path"], bad)
    m["capture_path"] = bad
    assert pc.load_capture(m) is None


def test_stale_capture_fails_closed(iso_captures):
    body = b"<html>listing</html>"
    stale = (datetime.now(pc.PDT) - timedelta(hours=5)).strftime(
        "%Y-%m-%d %H:%M:%S %Z")
    m = _manifest(iso_captures, body, captured_at=stale)
    assert pc.load_capture(m, max_age_hours=2) is None


def test_missing_stamp_fails_closed(iso_captures):
    m = _manifest(iso_captures, b"x", captured_at=None)
    assert pc.load_capture(m) is None


def test_uncaptured_manifest_fails_closed(iso_captures):
    assert pc.load_capture({"captured": False, "reason": "http_307"}) is None
    assert pc.load_capture(None) is None
