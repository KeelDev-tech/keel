"""Pinned local review escalation with durable budgets and a sticky HTTP 429.

Recommendations have no submission/approval capability. Permissions are
trusted caller observations and must come from the existing authoritative
gate. The local server's weights and downstream behavior are not attested.
"""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

from keel_agent.models import HTTPResult, ReviewerConfig
from keel_eval import evaluation as _evaluation
from keel_eval.evaluation import (
    EvaluationError, _snapshot, _sha, _keys, _hash, _token, _config,
    run_local, validate_dataset,
)


class RoutingError(ValueError):
    """Content-free route validation or durable-state failure."""


def _fail(code):
    raise RoutingError(code)


def digest(value):
    try:
        return _sha(value)
    except EvaluationError as exc:
        raise RoutingError(str(exc)) from None


def _policy(value):
    try:
        value = _snapshot(value)
        _keys(value, ("schema", "policy_id", "source_sha256", "small_config", "strong_config",
                      "max_model_calls", "max_wall_seconds", "escalate_on"), "policy_schema_invalid")
        if value["schema"] != "keel.loki.route-policy.v1":
            _fail("policy_version_invalid")
        _token(value["policy_id"], "policy_id_invalid")
        _hash(value["source_sha256"], "source_sha256_invalid")
        for key in ("small_config", "strong_config"):
            if value[key] is not None:
                _config(value[key])
        if value["strong_config"] is not None and value["small_config"] is None:
            _fail("strong_requires_small_stage")
        if value["strong_config"] is not None:
            small, strong = dict(value["small_config"]), dict(value["strong_config"])
            small.pop("reviewer_id")
            strong.pop("reviewer_id")
            if small == strong:
                _fail("duplicate_effective_model_configuration")
        if type(value["max_model_calls"]) is not int or not 1 <= value["max_model_calls"] <= 128:
            _fail("model_call_limit_invalid")
        seconds = value["max_wall_seconds"]
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 3600:
            _fail("wall_limit_invalid")
        if value["escalate_on"] != ["ABSTAIN"]:
            _fail("only_abstention_escalation_allowed")
        return value
    except EvaluationError as exc:
        raise RoutingError(str(exc)) from None


def make_policy(*, policy_id, source_sha256, small_config=None, strong_config=None,
                max_model_calls=2, max_wall_seconds=180):
    """Freeze an operator route. 'Small/strong' are names, not proven capability."""
    def convert(config):
        return asdict(config) if type(config) is ReviewerConfig else config
    return _policy({"schema": "keel.loki.route-policy.v1", "policy_id": policy_id,
        "source_sha256": source_sha256, "small_config": convert(small_config),
        "strong_config": convert(strong_config), "max_model_calls": max_model_calls,
        "max_wall_seconds": max_wall_seconds, "escalate_on": ["ABSTAIN"]})


def _permissions(value):
    try:
        value = _snapshot(value)
        _keys(value, ("ai_allowed", "consent_for_model", "holds", "permission_revision_sha256"),
              "permissions_schema_invalid")
        if type(value["ai_allowed"]) is not bool or type(value["consent_for_model"]) is not bool:
            _fail("permission_boolean_required")
        holds = value["holds"]
        if type(holds) is not list or len(holds) > 64:
            _fail("holds_invalid")
        for hold in holds:
            _token(hold, "hold_invalid")
        if len(holds) != len(set(holds)):
            _fail("duplicate_hold")
        _hash(value["permission_revision_sha256"], "permission_revision_sha256_invalid")
        return value
    except EvaluationError as exc:
        raise RoutingError(str(exc)) from None


def _subject(value):
    try:
        # Placeholder gold never enters a request and is never reported as a
        # measured label. run_local is reused only for its existing transport,
        # rubric, response parsing, error allowlist and literal loopback policy.
        data = validate_dataset({"schema": "keel.eval.dataset.v1", "dataset_id": "route-shape",
            "synthetic": True, "split": "development", "label_source": "synthetic_fixture",
            "cases": [{"case_id": "route-case", "tags": ["routing"], "expected_verdict": "ABSTAIN",
                "label_rationale": "Placeholder used only to validate subject shape; not a gold label.",
                "subject": value}]})
        return data
    except EvaluationError as exc:
        raise RoutingError(str(exc)) from None


def _new_state(policy):
    return {"schema": "keel.loki.route-state.v1", "policy_sha256": digest(policy),
        "source_sha256": policy["source_sha256"], "calls_attempted": 0,
        "elapsed_seconds": 0.0, "rate_limited": False, "in_flight": False}


def _validate_state(value, policy):
    try:
        _keys(value, ("schema", "policy_sha256", "source_sha256", "calls_attempted",
                      "elapsed_seconds", "rate_limited", "in_flight"), "state_schema_invalid")
    except EvaluationError as exc:
        raise RoutingError(str(exc)) from None
    if value["schema"] != "keel.loki.route-state.v1":
        _fail("state_version_invalid")
    if value["policy_sha256"] != digest(policy) or value["source_sha256"] != policy["source_sha256"]:
        _fail("state_policy_mismatch")
    if (type(value["calls_attempted"]) is not int
            or not 0 <= value["calls_attempted"] <= policy["max_model_calls"]):
        _fail("state_calls_invalid")
    seconds = value["elapsed_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        _fail("state_elapsed_invalid")
    if type(value["rate_limited"]) is not bool or type(value["in_flight"]) is not bool:
        _fail("state_flags_invalid")
    if (value["rate_limited"] or value["in_flight"]) and value["calls_attempted"] == 0:
        _fail("state_flags_without_attempt")
    return value


@contextmanager
def _locked_state(path, policy):
    """Single canonical state path must be shared by every caller.

    File updates are fsynced under one nonblocking advisory lock. A crash can
    leave an in-flight reservation or invalid/truncated JSON; both stop future
    calls rather than silently resetting the budget. No automatic reset API.
    """
    try:
        path = Path(path)
        if not path.is_absolute() or path.name in ("", ".", "..") or ".." in path.parts:
            _fail("state_path_must_be_absolute")
        # Traverse every component through held descriptors: checking resolve()
        # first and then opening by name would leave an ancestor-symlink race.
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.parent.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
        except BaseException:
            os.close(directory)
            raise
    except (OSError, TypeError, ValueError):
        _fail("state_parent_unavailable")
    fd = None
    try:
        created = False
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        anchored = "/proc/self/fd/" + str(directory) + "/" + path.name
        try:
            fd = os.open(anchored, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(anchored, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            _fail("state_private_regular_file_required")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("state_busy")
        def save(value):
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                _fail("state_path_changed")
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    _fail("state_write_failed")
                offset += written
            os.ftruncate(fd, len(raw))
            os.fsync(fd)
            os.fsync(directory)
        if created:
            value = _new_state(policy)
            save(value)
        else:
            if info.st_size > 8192:
                _fail("state_size_invalid")
            raw = os.read(fd, 8193)
            def unique(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        _fail("duplicate_state_key")
                    result[key] = item
                return result
            try:
                value = json.loads(raw, object_pairs_hook=unique,
                                   parse_constant=lambda x: _fail("nonfinite_state"))
            except (ValueError, UnicodeError):
                _fail("state_json_invalid")
            value = _validate_state(value, policy)
        yield value, save
    except OSError:
        _fail("state_io_unavailable")
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def run_route(subject, policy, *, expected_policy_sha256, source_sha256, permissions,
              state_path, allow_model_calls=False, transport=None):
    """Run frozen deterministic/no-model/small/strong branches once.

    Only valid ABSTAIN can escalate. Errors and HTTP 429 never trigger retries.
    The transport is trusted injected test code when supplied, never labelled
    real inference. Permission denials/holds dominate all model outputs.
    """
    policy, permissions, dataset = _policy(policy), _permissions(permissions), _subject(subject)
    try:
        _hash(expected_policy_sha256, "expected_policy_sha256_invalid")
        _hash(source_sha256, "source_sha256_invalid")
    except EvaluationError as exc:
        raise RoutingError(str(exc)) from None
    if digest(policy) != expected_policy_sha256:
        _fail("policy_pin_mismatch")
    if policy["source_sha256"] != source_sha256:
        _fail("source_changed")
    if type(allow_model_calls) is not bool:
        _fail("model_call_opt_in_invalid")
    if transport is not None and not callable(transport):
        _fail("transport_not_callable")
    records, calls, started = [], 0, time.monotonic()
    subject = dataset["cases"][0]["subject"]
    report = {"schema": "keel.loki.route-report.v1", "policy_sha256": digest(policy),
        "source_sha256": source_sha256, "subject_sha256": digest(subject),
        "permission_revision_sha256": permissions["permission_revision_sha256"],
        "mode": "NO_MODEL", "recommendation": "ABSTAIN", "stop_reason": None,
        "records": records, "model_calls_attempted": 0, "session_state": None,
        "model_quality_validated": False, "permissions_authenticated": False,
        "execution_authorized": False, "production_deployed": False}
    def stage(name, outcome, reason, **extra):
        records.append(dict(stage=name, outcome=outcome, reason=reason, **extra))
    def done(outcome, reason, state=None):
        report.update(recommendation=outcome, stop_reason=reason, model_calls_attempted=calls,
            mode=("NO_MODEL" if not calls else "INJECTED" if transport is not None else "LOCAL"),
            session_state=_snapshot(state) if state is not None else None,
            wall_ms=round(max(0.0, time.monotonic() - started) * 1000, 6))
        return report
    if permissions["holds"] or not permissions["ai_allowed"] or not permissions["consent_for_model"]:
        reason = ("existing_holds" if permissions["holds"] else
                  "no_ai_permission" if not permissions["ai_allowed"] else "model_consent_absent")
        stage("deterministic", "BLOCKED", reason)
        return done("BLOCKED", reason)
    stage("deterministic", "CHECKED", "permission_and_subject_shape_checked")
    if not subject["evidence"]:
        stage("no_model", "ABSTAIN", "source_evidence_missing")
        return done("ABSTAIN", "source_evidence_missing")
    if not allow_model_calls or policy["small_config"] is None:
        reason = "local_model_not_enabled" if not allow_model_calls else "no_local_model_configured"
        stage("no_model", "ABSTAIN", reason)
        return done("ABSTAIN", reason)
    with _locked_state(state_path, policy) as (state, save):
        base_elapsed = state["elapsed_seconds"]
        def remaining():
            return policy["max_wall_seconds"] - base_elapsed - max(0.0, time.monotonic() - started)
        for name, key in (("small_local", "small_config"), ("strong_local", "strong_config")):
            if policy[key] is None:
                return done("ABSTAIN", "no_further_local_stage", state)
            reason = None
            config = _config(policy[key])
            if state["rate_limited"]:
                reason = "not_run_after_rate_limit"
            elif state["in_flight"]:
                reason = "unknown_model_attempt"
            elif state["calls_attempted"] >= policy["max_model_calls"]:
                reason = "model_call_budget_exhausted"
            elif remaining() < config.timeout_seconds:
                reason = "wall_budget_insufficient_for_deadline"
            if reason:
                stage(name, "NOT_RUN", reason)
                return done("ERROR", reason, state)
            attempted, preflight_error = 0, None
            def send(selected, payload, timeout):
                nonlocal attempted, calls, preflight_error
                if remaining() < timeout:
                    preflight_error = "wall_budget_insufficient_for_deadline"
                    raise RuntimeError("route_deadline_preflight")
                state["calls_attempted"] += 1
                state["in_flight"] = True
                # A crash retains the reservation and unknown-attempt hold.
                state["elapsed_seconds"] = base_elapsed + max(0.0, time.monotonic() - started) + timeout
                save(state)
                attempted += 1
                calls += 1
                sender = _evaluation.loopback_transport if transport is None else transport
                response = sender(selected, payload, timeout)
                # Persist the 429 before response parsing or subsequent stages.
                if isinstance(response, HTTPResult) and type(response.status) is int and response.status == 429:
                    state["rate_limited"] = True
                    save(state)
                return response
            result = run_local(dataset, config, transport=send)["cases"][0]
            # Adapter errors after a reserved request remain unknown across
            # future routes; a parsed response or known HTTP error is resolved.
            error = preflight_error or result.get("error_code")
            unresolved = attempted and error in ("model_adapter_error", "model_transport_unavailable",
                                                  "model_deadline_exceeded")
            if attempted:
                state["in_flight"] = bool(unresolved)
                state["elapsed_seconds"] = base_elapsed + max(0.0, time.monotonic() - started)
                if error == "model_http_429":
                    state["rate_limited"] = True
                save(state)
            observed = "ERROR" if error else result["observed_verdict"]
            stage(name, observed, error, model_calls_attempted=attempted,
                assessment_sha256=result.get("assessment_sha256") if not preflight_error else None,
                request_sha256=result.get("request_sha256") if not preflight_error else None,
                response_sha256=result.get("response_sha256") if not preflight_error else None,
                latency_ms=result.get("latency_ms") if not preflight_error else None)
            if observed != "ABSTAIN":
                return done(observed, error, state)
        return done("ABSTAIN", "all_local_stages_abstained", state)


def demo():
    """Synthetic two-stage and durable-429 rehearsal; never real inference."""
    source = "a" * 64
    configs = [ReviewerConfig(name, "ollama", "http://127.0.0.1:11434/api/chat",
                              "fixture-" + name, timeout_seconds=.05) for name in ("small", "strong")]
    policy = make_policy(policy_id="demo", source_sha256=source,
                         small_config=configs[0], strong_config=configs[1])
    subject = {"required_claim_ids": ["claim"], "claims": [{"claim_id": "claim", "text": "Fixture fact."}],
               "evidence": [{"evidence_id": "source", "text": "Fixture record."}]}
    permissions = {"ai_allowed": True, "consent_for_model": True, "holds": [],
                   "permission_revision_sha256": "b" * 64}
    def response(config, payload, timeout):
        verdict = "ABSTAIN" if config.reviewer_id == "small" else "PASS"
        assessment = {"verdict": verdict, "covered_claim_ids": ["claim"],
                      "findings": ["Synthetic escalation."] if verdict == "ABSTAIN" else []}
        return HTTPResult(200, {"content-type": "application/json"}, json.dumps({"model": config.model,
            "done": True, "created_at": "fixture", "message": {"role": "assistant",
                "content": json.dumps(assessment)}}).encode())
    kwargs = dict(expected_policy_sha256=digest(policy), source_sha256=source, permissions=permissions)
    with tempfile.TemporaryDirectory(prefix="keel-loki-route-") as home:
        routed = run_route(subject, policy, state_path=str(Path(home) / "route.json"),
                           allow_model_calls=True, transport=response, **kwargs)
        stopped = run_route(subject, policy, state_path=str(Path(home) / "429.json"),
            allow_model_calls=True, transport=lambda *args: HTTPResult(429, {}, b""), **kwargs)
        resumed = run_route(subject, policy, state_path=str(Path(home) / "429.json"),
            allow_model_calls=True, transport=response, **kwargs)
    return {"escalation": routed, "rate_limit": stopped, "after_rate_limit": resumed,
            "synthetic": True, "real_model_calls": 0, "execution_authorized": False}
