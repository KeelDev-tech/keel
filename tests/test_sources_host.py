"""Host observations use injected process results, never models or browsers."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from keel_sources import host


def roster():
    return {"reviewers": [
        {"reviewer_id": "one", "backend": "ollama", "model": "local-a:1",
         "endpoint": "http://127.0.0.1:11434/api/chat"},
        {"reviewer_id": "two", "backend": "llama_cpp", "model": "local-b",
         "endpoint": "http://[::1]:8080/v1/chat/completions"},
    ]}


def executable_files(tmp_path, monkeypatch):
    files = {}
    for name in ("node", "ollama", "llama-server", "chromium"):
        path = tmp_path / name
        path.write_text("fixture only, never executed")
        path.chmod(0o700)
        files[name] = str(path)
    monkeypatch.setattr(host.shutil, "which", lambda name: files.get(name))
    return files


def test_default_never_runs_processes_and_never_claims_ready(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("default must not invoke a subprocess")
    monkeypatch.setattr(host, "_run_probe", forbidden)
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    report = host.inspect_host()
    assert report["schema"] == "keel.host_readiness.v1"
    assert report["status"] == "UNVERIFIED"
    assert report["execution_authorized"] is False
    assert report["models"]["configuration"]["status"] == "MISSING"
    assert report["models"]["inference"]["status"] == "NOT_CHECKED"
    assert report["models"]["server_reachability"]["status"] == "NOT_CHECKED"
    assert report["browser"]["rendered_fixture"]["status"] == "NOT_CHECKED"
    assert report["browser"]["dependencies"]["status"] == "MISSING"
    assert not any(report["actions"].values())
    json.dumps(report, allow_nan=False)


def test_config_present_does_not_attest_weights_hardware_or_cloud(monkeypatch, tmp_path):
    executable_files(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
    report = host.inspect_host(roster())
    model = report["models"]
    assert model["configuration"]["status"] == "AVAILABLE"
    assert model["configuration"]["diversity_verified"] is False
    assert all(row["weights_status"] == "NOT_CHECKED" for row in model["configuration"]["reviewers"])
    assert model["server_cloud_disabled"]["status"] == "UNVERIFIED"
    assert model["client_ollama_no_cloud_set"] is True
    assert model["weights_license"]["status"] == "NOT_CHECKED"
    assert report["hardware"]["memory"]["model_fit_verified"] is False
    assert report["hardware"]["gpu"]["status"] == "NOT_CHECKED"


@pytest.mark.parametrize("mutation", [
    {"endpoint": "https://api.example.com/chat"},
    {"endpoint": "http://token@127.0.0.1:11434/api/chat"},
    {"endpoint": "http://localhost:11434/api/chat"},
    {"endpoint": "http://127.0.0.1:11434/api/pull"},
    {"model": "local-cloud"}, {"api_key": "secret-test-key"},
    {"timeout_seconds": float("nan")},
])
def test_invalid_roster_is_unverified_without_echoing_secrets(mutation):
    config = roster()
    config["reviewers"][0].update(mutation)
    report = host.inspect_host(config)
    assert report["models"]["configuration"]["status"] == "UNVERIFIED"
    assert report["models"]["configuration"]["reviewers"] == []
    assert "secret-test-key" not in json.dumps(report)
    assert "token@" not in json.dumps(report)


@pytest.mark.parametrize("rows", [[], [{}], "not-list", [{}, {}]])
def test_invalid_roster_shape_is_not_ready(rows):
    assert host.inspect_host({"reviewers": rows})["models"]["configuration"]["status"] == "UNVERIFIED"


def test_duplicate_reviewer_ids_rejected():
    config = roster()
    config["reviewers"][1]["reviewer_id"] = "one"
    assert host.inspect_host(config)["models"]["configuration"]["status"] == "UNVERIFIED"


@pytest.mark.parametrize("config", [
    [], {"probe_url": "https://example.com"}, {"browser": []},
    {"browser": {"extra": "value"}}, {"browser": {"node_path": "node"}},
    {"browser": {"chromium_path": "../browser"}},
    {"browser": {"playwright_module": "https://example.com/code.js"}},
    {"browser": {"playwright_module": "other-package"}},
    {"browser": {"node_path": "/tmp/node\n--eval"}},
])
def test_invalid_config_rejected_before_process_probe(config, monkeypatch):
    monkeypatch.setattr(host, "_run_probe", lambda *args: pytest.fail("unexpected probe"))
    with pytest.raises(ValueError):
        host.inspect_host(config, detailed=True)


def test_detail_requires_actual_boolean():
    with pytest.raises(ValueError, match="boolean"):
        host.inspect_host(detailed="yes")


def test_detailed_uses_fixed_node_only_and_does_not_import_or_launch(monkeypatch, tmp_path):
    files = executable_files(tmp_path, monkeypatch)
    calls = []
    def run(argv):
        calls.append(argv)
        if argv[1] == "--version":
            return SimpleNamespace(returncode=0, stdout=b"v22.19.0\n")
        assert argv[1:3] == ["-e", host._NODE_METADATA]
        assert argv[3] == "playwright"
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "status": "AVAILABLE", "reason": "module_entry_resolved",
            "entry": "/installed/playwright/index.js", "package_name": "playwright",
            "version": "1.62.1"}).encode())
    monkeypatch.setattr(host, "_run_probe", run)
    config = roster()
    config["browser"] = {"chromium_path": files["chromium"]}
    report = host.inspect_host(config, detailed=True)
    assert len(calls) == 2
    assert all(call[0] == files["node"] for call in calls)
    assert "require.resolve(process.argv[1])" in host._NODE_METADATA
    assert "require(process.argv[1])" not in host._NODE_METADATA
    assert report["browser"]["dependencies"]["status"] == "AVAILABLE"
    assert report["browser"]["playwright"]["import_verified"] is False
    assert report["browser"]["rendered_fixture"]["status"] == "NOT_CHECKED"
    assert report["status"] == "UNVERIFIED"
    assert report["actions"]["browser_launches"] == 0


@pytest.mark.parametrize("probe_result", [
    SimpleNamespace(returncode=1, stdout=b"failure"),
    SimpleNamespace(returncode=0, stdout=b"not-json"),
    SimpleNamespace(returncode=0, stdout=b"x" * 8193),
    SimpleNamespace(returncode=0, stdout=b'{"status":"PASS","reason":"ready"}'),
    SimpleNamespace(returncode=0, stdout=b'{"status":"AVAILABLE","reason":"module_entry_resolved","secret":"x"}'),
])
def test_failed_or_invalid_probe_is_unverified(monkeypatch, tmp_path, probe_result):
    executable_files(tmp_path, monkeypatch)
    monkeypatch.setattr(host, "_run_probe", lambda _: probe_result)
    report = host.inspect_host(detailed=True)
    assert report["browser"]["playwright"]["status"] == "UNVERIFIED"
    assert report["browser"]["node_version"]["status"] == "UNVERIFIED"


def test_timeout_never_becomes_dependency_presence(monkeypatch, tmp_path):
    executable_files(tmp_path, monkeypatch)
    def timeout(argv):
        raise subprocess.TimeoutExpired(argv, 3)
    monkeypatch.setattr(host, "_run_probe", timeout)
    report = host.inspect_host(detailed=True)
    assert report["browser"]["playwright"]["status"] == "UNVERIFIED"


def test_missing_node_skips_all_process_probes(monkeypatch):
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    monkeypatch.setattr(host, "_run_probe", lambda *args: pytest.fail("unexpected probe"))
    report = host.inspect_host(detailed=True)
    assert report["browser"]["node_version"]["status"] == "NOT_CHECKED"


def test_subprocess_environment_drops_preload_and_secrets(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require /untrusted.js")
    monkeypatch.setenv("NODE_PATH", "/untrusted")
    monkeypatch.setenv("PRIVATE_SERVICE_KEY", "private")
    def run(argv, **kwargs):
        assert argv == ["/trusted/node", "--version"]
        assert kwargs["shell"] is False and kwargs["timeout"] == 3
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not {"NODE_OPTIONS", "NODE_PATH", "PRIVATE_SERVICE_KEY"} & kwargs["env"].keys()
        return SimpleNamespace(returncode=0, stdout=b"v22.19.0")
    monkeypatch.setattr(host.subprocess, "run", run)
    assert host._run_probe(["/trusted/node", "--version"]).returncode == 0


def test_existing_nonexecutable_browser_file_is_missing(monkeypatch, tmp_path):
    files = executable_files(tmp_path, monkeypatch)
    from pathlib import Path
    Path(files["chromium"]).chmod(0o600)
    report = host.inspect_host({"browser": {"chromium_path": files["chromium"]}})
    assert report["browser"]["chromium"]["status"] == "MISSING"


def test_memory_exposes_limit_without_calling_it_available_ram(monkeypatch):
    monkeypatch.setattr(host.os, "sysconf", lambda key: 1024 if key == "SC_PHYS_PAGES" else 4096)
    monkeypatch.setattr(host, "_read_number", lambda path: 1048576)
    monkeypatch.setattr(host.sys, "platform", "linux")
    result = host._memory()
    assert result["physical_bytes"] == 4194304
    assert result["cgroup_root_limit_bytes"] == 1048576
    assert result["process_available_bytes"] is None
