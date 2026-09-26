#!/usr/bin/env python3
"""2026-09-21 build-push port: candidate _scan_and_apply pinned.

The recovery candidate replaced verify_retry._scan_and_apply with a
compatibility shim delegating to pipeline_service.verify (bounded
board-cohort verification, 120s network budget, per-row compare-and-write,
durable outbox replay; NO ready promotion, no multi-file snapshot
rewrites, no title guesses). This file pins that ported behavior.

Deliberately NOT ported (out of scope for this push): the candidate's
check_live rewrite (posting_identity plan/observe), the safe_http import,
the posting_url key expansion, and the screen_promotion_* raise
changes. test_check_live_not_rewritten_to_posting_identity guards the
check_live exclusion.

All fixtures synthetic; pipeline_service.verify is mocked (no network).
"""

import inspect
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import pipeline_service  # noqa: E402
import verify_retry  # noqa: E402


def _expected_workspace():
    return Path(verify_retry.STD_Q).absolute().parents[2]


def test_scan_and_apply_delegates_to_pipeline_service_verify():
    report = {"status": "ok", "rows": 3}
    with mock.patch.object(pipeline_service, "verify",
                           return_value=report) as mv:
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = verify_retry._scan_and_apply(False, 7, "wave-1")
    assert out == report
    mv.assert_called_once_with(_expected_workspace(), limit=7,
                               timeout=120, live=False)
    assert json.loads(buf.getvalue()) == report


def test_scan_and_apply_default_limit_is_100():
    with mock.patch.object(pipeline_service, "verify",
                           return_value={}) as mv:
        with redirect_stdout(io.StringIO()):
            verify_retry._scan_and_apply(False, None)
    assert mv.call_args.kwargs["limit"] == 100


def test_scan_and_apply_forwards_live_flag():
    with mock.patch.object(pipeline_service, "verify",
                           return_value={}) as mv:
        with redirect_stdout(io.StringIO()):
            verify_retry._scan_and_apply(True, 5)
    assert mv.call_args.kwargs["live"] is True


def test_verification_unavailable_is_runtime_error():
    assert issubclass(verify_retry.VerificationUnavailable, RuntimeError)
    try:
        raise verify_retry.VerificationUnavailable("probe")
    except RuntimeError as exc:
        assert str(exc) == "probe"
    else:
        raise AssertionError("did not raise")


def test_historical_helpers_still_importable():
    for name in ("scan_lead_sync", "build_attempt_record",
                 "assemble_attempt_record", "check_live", "main",
                 "posting_url", "is_verify_only"):
        assert callable(getattr(verify_retry, name)), name


def test_check_live_not_rewritten_to_posting_identity():
    # The candidate rewrote check_live onto posting_identity.plan/observe;
    # that rewrite was excluded from this port. Pin the exclusion.
    src = inspect.getsource(verify_retry.check_live)
    assert "posting_identity" not in src
    assert "Only an exact public API record" not in (
        verify_retry.check_live.__doc__ or "")
