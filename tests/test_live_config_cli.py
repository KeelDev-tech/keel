"""Actual local CLI and private host configuration boundaries."""
import json
from pathlib import Path

import pytest

from keel_live.config import load_config, principal_from_config
from keel_live import __main__ as cli
from tools.make_live_demo import create_demo


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def config(tmp_path):
    fixture = create_demo(tmp_path / "fixture")
    value = {"schema": "keel.live_host.v1", "home": str(fixture.store.home),
             "workspace_id": fixture.scope["workspace_id"], "action": "PREPARE",
             "producer_components": {"host": ["policy", "form", "answers", "attachments", "target", "route"]},
             "attachment_source_root": str(tmp_path),
             "operator": {"actor_id": "fixture-operator", "authority_record_ref": "fixture:authority",
                          "allowed_actions": ["review:read", "review:request", "review:decide", "review:revoke"],
                          "allowed_scopes": [fixture.scope]}}
    path = tmp_path / "host.json"
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return fixture, value, path


def test_private_host_config_binds_operator_and_scope(tmp_path):
    fixture, value, path = config(tmp_path)
    loaded = load_config(path)
    actor = principal_from_config(loaded)
    assert actor.actor_id == value["operator"]["actor_id"]
    assert dict(actor.allowed_scopes[0]) == fixture.scope


@pytest.mark.parametrize("change", ["public", "symlink", "hardlink", "unknown", "submit", "forged_scope"])
def test_invalid_host_configuration_is_rejected(tmp_path, change):
    fixture, value, path = config(tmp_path)
    if change == "public": path.chmod(0o644)
    elif change == "symlink":
        link = tmp_path / "linked.json"; link.symlink_to(path); path = link
    elif change == "hardlink": (tmp_path / "linked.json").hardlink_to(path)
    else:
        if change == "unknown": value["unrecognized"] = True
        elif change == "submit": value["action"] = "SUBMIT"
        else: value["operator"]["allowed_scopes"][0]["workspace_id"] = "other-workspace"
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError): load_config(path)


def test_cli_does_not_refresh_stale_flow_into_live_evidence(tmp_path, capsys):
    fixture, _, path = config(tmp_path)
    body = tmp_path / "body.json"; body.write_text(json.dumps(fixture.body))
    output = tmp_path / "proof.json"
    assert cli.main(["proof", "--config", str(path), "--body", str(body), "--out", str(output)]) == 2
    assert not output.exists()
    assert "fresh canonical flow" in capsys.readouterr().err


def test_cli_init_does_not_create_evidence_or_overwrite_output(tmp_path):
    _, value, path = config(tmp_path)
    value["home"] = str(tmp_path / "new-source-store")
    path.write_text(json.dumps(value))
    output = tmp_path / "init.json"
    arguments = ["init", "--config", str(path), "--out", str(output)]
    assert cli.main(arguments) == 0
    before = output.read_bytes()
    assert json.loads(before)["source_records_created"] == 0
    assert cli.main(arguments) == 2
    assert output.read_bytes() == before
