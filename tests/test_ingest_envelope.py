#!/usr/bin/env python3
"""Tests for ingest_envelope.py ADD-2 hardening (2026-09-16).

ADD-2: worker-reported garbage ats keys made record_outcome.record()
raise ValueError mid-loop, aborting the whole envelope. The record() call
is now wrapped in try/except ValueError -> rejected += 1: one attempt
fails closed, the loop stays alive.

(a) an unknown ats key fails that attempt closed (rejected += 1) while
    later valid attempts still record;
(b) pre-existing fail-closed behavior is intact: an attempt failing
    validate_attempt still counts rejected and the loop continues.
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "worker-charter"))
import ingest_envelope as ie


def _attempt(role_id, ats="greenhouse", company="Acme"):
    return {"role_id": role_id, "company": company, "ats": ats,
            "technique": "t-commit", "outcome": "blocked",
            "note": "parked on a form gate"}


def _envelope(attempts):
    return {"status": "success",
            "result": {"attempts": attempts},
            "telemetry": {"applied_constraints": ["C-11"],
                          "tool_calls_count": 3,
                          "encountered_novel_edge_case": False,
                          "notes_for_evaluator": "test"}}


def _run(env):
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as f:
        json.dump(env, f)
        path = f.name
    old_argv, old_record = sys.argv, ie.record
    recorded_ats = []

    def fake_record(ats, technique, outcome, note, **kw):
        # mirror record_outcome's fail-closed contract (read-only here):
        # unknown ats keys raise ValueError
        if ats == "GARBAGE-ATS-KEY":
            raise ValueError(f"refusing to record outcome: unknown ats key {ats!r}")
        recorded_ats.append(ats)

    ie.record = fake_record
    sys.argv = ["ingest_envelope.py", path]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ie.main()
    finally:
        sys.argv, ie.record = old_argv, old_record
        os.unlink(path)
    return buf.getvalue(), recorded_ats


def test_a_unknown_ats_fails_one_attempt_closed():
    attempts = [_attempt("R-1"), _attempt("R-2", ats="GARBAGE-ATS-KEY"),
                _attempt("R-3")]
    out, recorded_ats = _run(_envelope(attempts))
    assert "recorded=2 rejected=1" in out, f"loop did not survive: {out!r}"
    assert recorded_ats == ["greenhouse", "greenhouse"], recorded_ats
    assert "failing this attempt closed" in out
    print("a ok: unknown ats fails one attempt closed, loop continues "
          "(recorded=2 rejected=1)")


def test_b_invalid_attempt_still_rejected_and_loop_continues():
    attempts = [_attempt("R-1"), _attempt("R-2", company=""),
                _attempt("R-3", ats="GARBAGE-ATS-KEY"), _attempt("R-4")]
    out, recorded_ats = _run(_envelope(attempts))
    assert "recorded=2 rejected=2" in out, f"unexpected counts: {out!r}"
    assert recorded_ats == ["greenhouse", "greenhouse"], recorded_ats
    print("b ok: validate-failure and record-failure both reject one "
          "attempt, loop continues (recorded=2 rejected=2)")


if __name__ == "__main__":
    test_a_unknown_ats_fails_one_attempt_closed()
    test_b_invalid_attempt_still_rejected_and_loop_continues()
    print("ingest_envelope ADD-2 tests pass")
