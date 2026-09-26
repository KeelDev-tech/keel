"""Tests for the honesty tools: form probing contract + truthfulness check."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-server"))

from tools import honesty  # noqa: E402


def test_truthfulness_check_flags_missing():
    profile = {"verified_capabilities": ["python", "ops"]}
    role = {"hard_requirements": [
        {"name": "rust", "status": "MISSING", "mandatory": True},
        {"name": "python", "status": "VERIFIED", "mandatory": True},
    ]}
    out = honesty.keel_truthfulness_check(profile, role)
    assert out["gap_count"] == 1
    assert any("rust" in g for g in out["gaps"])


def test_truthfulness_check_clean():
    profile = {"verified_capabilities": ["python"]}
    role = {"hard_requirements": [{"name": "python", "status": "VERIFIED"}]}
    out = honesty.keel_truthfulness_check(profile, role)
    assert out["gap_count"] == 0 and out["gaps"] == []


def test_truthfulness_check_empty_inputs():
    out = honesty.keel_truthfulness_check({}, {})
    assert out["gap_count"] == 0


def test_probe_form_unknown_ats_graceful():
    # example.invalid never resolves; probe_url must degrade, not raise.
    out = honesty.keel_probe_form("https://example.invalid/jobs/123")
    assert isinstance(out, dict)
    assert out['status'] == 'UNAVAILABLE'
    assert out['questions'] is None
    assert out['execution_authorized'] is False


def test_probe_failure_does_not_leak_transport_details(monkeypatch):
    from urllib.error import URLError
    def unavailable(*args, **kwargs):
        raise URLError('secret=synthetic_private_token')
    monkeypatch.setattr(honesty.keel_bridge.form_intel_mod, 'probe_url', unavailable)
    out = honesty.keel_probe_form('https://example.invalid/')
    assert out['status'] == 'UNAVAILABLE'
    assert 'synthetic_private_token' not in str(out)


def test_probe_rate_limit_stays_explicit(monkeypatch):
    from urllib.error import HTTPError
    def limited(*args, **kwargs):
        raise HTTPError('https://example.invalid/', 429, 'limited', {}, None)
    monkeypatch.setattr(honesty.keel_bridge.form_intel_mod, 'probe_url', limited)
    out = honesty.keel_probe_form('https://example.invalid/')
    assert out['status'] == 'RATE_LIMITED'
    assert out['questions'] is None
