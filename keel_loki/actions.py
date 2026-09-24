"""Preparation-only typed gateway; a trusted host supplies policy and approvals.

This is an application boundary, not OS process containment. Do not expose host
snapshot updates, approval registration or this object to a model tool runner.
Capabilities are process-local: restart loses grants and fails closed.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

from .forms import (FormError, build_plan, clone, digest, exact, hash_value, ident,
                    integer, validate_contract, validate_origin, validate_revisions)

OPERATIONS = {"text": "set_text", "select": "select_option", "radio": "choose_radio",
              "checkbox": "set_checkbox", "attestation": "set_attestation", "attachment": "attach_file"}


class ActionError(FormError):
    pass


def _snapshot(value):
    value = clone(value)
    exact(value, ("schema", "snapshot_id", "origin", "account_id", "contract_sha256", "revisions",
                  "consent", "approval_current", "no_ai", "holds", "unknown_attempt", "rate_limited",
                  "revoked_approval_ids", "issued_at", "expires_at", "synthetic"), "host snapshot")
    if value["schema"] != "keel.loki.host.v1":
        raise ActionError("invalid host schema")
    ident(value["snapshot_id"])
    ident(value["account_id"])
    validate_origin(value["origin"])
    hash_value(value["contract_sha256"])
    validate_revisions(value["revisions"])
    for key in ("consent", "approval_current", "no_ai", "unknown_attempt", "rate_limited", "synthetic"):
        if type(value[key]) is not bool:
            raise ActionError("policy flags must be exact booleans")
    for key in ("holds", "revoked_approval_ids"):
        if not isinstance(value[key], list) or len(value[key]) > 1000:
            raise ActionError("bounded policy lists required")
        for item in value[key]:
            ident(item)
        if len(set(value[key])) != len(value[key]):
            raise ActionError("duplicate policy entries")
    integer(value["issued_at"])
    integer(value["expires_at"])
    if value["expires_at"] <= value["issued_at"]:
        raise ActionError("invalid policy interval")
    return value


def _actions(plan):
    return [{"operation": OPERATIONS[row["kind"]], "field_id": row["field_id"], "value": clone(row["value"]),
             "approval_id": row["approval_id"], "plan_sha256": digest(plan)}
            for row in plan["fields"] if row["state"] == "prepared"]


class ActionGateway:
    """Instantiate inside trusted host, with authenticated host-loaded approvals.

    The model may supply only proposed `values`; it cannot create approval grants.
    The host must call update_snapshot with fresh canonical state before effects.
    These inputs are not authenticated by hashes, and must never be accepted from
    the model or an untrusted HTTP request as proof of human consent.
    """

    def __init__(self, contract, host_snapshot, host_approvals):
        self._contract = validate_contract(contract)
        self._host = _snapshot(host_snapshot)
        self._approvals = clone(host_approvals)
        self._key = secrets.token_bytes(32)
        self._grants = {}
        self._revoked = set()
        self._lock = threading.RLock()

    def update_snapshot(self, snapshot):
        """Trusted-host-only refresh; previously issued capabilities are rechecked."""
        value = _snapshot(snapshot)
        with self._lock:
            if value["issued_at"] < self._host["issued_at"]:
                raise ActionError("host policy time cannot move backward")
            self._host = value

    def _check(self, now, plan=None):
        integer(now)
        host = self._host
        if not host["issued_at"] <= now < host["expires_at"]:
            raise ActionError("host policy snapshot expired or from the future")
        if (host["consent"] is not True or host["approval_current"] is not True or host["no_ai"] or
                host["holds"] or host["unknown_attempt"] or host["rate_limited"]):
            raise ActionError("host consent/policy/hold/unknown/429 gate blocks preparation")
        if (host["contract_sha256"] != digest(self._contract) or host["revisions"] != self._contract["revisions"] or
                host["origin"] != self._contract["origin"] or host["account_id"] != self._contract["account_id"]):
            raise ActionError("host scope or revision changed")
        if plan is not None:
            if now >= plan["valid_until"]:
                raise ActionError("human preparation approval expired")
            if any(row["approval_id"] in host["revoked_approval_ids"] for row in plan["fields"]):
                raise ActionError("human preparation approval revoked")

    def issue(self, proposed_values, *, now, ttl=60):
        """A host may issue only a preparation capability for existing human grants."""
        integer(ttl, 1, 300)
        with self._lock:
            self._check(now)
            plan = build_plan(self._contract, proposed_values, self._approvals, now=now)
            plan["synthetic"] = self._host["synthetic"]
            self._check(now, plan)
            actions = _actions(plan)
            token = {"schema": "keel.loki.capability.v1", "capability_id": secrets.token_hex(32),
                     "plan_sha256": digest(plan), "issued_at": now,
                     "expires_at": min(now + ttl, self._host["expires_at"], plan["valid_until"]),
                     "operation_scope": "prepare_only", "action_count": len(actions)}
            token["signature"] = hmac.new(self._key, digest(token).encode(), hashlib.sha256).hexdigest()
            self._grants[token["capability_id"]] = {"token": clone(token), "plan": plan, "actions": actions, "next": 0}
            return {"capability": clone(token), "plan": clone(plan), "actions": clone(actions)}

    def consume(self, capability, action, *, now):
        """Validate and consume once BEFORE a browser effect; no automatic retries.

        A browser error after this returns has an uncertain effect. Revoke the
        capability and obtain a fresh verified readback; never replay this action.
        """
        with self._lock:
            self._check(now)
            exact(capability, ("schema", "capability_id", "plan_sha256", "issued_at", "expires_at",
                               "operation_scope", "action_count", "signature"), "capability")
            token = clone(capability)
            if token["schema"] != "keel.loki.capability.v1" or token["operation_scope"] != "prepare_only":
                raise ActionError("unknown capability scope")
            hash_value(token["capability_id"])
            hash_value(token["signature"])
            signature = token.pop("signature")
            expected = hmac.new(self._key, digest(token).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ActionError("invalid capability signature")
            grant = self._grants.get(token["capability_id"])
            if grant is None or token["capability_id"] in self._revoked:
                raise ActionError("unknown, revoked or previous-process capability")
            if digest(capability) != digest(grant["token"]):
                raise ActionError("capability mismatch")
            if not token["issued_at"] <= now < token["expires_at"]:
                raise ActionError("expired or future capability")
            self._check(now, grant["plan"])
            index = grant["next"]
            if index >= len(grant["actions"]):
                raise ActionError("capability exhausted")
            if digest(action) != digest(grant["actions"][index]):
                raise ActionError("unknown, replayed, out-of-order or out-of-scope action")
            grant["next"] += 1
            return clone(grant["actions"][index])

    def revoke(self, capability_id):
        """Trusted host revocation, including on uncertain browser effects."""
        hash_value(capability_id)
        with self._lock:
            self._revoked.add(capability_id)

    def status(self, capability_id):
        hash_value(capability_id)
        with self._lock:
            grant = self._grants.get(capability_id)
            return {"known": grant is not None, "revoked": capability_id in self._revoked,
                    "consumed": grant["next"] if grant else 0,
                    "total": len(grant["actions"]) if grant else 0,
                    "submission_authorized": False}
