"""One durable resource reservation for each attempted local inference call."""
import hashlib
from contextvars import ContextVar
import json
import math
import time
import uuid

from .usage import estimate_resources, unknown_usage, usage_from_response


_ACTIVE_DISPATCH = ContextVar("keel_governed_dispatch", default=None)


def claim_governed_loopback(config, payload, timeout_seconds):
    """Claim one nested loopback send covered by an enclosing reservation."""
    active = _ACTIVE_DISPATCH.get()
    if active is None:
        return False
    if active["loopback_calls"]:
        raise ValueError("one loopback send per resource reservation")
    from keel_agent.models import reviewer_config_digest
    if (reviewer_config_digest(config) != active["config_sha256"] or
            _payload_digest(payload) != active["payload_sha256"] or
            type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or
            not 0 < timeout_seconds <= active["timeout_seconds"]):
        raise ValueError("nested dispatch differs from admitted resource binding")
    active["loopback_calls"] = 1
    return True


def _payload_digest(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class GovernedTransport:
    """Wrap an explicitly configured local transport; no retries or fallback.

    last_receipt is diagnostic only and intended for sequential callers. The
    ledger itself serializes concurrent spending. Construct one wrapper per
    worker when associating receipts with responses.
    """
    def __init__(self, ledger, scope_id, transport, binding=None):
        from .ledger import ResourceLedger
        if not isinstance(ledger, ResourceLedger) or not isinstance(scope_id, str) or not scope_id:
            raise ValueError("resource ledger and scope_id required")
        if not callable(transport) or isinstance(transport, GovernedTransport):
            raise ValueError("one unwrapped local transport required")
        if binding is not None and type(binding) is not dict:
            raise ValueError("binding must be a dictionary")
        self.ledger, self.scope_id, self.transport = ledger, scope_id, transport
        self.binding = dict(binding or {})
        self.last_receipt = None
        self.last_usage = unknown_usage("not_dispatched")

    def __call__(self, config, payload, timeout_seconds):
        from keel_agent.models import ReviewerConfig, reviewer_config_digest
        from .ledger import BudgetExceeded
        self.last_receipt = None
        self.last_usage = unknown_usage("not_dispatched")
        if not isinstance(config, ReviewerConfig):
            raise ValueError("validated local ReviewerConfig required")
        if _ACTIVE_DISPATCH.get() is not None:
            raise ValueError("nested governed transports are forbidden")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= config.timeout_seconds:
            raise ValueError("invalid governed transport timeout")
        if type(payload) is not dict or payload.get("model") != config.model or payload.get("stream") is not False:
            raise ValueError("governed payload must bind model and disable streaming")
        # Enforce the actual wire cap as well as the declared reviewer config.
        if config.backend == "ollama":
            options = payload.get("options")
            wire_cap = options.get("num_predict") if type(options) is dict else None
        else:
            wire_cap = payload.get("max_tokens")
        if type(wire_cap) is not int or not 1 <= wire_cap <= config.max_tokens:
            raise ValueError("governed payload output cap exceeds config")
        estimate = estimate_resources(payload, max_tokens=wire_cap, timeout_seconds=timeout_seconds)
        request_id = uuid.uuid4().hex
        metadata = dict(self.binding, backend=config.backend, model=config.model,
                        payload_sha256=_payload_digest(payload),
                        input_estimate_method="utf8_payload_bytes_plus_256_template_allowance",
                        estimate_is_tokenizer_exact=False, compute_ms_basis="client_elapsed_wall_time",
                        paid_fallback_enabled=False, backend_billing_attested=False)
        self.last_receipt = self.ledger.reserve(request_id, self.scope_id, estimate, metadata=metadata)
        # A crash after this transition retains the whole estimate for recovery.
        try:
            self.last_receipt = self.ledger.mark_dispatched(request_id)
        except BudgetExceeded:
            # A concurrent overage can lock an ancestor between reserve/send.
            # This transition did not grant dispatch, so cancellation is safe.
            self.last_receipt = self.ledger.cancel(request_id)
            raise
        self.last_usage = unknown_usage("transport_did_not_return_usage")
        started = time.monotonic()
        context_token = _ACTIVE_DISPATCH.set({"loopback_calls": 0,
            "config_sha256": reviewer_config_digest(config), "payload_sha256": metadata["payload_sha256"],
            "timeout_seconds": timeout_seconds})
        try:
            response = self.transport(config, payload, timeout_seconds)
            self.last_usage = usage_from_response(config, response)
            return response
        finally:
            _ACTIVE_DISPATCH.reset(context_token)
            elapsed = math.ceil(max(0, time.monotonic() - started) * 1000)
            # Cached and reasoning counts are subsets, never added/subtracted.
            self.last_receipt = self.ledger.settle(request_id, {
                "calls": 1, "input_tokens": self.last_usage["input_tokens"],
                "output_tokens": self.last_usage["output_tokens"],
                "compute_ms": elapsed, "external_credit_micros": 0})
