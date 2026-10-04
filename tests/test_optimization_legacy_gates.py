"""Legacy entry points cannot turn missing authority into permission."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import answer_resolver as answers
import apply_loop


@pytest.mark.parametrize("failure", ["missing", "acquire", "cli_timeout", "cli_error"])
def test_launch_guard_unavailable_or_unconfirmed_is_denied(monkeypatch, failure):
    monkeypatch.setattr(apply_loop, "launch_lock", None)
    monkeypatch.setattr(apply_loop.os.path, "isfile", lambda path: failure.startswith("cli_"))
    if failure == "acquire":
        def broken(*args, **kwargs): raise OSError("fixture storage unavailable")
        monkeypatch.setattr(apply_loop, "launch_lock", SimpleNamespace(acquire=broken))
    if failure.startswith("cli_"):
        def broken(*args, **kwargs):
            if failure == "cli_timeout": raise subprocess.TimeoutExpired("fixture", 30)
            raise OSError("fixture process unavailable")
        monkeypatch.setattr(apply_loop.subprocess, "run", broken)
    permitted, task, reason = apply_loop._launch_guard("fixture-role", "Fixture", "Role")
    assert permitted is False
    assert task
    assert reason


@pytest.mark.parametrize("guard", [
    SimpleNamespace(prelaunch_guard=lambda *a, **kw: ("yes", {"verdict": "GO"})),
    SimpleNamespace(prelaunch_guard=lambda *a, **kw: (True, ["GO"])),
    SimpleNamespace(acquire=lambda *a, **kw: ("yes", {"status": "ACQUIRED"})),
])
def test_malformed_module_guard_cannot_authorize(monkeypatch, guard):
    monkeypatch.setattr(apply_loop, "launch_lock", guard)
    assert apply_loop._launch_guard("fixture-role", "Fixture", "Role")[0] is False


@pytest.mark.parametrize("output", ["GO", '{"verdict":"REFUSE","note":"GO denied"}',
                                    '{"verdict":"GO","verdict":"REFUSE"}'])
def test_cli_guard_requires_explicit_structured_success(monkeypatch, output):
    monkeypatch.setattr(apply_loop, "launch_lock", None)
    monkeypatch.setattr(apply_loop.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(apply_loop.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=output, stderr=""))
    assert apply_loop._launch_guard("fixture-role", "Fixture", "Role")[0] is False


@pytest.mark.parametrize("kind", ["module", "acquire", "cli"])
def test_legitimate_guard_success_still_authorizes(monkeypatch, kind):
    info = {"verdict": "GO", "status": "ACQUIRED", "role_id": "fixture-role"}
    if kind == "module":
        monkeypatch.setattr(apply_loop, "launch_lock", SimpleNamespace(prelaunch_guard=lambda *a, **kw: (True, info)))
    elif kind == "acquire":
        monkeypatch.setattr(apply_loop, "launch_lock", SimpleNamespace(acquire=lambda *a, **kw: (True, info)))
    else:
        monkeypatch.setattr(apply_loop, "launch_lock", None)
        monkeypatch.setattr(apply_loop.os.path, "isfile", lambda path: True)
        monkeypatch.setattr(apply_loop.subprocess, "run", lambda *a, **kw:
                            SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr=""))
    assert apply_loop._launch_guard("fixture-role", "Fixture", "Role")[0] is True


@pytest.mark.parametrize("failure", ["exception", "invalid_verdict"])
def test_refresh_prescreen_failure_never_publishes_buffer_state(tmp_path, monkeypatch, failure):
    entry = {"role_id": "fixture-role", "company": "Fixture", "title": "Role",
             "status": "READY", "fit_score": 80}
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps({"launch_task_id": "fixture-task"}))
    saved, released, events = [], [], []
    monkeypatch.setattr(apply_loop, "PACKETS", str(tmp_path))
    monkeypatch.setattr(apply_loop, "_buffer_lock", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(apply_loop, "load_buffer_state", lambda: [])
    monkeypatch.setattr(apply_loop, "save_buffer_state", lambda state: saved.extend(state))
    monkeypatch.setattr(apply_loop, "_load_buffer_watermark", lambda: {})
    monkeypatch.setattr(apply_loop, "_save_buffer_watermark", lambda *args: None)
    monkeypatch.setattr(apply_loop, "load_answer_bank", lambda: {})
    monkeypatch.setattr(apply_loop, "eligible", lambda *args: (True, "fixture"))
    monkeypatch.setattr(apply_loop, "_launch_guard", lambda *args: (True, "fixture-task", ""))
    monkeypatch.setattr(apply_loop, "build_packet", lambda *a, **kw: str(packet))
    monkeypatch.setattr(apply_loop, "_release_launch_lock", lambda *args: released.append(args))
    monkeypatch.setattr(apply_loop.log_event, "log", lambda *args, **kwargs: events.append((args, kwargs)))

    def screen(*args):
        if failure == "exception": raise OSError("fixture prescreen unavailable")
        return {"verdict": "UNVERIFIED", "reasons": []}

    monkeypatch.setattr(apply_loop.prescreen, "screen_packet", screen)
    assert apply_loop.refresh_buffer([(entry, "standard")]) == 0
    assert saved == []
    assert released == [("fixture-role", "fixture-task")]
    assert not packet.exists()
    assert all(event[0][0] != "brief_built" for event in events)


@pytest.mark.parametrize("failure", ["exception", "invalid_verdict"])
def test_buffered_packet_prescreen_failure_cannot_mark_inflight(tmp_path, monkeypatch, failure):
    entry = {"role_id": "fixture-role", "company": "Fixture", "title": "Role",
             "status": "READY", "fit_score": 80}
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps({"launch_task_id": "fixture-task"}))
    inflight, released, events = [], [], []
    monkeypatch.setattr(apply_loop, "PACKETS", str(tmp_path))
    monkeypatch.setattr(apply_loop.sys, "argv", ["apply_loop.py"])
    monkeypatch.setattr(apply_loop, "load_queue", lambda: [entry])
    monkeypatch.setattr(apply_loop, "load_strategic_queue", lambda: [])
    monkeypatch.setattr(apply_loop, "load_answer_bank", lambda: {})
    monkeypatch.setattr(apply_loop, "eligible", lambda *args: (True, "fixture"))
    monkeypatch.setattr(apply_loop.rate_limits, "is_allowed", lambda company: (True, "fixture"))
    monkeypatch.setattr(apply_loop, "_buffered_fresh_packet", lambda role: str(packet))
    monkeypatch.setattr(apply_loop, "_claim_or_release", lambda *args: str(packet))
    monkeypatch.setattr(apply_loop, "mark_inflight", lambda *args: inflight.append(args))
    monkeypatch.setattr(apply_loop, "_release_launch_lock", lambda *args: released.append(args))
    monkeypatch.setattr(apply_loop.log_event, "log", lambda *args, **kwargs: events.append((args, kwargs)))

    def screen(*args):
        if failure == "exception": raise OSError("fixture prescreen unavailable")
        return {"verdict": "UNVERIFIED", "reasons": []}

    monkeypatch.setattr(apply_loop.prescreen, "screen_packet", screen)
    apply_loop.main()
    assert inflight == []
    assert released == [("fixture-role", "fixture-task")]
    assert all(event[0][0] not in {"lead_verified", "brief_built"} for event in events)
    assert not packet.exists()
    assert (tmp_path / "archive" / "packet.json").exists()


@pytest.mark.parametrize("field", ["expires", "expiry", "valid_until", "expires_at"])
@pytest.mark.parametrize("value", ["not-a-date", "2026-99-99", False, 0, {}, ["2030-01-01"]])
def test_malformed_expiry_never_resolves_scoped_answer(field, value):
    result = answers.resolve("travel_agreement", {"value": "Yes", "scope": "global", field: value},
                             employer="Fixture", role_context={"role_id": "fixture-role", "company": "Fixture"})
    assert result.status == answers.STATUS_ABSTAIN
    assert result.value == ""


@pytest.mark.parametrize("expiry", [None, "", "2999-01-01T00:00:00+00:00"])
def test_absent_or_valid_future_expiry_preserves_scoped_resolution(expiry):
    result = answers.resolve("fixture_fact", {"value": "Yes", "scope": "global", "expires_at": expiry})
    assert result.status == answers.STATUS_RESOLVED
    assert result.value == "Yes"


def test_genuinely_expired_authority_remains_blocked():
    result = answers.resolve("fixture_fact", {"value": "Yes", "scope": "global",
                             "expires_at": "2000-01-01T00:00:00+00:00"})
    assert result.status == answers.STATUS_ABSTAIN


@pytest.mark.parametrize("secondary", ["not-a-date", "2000-01-01T00:00:00+00:00", False])
def test_future_expiry_alias_cannot_mask_other_invalid_or_expired_authority(secondary):
    result = answers.resolve("fixture_fact", {"value": "Yes", "scope": "global",
                             "expires": "2999-01-01T00:00:00+00:00", "expires_at": secondary})
    assert result.status == answers.STATUS_ABSTAIN


def test_multiple_valid_expiry_aliases_use_earliest_instant():
    result = answers.resolve("fixture_fact", {"value": "Yes", "scope": "global",
                             "expires": "2999-01-01T00:00:00+00:00",
                             "expires_at": "2998-12-31T20:00:00+00:00"})
    assert result.status == answers.STATUS_RESOLVED
    assert result.expiry == "2998-12-31T20:00:00+00:00"


@pytest.mark.parametrize("verdict", [None, {}, {"verdict": "UNKNOWN", "reasons": []},
                                    {"verdict": "CLEAN", "reasons": "invalid"}])
def test_direct_packet_builder_refuses_malformed_prescreen_before_publication(tmp_path, monkeypatch, verdict):
    monkeypatch.setattr(apply_loop, "BASE", str(tmp_path))
    monkeypatch.setattr(apply_loop, "HOME", str(tmp_path))
    monkeypatch.setattr(apply_loop, "load_answer_bank", lambda: {})
    monkeypatch.setattr(apply_loop.form_intel, "probe_url", lambda url: {"ats": "fixture"})
    monkeypatch.setattr(apply_loop.prescreen, "screen_packet", lambda *args: verdict)
    destination = tmp_path / "packets"
    with pytest.raises(ValueError, match="invalid prescreen verdict"):
        apply_loop.build_packet({"role_id": "fixture-role", "company": "Fixture", "title": "Role",
            "ats_url": "https://fixture.invalid/job", "materials": {"resume": "fixture.pdf"}},
            dest_dir=str(destination))
    assert not list(destination.glob("*.json"))
