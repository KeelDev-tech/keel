"""Synthetic source-to-rendered-field rehearsal; never submits an application.

Filesystem discovery is deliberately separate from a runtime observation. A
dependency being present is not evidence that a browser was launched. Injected
adapters can exercise orchestration, but can never produce a rendered PASS.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading

from keel_agent.browser import BrowserAdapter, BrowserError, WORKER, contract_hash
from keel_agent.io import private_home
from keel_agent.models import ReviewerConfig
from keel_agent.revisions import _directory_fd
from keel_eval import run_local
from keel_grounding.answers import resolve_grounded_answer
from keel_grounding.claims import verify_grounding
from keel_grounding.demo import make_fixture
from keel_grounding.evidence import GroundingError, read_verified_file
from keel_grounding.packet import verify_packet
from keel_trust.common import canonical, digest, strict_json
from tools.local_application_fixture import create_fixture, fixture_contract


_REAL_ADAPTER = BrowserAdapter
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _path_value(value):
    return (type(value) is str and 0 < len(value) <= 4096
            and not any(ord(c) < 32 for c in value))


def _regular(path, *, executable=False):
    try:
        candidate = Path(path)
        return candidate.is_file() and (not executable or os.access(candidate, os.X_OK))
    except (OSError, ValueError, TypeError):
        return False


def _playwright_observation():
    configured = os.environ.get("KEEL_PLAYWRIGHT_MODULE")
    module = configured or "playwright"
    result = {"configured": configured is not None, "status": "UNKNOWN",
              "runnability_verified": False}
    if not _path_value(module):
        return {**result, "status": "INVALID_CONFIGURATION"}
    # The reviewed worker uses createRequire(import.meta.url), not NODE_PATH.
    # Probe only the bounded Node ancestor locations; do not execute the module.
    if module.startswith("/"):
        candidates = [Path(module)]
    elif re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", module):
        candidates = [parent / "node_modules" / module for parent in WORKER.parents]
    else:
        # Relative module names and unusual loader behavior need a real run.
        return result
    try:
        found = any(_regular(path) or _regular(path / "package.json") for path in candidates)
    except (OSError, ValueError):
        return result
    return {**result, "status": "FOUND" if found else "NOT_FOUND"}


def inspect_host():
    """Observe dependency files only; no sockets, subprocesses or environment dump.

    Only the two documented browser configuration variables are inspected.
    Their values are never copied into the report. Chromium's default download
    location is owned by Playwright; absence is established by an actual launch,
    not guessed from an incomplete cache search.
    """
    node = shutil.which("node")
    chromium = os.environ.get("KEEL_CHROMIUM_EXECUTABLE")
    chromium_status = "UNKNOWN"
    if chromium is not None:
        chromium_status = ("FOUND" if _path_value(chromium)
                           and _regular(chromium, executable=True) else "NOT_FOUND")
    memory_bytes = None
    try:
        page_size, pages = os.sysconf("SC_PAGE_SIZE"), os.sysconf("SC_PHYS_PAGES")
        if type(page_size) is int and type(pages) is int and page_size > 0 and pages > 0:
            memory_bytes = page_size * pages
    except (AttributeError, OSError, ValueError):
        pass
    return {"schema": "keel.bench.host-inspection.v1", "inspection": "FILESYSTEM_ONLY",
            "python": {"version": ".".join(map(str, sys.version_info[:3])), "running": True},
            "node": {"status": "FOUND" if node and _regular(node, executable=True) else "NOT_FOUND",
                     "runnability_verified": False},
            "playwright": _playwright_observation(),
            "chromium": {"status": chromium_status, "configured": chromium is not None,
                         "runnability_verified": False},
            "worker": {"status": "FOUND" if _regular(WORKER) else "NOT_FOUND"},
            "hardware_observation": {"logical_cpu_count": os.cpu_count(),
                                     "physical_memory_bytes": memory_bytes,
                                     "basis": "OS_REPORTED_NOT_RUNTIME_ATTESTED",
                                     "container_limits_measured": False,
                                     "energy_cost": "NOT_MEASURED"},
            "network_calls": 0, "subprocess_calls": 0,
            "model_inference": "NOT_RUN", "rendered_browser": "NOT_RUN"}


def _new_home(value):
    try:
        raw = os.fspath(value)
        if (not _path_value(raw) or "\\" in raw or ".." in raw.split("/")
                or "." in raw.split("/")):
            raise ValueError("new_private_home_required")
        home = Path(raw).absolute()
        if not home.name:
            raise ValueError("new_private_home_required")
        # Descriptor traversal rejects a symlink in *any* parent component.
        # The parent must exist. mkdir is exclusive; existing homes are never
        # reused, repaired, chmod'ed, overwritten or deleted.
        parent_fd = _directory_fd(home.parent)
        try:
            # Keep the creation anchored to the held descriptor while exposing
            # its actual destination to path-based audit hooks. Python's mkdir
            # audit event otherwise provides only the basename, which a hook
            # may resolve relative to cwd rather than the supplied dir_fd.
            # Linux procfs is already required by the additive installer; if
            # unavailable, fail closed instead of falling back to a racy path.
            anchored = Path("/proc/self/fd") / str(parent_fd) / home.name
            os.mkdir(anchored, mode=0o700)
            created = os.stat(home.name, dir_fd=parent_fd, follow_symlinks=False)
            checked = private_home(home)
            current = checked.stat()
            if (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("new_private_home_required")
        finally:
            os.close(parent_fd)
        return home, (created.st_dev, created.st_ino)
    except (OSError, TypeError, ValueError):
        raise ValueError("new_private_home_required") from None


def _same_home(home, identity):
    try:
        current = home.lstat()
        return (stat.S_ISDIR(current.st_mode) and stat.S_IMODE(current.st_mode) == 0o700
                and (current.st_dev, current.st_ino) == identity
                and home.resolve(strict=True) == home)
    except OSError:
        return False


def _check(status, reason=None):
    result = {"status": status}
    if reason is not None:
        result["reason"] = reason
    return result


def _unavailable(inspection):
    for key in ("node", "playwright", "chromium", "worker"):
        if inspection[key]["status"] in {"NOT_FOUND", "INVALID_CONFIGURATION"}:
            return key + "_unavailable"
    return None


def _readback_valid(output, contract, packet_bytes):
    """Validate even a test double; no caller-provided PASS is accepted."""
    if type(output) is not dict:
        return False
    expected = {field["label"]: field["value"] for field in contract["fields"]}
    expected["attachment"] = {"name": "answer.txt", "size": len(packet_bytes),
                              "sha256": hashlib.sha256(packet_bytes).hexdigest()}
    return (output.get("status") == "PREPARED"
            and output.get("bundle_hash") == contract_hash(contract)
            and output.get("account_id") == contract["account_id"]
            and output.get("origin") == contract["origin"]
            and type(output.get("form_fingerprint")) is str
            and _HASH.fullmatch(output["form_fingerprint"]) is not None
            and canonical(output.get("readback")) == canonical(expected)
            and output.get("submitted") is False
            and output.get("execution_authorized") is False
            and output.get("local_fixture_submitted", False) is False
            and "receipt" not in output)


def _model_trial(value, source_value, config, transport):
    """A single supported synthetic claim: smoke test, never quality benchmark."""
    dataset = {"schema": "keel.eval.dataset.v1", "dataset_id": "synthetic-host-grounding",
               "synthetic": True, "split": "development", "label_source": "synthetic_fixture",
               "cases": [{"case_id": "synthetic-grounded-answer", "tags": ["host-smoke", "supported"],
                          "expected_verdict": "PASS",
                          "label_rationale": "The synthetic claim repeats the exact verified source value.",
                          "subject": {"required_claim_ids": ["synthetic-claim"],
                                      "claims": [{"claim_id": "synthetic-claim", "text": value}],
                                      "evidence": [{"evidence_id": "synthetic-verified-file",
                                                    "text": source_value}]}}]}
    evaluated = run_local(dataset, config, transport=transport)
    row = evaluated["cases"][0]
    passed = row["observed_verdict"] == "PASS"
    return {"status": "SIMULATED" if transport is not None else "PASS" if passed else "BLOCKED",
            "observed_verdict": row["observed_verdict"],
            "permits_fixture_preparation": passed, "error_code": row["error_code"],
            "mode": evaluated["mode"], "model_inference": evaluated["model_inference"],
            "transport_calls_attempted": evaluated["transport_calls_attempted"],
            "model_calls_attempted": evaluated["model_calls_attempted"],
            "dataset_sha256": evaluated["dataset_sha256"],
            "model_config_sha256": evaluated["model_config_sha256"],
            "request_sha256": row.get("request_sha256"), "response_sha256": row.get("response_sha256"),
            "assessment_sha256": row["assessment_sha256"], "latency_ms": row["latency_ms"],
            "model_weights_attested": False, "model_quality_validated": False,
            "quality_release_gate_satisfied": False,
            "boundary": "One synthetic identity-support smoke check; no held-out quality, "
                        "model weights, server egress or approval authority is established."}


def _browser_trial(answer, packet_bytes, *, adapter, inspection):
    injected = adapter is not None
    if not injected:
        reason = _unavailable(inspection)
        if reason:
            return {"status": "UNAVAILABLE", "reason": reason, "fixture_server_started": False}
        adapter = _REAL_ADAPTER()
    server = None
    thread = None
    started = False
    try:
        server, state = create_fixture(port=0)
        if (server.server_address[0] != "127.0.0.1" or not 0 < server.server_port < 65536
                or not _HASH.fullmatch(state.nonce)):
            raise ValueError("fixture_identity_invalid")
        contract = fixture_contract(server, state)
        # Only one field is in this approved artifact. Other required fields
        # deliberately stay empty. The fixture is PREPARED, never submitted.
        contract["fields"] = [{"label": "Motivation", "kind": "text", "value": answer["value"]}]
        contract["attachment"] = {"label": "Résumé", "name": "answer.txt", "mime_type": "text/plain",
                                  "base64": base64.b64encode(packet_bytes).decode("ascii")}
        contract["operation"] = "prepare"
        expected_contract_hash = contract_hash(contract)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = True
        output = adapter.prepare(contract)
        if (contract_hash(contract) != expected_contract_hash
                or not _readback_valid(output, contract, packet_bytes) or state.receipts):
            return {"status": "FAIL", "reason": "rendered_readback_mismatch",
                    "fixture_server_started": started, "injected_adapter": injected}
        return {"status": "SIMULATED" if injected else "PASS",
                "reason": "injected_adapter_not_rendered" if injected else "partial_form_readback_verified",
                "fixture_server_started": started, "injected_adapter": injected,
                "bundle_sha256": expected_contract_hash,
                "form_fingerprint": output["form_fingerprint"],
                "readback_sha256": digest(output["readback"]),
                "fixture_receipts": 0}
    except BrowserError as exc:
        # The reviewed worker has only two unambiguous dependency error codes.
        # Mapping failures, malformed readback and timeouts remain FAIL rather
        # than being relabelled as an innocent missing dependency.
        missing = ("Node.js is not installed", "Chromium executable is not installed",
                   "configured Chromium executable is unavailable")
        unavailable = not injected and any(str(exc).startswith(prefix) for prefix in missing)
        return {"status": "UNAVAILABLE" if unavailable else "FAIL",
                "reason": "browser_dependency_unavailable" if unavailable else "browser_operation_unverified",
                "fixture_server_started": started, "injected_adapter": injected}
    except OSError:
        return {"status": "FAIL", "reason": "local_fixture_unavailable",
                "fixture_server_started": started, "injected_adapter": injected}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"status": "FAIL", "reason": "browser_contract_unverified",
                "fixture_server_started": started, "injected_adapter": injected}
    finally:
        if server is not None:
            if started:
                server.shutdown()
            server.server_close()
        if thread is not None and started:
            thread.join(timeout=5)


def run_host_trial(home, *, render_browser=False, adapter=None, reviewer_config=None,
                   allow_model_calls=False, transport=None):
    """Create an isolated synthetic trial in a new private directory.

    By default no server or browser is started. ``render_browser=True`` invokes
    the existing bounded worker against an ephemeral 127.0.0.1 fixture only.
    Any injected adapter is labelled SIMULATED, even a BrowserAdapter instance.
    A configured reviewer requires explicit ``allow_model_calls=True``. It
    judges the verified source value before any browser preparation; FAIL,
    ABSTAIN and ERROR prevent preparation. Injected transport stays SIMULATED.
    This tests a single grounded field and its exact packet attachment. It is
    not a full form, human approval, live integration or model quality eval.
    """
    if type(render_browser) is not bool or (adapter is not None and not render_browser):
        raise ValueError("explicit_browser_trial_required")
    if (type(allow_model_calls) is not bool
            or (reviewer_config is not None and (not allow_model_calls or type(reviewer_config) is not ReviewerConfig))
            or (allow_model_calls and reviewer_config is None)
            or (transport is not None and (reviewer_config is None or not callable(transport)))):
        raise ValueError("explicit_local_model_configuration_required")
    home, identity = _new_home(home)
    report = {"schema": "keel.bench.host-trial.v1", "synthetic": True, "status": "PARTIAL",
              "trial_scope": "PARTIAL_FORM_READBACK", "full_application_trial": False,
              "whole_form_prepared": False, "planned_field_labels": ["Motivation"],
              "prepared_field_labels": [], "simulated_field_labels": [],
              "unprepared_field_labels": ["Full name", "Email", "Motivation", "Work authorization", "Confirm accuracy"],
              "fixture_clock": "2026-09-19T12:00:00+00:00", "checks": {},
              "browser": {"status": "NOT_RUN", "fixture_server_started": False},
              "model": {"status": "NOT_RUN", "model_inference": "NOT_RUN",
                        "model_calls_attempted": 0, "transport_calls_attempted": 0},
              "execution_authorized": False,
              "canonical_writes": 0, "external_submission": False, "local_submission": False,
              "truth_independently_verified": False, "human_approval_verified": False,
              "live_integration": "NOT_RUN", "source_kind": "SYNTHETIC_FIXTURE",
              "boundary": "Exact synthetic field and packet readback only; no full-form success, "
                          "human decision, factual truth, model quality or submission is established."}
    checks = report["checks"]
    for name in ("evidence", "scoped_answer", "packet", "answer_packet_binding", "rendered_readback"):
        checks[name] = _check("NOT_RUN")
    phase = "evidence"
    try:
        if not _same_home(home, identity):
            raise ValueError("trial_home_changed")
        case = make_fixture(home)
        grounded = verify_grounding(case["document"], case["bindings"], root=home / "evidence", now=case["now"])
        checks[phase] = _check("PASS" if grounded["status"] == "VERIFIED" else "FAIL")
        if checks[phase]["status"] != "PASS":
            raise ValueError("grounding_blocked")
        report["trust_snapshot_sha256"] = digest(case["document"])
        phase = "scoped_answer"
        answer = resolve_grounded_answer(case["records"], case["context"], case["answer_bindings"],
            case["document"], case["bindings"], root=home / "evidence", now=case["now"])
        checks[phase] = _check("PASS" if answer["status"] == "RESOLVED" else "FAIL")
        if checks[phase]["status"] != "PASS":
            raise ValueError("answer_blocked")
        phase = "packet"
        packet_sha256 = digest(case["packet"])
        packet = verify_packet(case["document"], case["bindings"], case["packet"],
            evidence_root=home / "evidence", packet_root=home / "packet", now=case["now"],
            expected_packet_sha256=packet_sha256)
        checks[phase] = _check("PASS" if packet["status"] == "VERIFIED" else "FAIL")
        if checks[phase]["status"] != "PASS":
            raise ValueError("packet_blocked")
        report["packet_manifest_sha256"] = packet_sha256
        phase = "answer_packet_binding"
        artifact = case["packet"]["artifacts"][0]
        packet_bytes = read_verified_file(home / "packet", artifact["path"], artifact["sha256"])
        if (type(answer["value"]) is not str or packet_bytes != (answer["value"] + "\n").encode("utf-8")
                or not _same_home(home, identity)):
            raise ValueError("answer_packet_mismatch")
        checks[phase] = _check("PASS")
        report["answer_value_sha256"] = digest(answer["value"])
        report["packet_bytes_sha256"] = hashlib.sha256(packet_bytes).hexdigest()
        model_permits = True
        if reviewer_config is not None:
            phase = "model_review"
            source = case["document"]["sources"][0]
            source_raw = read_verified_file(home / "evidence", case["bindings"]["sources"][0]["path"],
                                            source["content_hash"])
            source_value = strict_json(source_raw)["value"]
            if type(source_value) is not str or source_value != answer["value"]:
                raise ValueError("model_source_value_mismatch")
            report["model"] = _model_trial(answer["value"], source_value, reviewer_config, transport)
            model_permits = report["model"]["permits_fixture_preparation"]
            checks[phase] = _check(report["model"]["status"] if model_permits else "BLOCKED")
        if render_browser and model_permits:
            phase = "rendered_readback"
            report["browser"] = _browser_trial(answer, packet_bytes, adapter=adapter, inspection=inspect_host())
            checks[phase] = _check(report["browser"]["status"], report["browser"].get("reason"))
        elif render_browser:
            report["browser"]["reason"] = "model_review_blocked"
            checks["rendered_readback"] = _check("NOT_RUN", "model_review_blocked")
        if not _same_home(home, identity):
            raise ValueError("trial_home_changed")
    except (GroundingError, OSError, ValueError, TypeError, KeyError, AttributeError):
        checks[phase] = _check("FAIL", "host_trial_unverified")
    if any(check["status"] in {"FAIL", "BLOCKED"} for check in checks.values()):
        report["status"] = "FAIL"
    elif all(check["status"] == "PASS" for check in checks.values()):
        report["status"] = "PASS"
    report["rendered_browser_verified"] = report["browser"]["status"] == "PASS"
    if report["rendered_browser_verified"]:
        report["prepared_field_labels"] = ["Motivation"]
        report["unprepared_field_labels"].remove("Motivation")
    elif report["browser"]["status"] == "SIMULATED":
        report["simulated_field_labels"] = ["Motivation"]
    report["model_to_rendered_slice_verified"] = (report["model"]["status"] == "PASS"
                                                 and report["rendered_browser_verified"]
                                                 and report["status"] == "PASS")
    report["blockers"] = (["rendered_browser_not_verified"] if not report["rendered_browser_verified"] else [])
    # These are limitations even when the deliberately narrow trial passes.
    report["remaining_validation"] = ["full_form_and_attestations", "authentic_records_and_human_review",
                                       "real_model_quality", "live_host_integration"]
    return report
