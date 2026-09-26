"""No external wording call without explicit valid operator opt-in."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import triage_llm

LINE = "triage [Your call]: waiting for your answer"


@pytest.fixture
def backend(tmp_path, monkeypatch):
    config = tmp_path / "tray_polish.json"
    script = tmp_path / "external.py"
    script.write_text("# placeholder; never executed\n")
    monkeypatch.setattr(triage_llm, "CONFIG", str(config))
    monkeypatch.setattr(triage_llm, "SKILL_BIN", str(script))
    monkeypatch.setattr(triage_llm, "GEMINI_BIN", str(script))
    triage_llm._reset()
    calls = []
    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=LINE)
    monkeypatch.setattr(triage_llm.subprocess, "run", fake_run)
    return config, calls


@pytest.mark.parametrize("config", [None, {}, [], {"enabled": "false"}, {"enabled": 1},
    {"enabled": True, "timeout_s": "no"}, {"enabled": True, "timeout_s": float("nan")},
    {"enabled": True, "timeout_s": 0}, {"enabled": True, "max_tokens": True},
    {"enabled": True, "gemini": None}, {"enabled": True, "compare": []},
    {"gemini": {"enabled": "true"}}])
def test_missing_or_malformed_config_never_invokes_provider(backend, config):
    path, calls = backend
    if config is not None:
        path.write_text(json.dumps(config))
    assert triage_llm.polish_best(LINE) == LINE
    assert calls == []
    assert not triage_llm.enabled() and not triage_llm.gemini_enabled()


def test_corrupt_json_never_invokes_provider(backend):
    path, calls = backend
    path.write_text("{broken")
    assert triage_llm.polish_best(LINE) == LINE and calls == []


@pytest.mark.parametrize("config", [{"enabled": True}, {"gemini": {"enabled": True}}])
def test_valid_explicit_opt_in_calls_only_selected_provider(backend, config):
    path, calls = backend
    path.write_text(json.dumps(config))
    assert triage_llm.polish_best(LINE) == LINE
    assert len(calls) == 1
