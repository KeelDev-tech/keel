"""Read-only host observations; availability never authorizes execution.

The default path runs no subprocesses. The opt-in detailed path runs two fixed
Node commands with no shell, preload options, package manager, model call, or
browser launch. It resolves Playwright's entry point without importing it.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from keel_agent.models import ReviewerConfig


_NODE_METADATA = r"""const fs=require('node:fs'),path=require('node:path');
let out={status:'UNVERIFIED',reason:'module_resolution_failed'};
try {
  const entry=require.resolve(process.argv[1]);
  let dir=path.dirname(entry), version=null, name=null;
  for(let i=0;i<8;i++) {
    const file=path.join(dir,'package.json');
    if(fs.existsSync(file) && fs.statSync(file).isFile() && fs.statSync(file).size<65536) {
      const pkg=JSON.parse(fs.readFileSync(file,'utf8'));
      if(['playwright','playwright-core'].includes(pkg.name)) {
        name=pkg.name; version=String(pkg.version).slice(0,80); break;
      }
    }
    const parent=path.dirname(dir); if(parent===dir) break; dir=parent;
  }
  out={status:name?'AVAILABLE':'UNVERIFIED',reason:name?'module_entry_resolved':'package_identity_unverified',
       entry:entry.slice(0,4096),package_name:name,version};
} catch(e) { if(e.code==='MODULE_NOT_FOUND') out={status:'MISSING',reason:'module_not_resolved'}; }
process.stdout.write(JSON.stringify(out));"""


def _status(state, reason, **fields):
    return {"status": state, "reason": reason, **fields}


def _read_number(path):
    """Bounded read of a kernel resource counter, never arbitrary config paths."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(64).strip()
        value = int(raw)
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def _memory():
    try:
        total = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        physical = total if total > 0 else None
    except (AttributeError, OSError, ValueError, TypeError):
        physical = None
    # A host's physical RAM is not necessarily available to this process.
    limit = None
    if sys.platform.startswith("linux"):
        limits = [_read_number("/sys/fs/cgroup/memory.max"),
                  _read_number("/sys/fs/cgroup/memory/memory.limit_in_bytes")]
        limits = [n for n in limits if n is not None and n < (1 << 60)]
        if limits:
            limit = min(limits)
    return _status("AVAILABLE" if physical else "NOT_CHECKED",
                   "kernel_counters" if physical else "portable_counter_unavailable",
                   physical_bytes=physical, cgroup_root_limit_bytes=limit,
                   process_available_bytes=None,
                   model_fit_verified=False)


def _hardware():
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    try:
        disk = shutil.disk_usage(Path.cwd())
        disk_result = _status("AVAILABLE", "current_working_filesystem",
                              total_bytes=disk.total, free_bytes=disk.free)
    except OSError:
        disk_result = _status("NOT_CHECKED", "filesystem_counter_unavailable")
    uname = os.uname() if hasattr(os, "uname") else None
    return {
        "python": _status("AVAILABLE", "running_interpreter", version=sys.version.split()[0]),
        "os": {"platform": sys.platform, "name": os.name,
               "release": uname.release if uname else None,
               "machine": uname.machine if uname else None},
        "cpu": {"logical_count": os.cpu_count(), "affinity_count": affinity,
                "quota_verified": False},
        "memory": _memory(), "disk": disk_result,
        "gpu": _status("NOT_CHECKED", "no_driver_or_device_probe"),
    }


def _executable(name, configured=None):
    path = configured or shutil.which(name)
    if path is None:
        return _status("MISSING", "executable_not_found_on_path", path=None)
    try:
        valid = Path(path).is_file() and os.access(path, os.X_OK)
    except (OSError, ValueError):
        valid = False
    return _status("AVAILABLE" if valid else "MISSING",
                   "executable_file_present" if valid else "executable_file_unavailable",
                   path=str(path), binary_identity_verified=False)


def _run_probe(argv):
    # NODE_OPTIONS can inject code even into `node --version`. Do not inherit it,
    # NODE_PATH, proxy settings, or unrelated service secrets.
    environment = {key: os.environ[key] for key in
                   ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SystemRoot")
                   if key in os.environ}
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=environment, shell=False,
                          timeout=3, check=False)


def _detailed_node(node, module):
    if node["status"] != "AVAILABLE":
        return (_status("NOT_CHECKED", "node_unavailable"),
                _status("NOT_CHECKED", "node_unavailable"))
    version = _status("UNVERIFIED", "node_version_probe_failed")
    package = _status("UNVERIFIED", "playwright_resolution_probe_failed")
    try:
        result = _run_probe([node["path"], "--version"])
        if result.returncode == 0 and len(result.stdout) <= 80:
            value = result.stdout.decode("ascii").strip()
            if re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value):
                version = _status("AVAILABLE", "node_version_reported", version=value)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    try:
        result = _run_probe([node["path"], "-e", _NODE_METADATA, module])
        if result.returncode == 0 and len(result.stdout) <= 8192:
            value = json.loads(result.stdout)
            if (type(value) is dict and value.get("status") in {"AVAILABLE", "MISSING", "UNVERIFIED"}
                    and value.get("reason") in {"module_entry_resolved", "module_not_resolved",
                                                 "module_resolution_failed", "package_identity_unverified"}
                    and set(value) <= {"status", "reason", "entry", "package_name", "version"}):
                package = value
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        pass
    return version, {**package, "import_verified": False}


def _configuration(config):
    if config is None:
        return _status("MISSING", "reviewer_configuration_not_supplied", reviewers=[]), {}
    if type(config) is not dict or set(config) - {"reviewers", "browser"}:
        raise ValueError("host configuration accepts only reviewers and browser")
    browser = config.get("browser", {})
    if type(browser) is not dict or set(browser) - {"node_path", "playwright_module", "chromium_path"}:
        raise ValueError("unsupported browser configuration")
    for key, value in browser.items():
        if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
            raise ValueError("browser settings must be bounded single-line strings")
        if key != "playwright_module" and not Path(value).is_absolute():
            raise ValueError("configured executable paths must be absolute")
        if key == "playwright_module" and value != "playwright" and not Path(value).is_absolute():
            raise ValueError("playwright_module must be playwright or an absolute installed path")
    rows = config.get("reviewers")
    if rows is None:
        return _status("MISSING", "reviewer_configuration_not_supplied", reviewers=[]), browser
    if type(rows) is not list or not 2 <= len(rows) <= 16 or any(type(row) is not dict for row in rows):
        return _status("UNVERIFIED", "reviewer_roster_invalid", reviewers=[]), browser
    try:
        configs = [ReviewerConfig(**row) for row in rows]
        if len({cfg.reviewer_id for cfg in configs}) != len(configs):
            raise ValueError("duplicate reviewer")
    except (ValueError, TypeError):
        return _status("UNVERIFIED", "reviewer_roster_invalid", reviewers=[]), browser
    return _status("AVAILABLE", "loopback_configuration_validated",
                   reviewers=[{"reviewer_id": row.reviewer_id, "backend": row.backend,
                               "endpoint": row.endpoint, "configured_model": row.model,
                               "weights_status": "NOT_CHECKED"} for row in configs],
                   diversity_verified=False), browser


def inspect_host(config=None, *, detailed=False):
    """Observe this host without installing, downloading, or contacting servers.

    Config: optional reviewers (the existing Keel ReviewerConfig dictionaries)
    and browser {node_path, playwright_module, chromium_path}. Details opt in to
    fixed Node probes only. Playwright and browser code are never imported/run.
    Invalid reviewer entries yield UNVERIFIED without echoing their contents.
    Invalid configuration shape raises ValueError before any probe.
    """
    if type(detailed) is not bool:
        raise ValueError("detailed must be a boolean")
    model_config, browser_config = _configuration(config)
    node = _executable("node", browser_config.get("node_path"))
    executables = {"node": node, "ollama": _executable("ollama"),
                   "llama_server": _executable("llama-server")}
    chromium = (_executable("chromium", browser_config["chromium_path"])
                if "chromium_path" in browser_config else
                _status("NOT_CHECKED", "chromium_path_not_supplied"))
    node_version, playwright = (_detailed_node(node, browser_config.get("playwright_module", "playwright"))
                                if detailed else
                                (_status("NOT_CHECKED", "detailed_probe_not_requested"),
                                 _status("NOT_CHECKED", "detailed_probe_not_requested")))
    dependency_states = [node["status"], playwright["status"], chromium["status"]]
    dependencies = ("MISSING" if "MISSING" in dependency_states else
                    "AVAILABLE" if all(state == "AVAILABLE" for state in dependency_states) else
                    "UNVERIFIED")
    return {
        "schema": "keel.host_readiness.v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "UNVERIFIED", "execution_authorized": False,
        "hardware": _hardware(), "executables": executables,
        "models": {"configuration": model_config,
                   "server_reachability": _status("NOT_CHECKED", "no_endpoint_calls"),
                   "inference": _status("NOT_CHECKED", "real_inference_required"),
                   "weights_license": _status("NOT_CHECKED", "operator_must_verify_selected_weights"),
                   "server_cloud_disabled": _status("UNVERIFIED", "server_process_not_inspected"),
                   "client_ollama_no_cloud_set": os.environ.get("OLLAMA_NO_CLOUD") == "1"},
        "browser": {"dependencies": _status(dependencies, "file_and_resolution_observations_only"),
                    "node_version": node_version, "playwright": playwright, "chromium": chromium,
                    "rendered_fixture": _status("NOT_CHECKED", "fixture_rehearsal_required")},
        "actions": {"subprocess_probes_requested": detailed, "http_calls": 0,
                    "model_calls": 0, "browser_launches": 0, "downloads": 0,
                    "installs": 0, "canonical_writes": 0},
    }
