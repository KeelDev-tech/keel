"""Tests for the Keel MCP server safety guardrails.

These tests encode the non-negotiable invariants: the server must never
touch the operator's real pipeline data, credentials, or answer bank, and no tool
may perform an irreversible external action.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-server"))

from safety import (  # noqa: E402
    SafetyError,
    assert_safe_basename,
    check_no_external_action,
    resolve_data_dir,
)


def test_resolve_data_dir_defaults_to_bundled_samples():
    d = resolve_data_dir(None)
    assert d.name == "sample_data"
    assert "mcp-server" in d.parts


def test_resolve_data_dir_refuses_job_pipeline_queue():
    with pytest.raises(SafetyError):
        resolve_data_dir(str(Path.home() / "workspace/job-pipeline/queue"))


def test_resolve_data_dir_refuses_credentials():
    with pytest.raises(SafetyError):
        resolve_data_dir(str(Path.home() / "workspace/job-pipeline/credentials"))


def test_resolve_data_dir_refuses_live_pipeline_tree():
    with pytest.raises(SafetyError):
        resolve_data_dir(str(Path.home() / "workspace/job-pipeline"))


def test_resolve_data_dir_allows_tmp(tmp_path):
    d = resolve_data_dir(str(tmp_path))
    assert d == tmp_path.resolve()


def test_forbidden_basenames():
    for name in ("answer_bank.json", "professional-references.md", ".env"):
        with pytest.raises(SafetyError):
            assert_safe_basename(f"/some/dir/{name}")


def test_example_bank_basename_allowed():
    # The *example* bank is synthetic fixture data, safe to ship.
    assert_safe_basename("answer_bank.example.json")


def test_external_action_allowlist():
    check_no_external_action("http-get")
    check_no_external_action("read-file")
    with pytest.raises(SafetyError):
        check_no_external_action("submit-application")
    with pytest.raises(SafetyError):
        check_no_external_action("send-email")
    with pytest.raises(SafetyError):
        check_no_external_action("write-file")
