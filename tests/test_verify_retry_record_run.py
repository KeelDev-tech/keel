#!/usr/bin/env python3
"""Silent-defect sweep (2026-09-19) — verify_retry._record_run observability.

FIX 6a (engines/verify_retry.py:_record_run): `except OSError: pass` made
run-record logging failures invisible. Now emits
`print(..., file=sys.stderr)` on failure (sys was already imported) while
staying non-fatal (never breaks the run).

Fixtures synthetic; the runs log is redirected to tmp.
"""

import json
import os
import sys

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import verify_retry  # noqa: E402


def test_record_run_appends(monkeypatch, tmp_path):
    target = str(tmp_path / "runs.jsonl")
    monkeypatch.setattr(verify_retry, "_RUNS_LOG", target)
    verify_retry._record_run({"probe": True, "n": 1})
    with open(target) as f:
        rows = [json.loads(line) for line in f]
    assert rows == [{"probe": True, "n": 1}]


def test_record_run_failure_is_observable_not_silent(monkeypatch, capsys):
    """Simulate an OSError on the append path: the run must NOT break
    (no exception), and stderr must carry a diagnostic. Before the fix,
    this path did `pass` and capsys.err was empty."""
    def boom(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(verify_retry.os, "makedirs", boom)
    verify_retry._record_run({"probe": True})  # must not raise
    err = capsys.readouterr().err
    assert err, "OSError was swallowed silently"
    assert "verify run record append failed" in err
