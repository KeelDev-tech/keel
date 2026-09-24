#!/usr/bin/env python3
"""Regression tests: inflight_marker write-failure resilience.

FIX 5: _atomic_queue_write referenced queue_io.DiskFullError /
       queue_io.report_disk_full, which do not exist in this tree — on a
       real OSError the except clause itself raised AttributeError,
       masking the original error. getattr guards keep the original
       error propagating.
FIX 6: the claim phase acquires the placeholder lock BEFORE
       queue_io.queue_lock(); a write failure (or corrupt queue) must
       release the placeholder instead of leaving a fresh 2h lock.

Synthetic fixtures only: tmp dirs, never live queue files.
Run: python3 -m pytest test_inflight_marker_resilience.py
"""
import json
import os
import sys

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import inflight_marker as im
import launch_lock
import queue_io
import submit_intent


# ---------------------------------------------------------------- FIX 5

def test_atomic_write_oserror_propagates_without_diskfull_names(
        tmp_path, monkeypatch):
    """queue_io lacks DiskFullError/report_disk_full: the ORIGINAL OSError
    must propagate — no AttributeError from the except clause."""
    assert not hasattr(queue_io, "DiskFullError")
    assert not hasattr(queue_io, "report_disk_full")

    def boom(path, raw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(queue_io, "atomic_write_json", boom)
    with pytest.raises(OSError) as ei:
        im._atomic_queue_write(str(tmp_path / "q.json"), [], role_id="R-1")
    assert ei.value.errno == 28, \
        f"expected the original OSError(28), got {ei.value!r}"


def test_atomic_write_diskfull_branch_when_names_exist(tmp_path, monkeypatch):
    """Where a queue_io DOES define the names, the telemetry hook still
    fires and the error propagates."""

    class FakeDiskFull(OSError):
        def __init__(self):
            super().__init__(28, "No space left on device")
            self.bytes_attempted = 1234

    calls = []

    def fake_report(path, nbytes, ex, role_id=""):
        calls.append((path, nbytes, role_id))

    def boom(path, raw):
        raise FakeDiskFull()

    monkeypatch.setattr(queue_io, "DiskFullError", FakeDiskFull,
                        raising=False)
    monkeypatch.setattr(queue_io, "report_disk_full", fake_report,
                        raising=False)
    monkeypatch.setattr(queue_io, "atomic_write_json", boom)
    with pytest.raises(FakeDiskFull):
        im._atomic_queue_write(str(tmp_path / "q.json"), [], role_id="R-9")
    assert calls == [(str(tmp_path / "q.json"), 1234, "R-9")]


# ---------------------------------------------------------------- FIX 6

@pytest.fixture()
def claim_iso(tmp_path, monkeypatch):
    """Synthetic queue + lock + intent-store isolation for the claim phase."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(launch_lock, "LOCK_DIR", str(lock_dir))
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(tmp_path / "queue.lock"))
    store_dir = tmp_path / "intents"
    store_dir.mkdir()
    submit_intent.set_store_dir(str(store_dir))
    qdir = tmp_path / "queues"
    qdir.mkdir()
    qfile = qdir / "standard-queue.json"
    qfile.write_text(json.dumps([{"role_id": "R-1", "status": "READY"}]))
    im.set_queue_paths({"standard": str(qfile)})
    yield tmp_path
    im.set_queue_paths(None)
    submit_intent.reset_store_dir()


def test_claim_write_failure_releases_placeholder(claim_iso, monkeypatch):
    """Forced write failure during claim -> placeholder lock file gone."""

    def boom(path, raw, role_id=""):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(im, "_atomic_queue_write", boom)
    with pytest.raises(OSError):
        im.mark_inflight("R-1", phase="claim", owner="test")
    assert not os.path.exists(launch_lock._lock_path("R-1")), \
        "placeholder lock leaked after claim write failure"


def test_claim_success_keeps_placeholder_and_marks(claim_iso):
    """Happy path unchanged: marker written, placeholder held by us."""
    res = im.mark_inflight("R-1", phase="claim", owner="test")
    assert res["ok"] is True, res
    assert res["phase"] == "claim"
    lock = launch_lock.check("R-1")
    assert lock is not None and lock["task_id"] == "claim:R-1"
    qfile = claim_iso / "queues" / "standard-queue.json"
    entry = json.loads(qfile.read_text())[0]
    assert entry["status"] == "IN-FLIGHT"
