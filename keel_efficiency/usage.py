"""Strict local-backend usage accounting without token/credit conversion.

Backend counts are assertions by the configured server, not billing attestations.
Missing counts remain unknown; cached/reasoning tokens are subsets of totals.
"""
import json
import math


_MAX_COUNT = (1 << 53) - 1


def unknown_usage(reason="usage_missing"):
    return {"status": "UNKNOWN", "provenance": "unknown",
            "input_tokens": None, "output_tokens": None,
            "cached_input_tokens": None, "reasoning_output_tokens": None,
            "backend_duration_ns": None, "errors": [reason]}


def parse_usage(backend, envelope):
    """Read Ollama or llama.cpp non-streaming counts without coercion.

    A malformed reported field invalidates all usage for that response. This
    deliberately retains reservations rather than accepting a cheap partial
    interpretation of inconsistent accounting. Missing fields are allowed.
    """
    if backend not in {"ollama", "llama_cpp"}:
        raise ValueError("unsupported usage backend")
    if type(envelope) is not dict:
        return unknown_usage("usage_envelope_invalid")
    result = unknown_usage()
    result["errors"] = []
    errors = []

    def count(data, field):
        if field not in data:
            return None
        value = data[field]
        if type(value) is not int or not 0 <= value <= _MAX_COUNT:
            errors.append("invalid_" + field)
            return None
        return value

    if backend == "ollama":
        result["input_tokens"] = count(envelope, "prompt_eval_count")
        result["output_tokens"] = count(envelope, "eval_count")
        result["backend_duration_ns"] = count(envelope, "total_duration")
    else:
        usage = envelope.get("usage", {})
        if type(usage) is not dict:
            return dict(unknown_usage("usage_object_invalid"), status="INVALID")
        result["input_tokens"] = count(usage, "prompt_tokens")
        result["output_tokens"] = count(usage, "completion_tokens")
        total = count(usage, "total_tokens")
        if total is not None and result["input_tokens"] is not None and result["output_tokens"] is not None:
            if total != result["input_tokens"] + result["output_tokens"]:
                errors.append("inconsistent_total_tokens")
        for field, subfield, target, parent in (
                ("prompt_tokens_details", "cached_tokens", "cached_input_tokens", "input_tokens"),
                ("completion_tokens_details", "reasoning_tokens", "reasoning_output_tokens", "output_tokens")):
            detail = usage.get(field, {})
            if type(detail) is not dict:
                errors.append("invalid_" + field)
                continue
            result[target] = count(detail, subfield)
            if result[target] is not None and result[parent] is not None and result[target] > result[parent]:
                errors.append("inconsistent_" + subfield)
    if errors:
        return dict(unknown_usage(), status="INVALID", errors=errors)
    known = sum(result[key] is not None for key in ("input_tokens", "output_tokens"))
    result["status"] = "REPORTED" if known == 2 else "PARTIAL" if known else "UNKNOWN"
    result["provenance"] = "backend_reported_unverified" if any(
        result[key] is not None for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                                            "reasoning_output_tokens", "backend_duration_ns")) else "unknown"
    if not known:
        result["errors"] = ["usage_missing"]
    return result


def estimate_resources(payload, *, max_tokens, timeout_seconds):
    """Conservative byte-based input allowance, explicitly not a tokenizer.

    The 256-unit template allowance is a heuristic. A server can report an
    overage, which the ledger records honestly and blocks from further use.
    compute_ms measures caller elapsed time, not GPU or server execution time.
    """
    if type(payload) is not dict or type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
        raise ValueError("bounded payload and output cap required")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180:
        raise ValueError("bounded transport timeout required")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    if len(encoded) > 1048576:
        raise ValueError("payload exceeds accounting limit")
    return {"calls": 1, "input_tokens": len(encoded) + 256, "output_tokens": max_tokens,
            "compute_ms": math.ceil(timeout_seconds * 1000), "external_credit_micros": 0}


def usage_from_response(config, response):
    """Extract usage only from a bounded response bound to the requested model."""
    from keel_agent.models import HTTPResult, ModelError, _strict_json
    if not isinstance(response, HTTPResult) or type(response.status) is not int or response.status != 200:
        return unknown_usage("response_not_successful")
    if type(response.body) is not bytes or len(response.body) > config.max_response_bytes or type(response.headers) is not dict:
        return unknown_usage("response_envelope_invalid")
    headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json" or headers.get("content-encoding", "identity").lower() != "identity":
        return unknown_usage("response_media_invalid")
    try:
        data = _strict_json(response.body)
    except ModelError:
        return unknown_usage("response_json_invalid")
    if type(data) is not dict or data.get("model") != config.model or "error" in data:
        return unknown_usage("response_model_unbound")
    return parse_usage(config.backend, data)
