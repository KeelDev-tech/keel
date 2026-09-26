"""Enforced admission for explicitly scoped LOCAL handlers.

Trusted host supplies policy, issuer keys, authenticated identity, record resolver,
handlers and durable grant consumption. None can be supplied in request JSON.
This is not an OS sandbox and cannot constrain code that bypasses this gateway.
"""
import hashlib
import hmac
import uuid
from .common import (keys, version, text, strings, choice, require, hexdigest, timestamp,
                     canonical, digest, clock, fresh, clone, integer)

ACTIONS = {"READ_FIELDS", "BUILD_DECISION_BRIEF", "RUN_TWIN", "REPLAY_INCIDENT"}
REQUEST_FIELDS = {"schema_version", "request_id", "workspace_id", "principal_id", "action", "resource_id", "fields"}
GRANT_FIELDS = {"schema_version", "grant_id", "issuer_id", "workspace_id", "principal_id", "policy_revision", "action",
                "resource_id", "resource_revision", "resource_hash", "request_hash", "not_before", "expires_at"}


class Denied(PermissionError):
    pass


def sign_grant(payload, key):
    """Trusted issuer helper, never expose this function/key as an agent tool."""
    require(type(key) is bytes and len(key) >= 32, "issuer requires at least 32 key bytes")
    keys(payload, GRANT_FIELDS); version(payload)
    return {"payload": clone(payload), "signature": hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()}


class CanonicalGrantUse:
    """Single admission via the EXISTING logger's locked event-ID conflict rule.

    Every invocation has a fresh nonce. Reusing the stable grant event ID conflicts
    atomically, even for the identical request. A crash after append consumes the
    grant; no automatic retry. Rotation/truncation must preserve consumed history.
    """
    def __init__(self, logger): self.logger = logger

    def __call__(self, grant, request):
        nonce = uuid.uuid4().hex
        event_id = "grant-use-" + digest([grant["workspace_id"], grant["issuer_id"], grant["grant_id"]])
        details = {"grant_ref_hash": digest([grant["issuer_id"], grant["grant_id"]]), "request_hash": digest(request),
                   "action": request["action"], "use_nonce": nonce, "provider_acceptance_verified": False}
        try:
            receipt = self.logger("authority_use", source="keel-trust", details=details, event_id=event_id)
        except ValueError:
            return False
        require(type(receipt) is dict and receipt.get("event_type") == "authority_use"
                and receipt.get("event_id") == event_id and receipt.get("details") == details,
                "canonical logger failed to acknowledge grant consumption")
        return True


class Gateway:
    def __init__(self, *, policy, issuer_keys, resolve_record, handlers, consume_grant):
        keys(policy, {"schema_version", "workspace_id", "revision", "paused", "essential_actions", "principals", "max_record_age_seconds"})
        version(policy); text(policy["workspace_id"]); text(policy["revision"])
        require(type(policy["paused"]) is bool, "explicit pause flag required")
        strings(policy["essential_actions"]); require(set(policy["essential_actions"]) <= ACTIONS, "unknown essential action")
        integer(policy["max_record_age_seconds"], maximum=90); require(policy["max_record_age_seconds"] > 0, "positive freshness bound required")
        require(type(policy["principals"]) is dict, "principal policy required")
        for principal, rules in policy["principals"].items():
            text(principal); require(type(rules) is dict and set(rules) <= ACTIONS, "unsupported action policy")
            for fields in rules.values(): strings(fields, "field allowlist", nonempty=True)
        require(type(issuer_keys) is dict and issuer_keys, "trusted issuers required")
        for issuer, key in issuer_keys.items():
            text(issuer); require(type(key) is bytes and len(key) >= 32, "invalid issuer key")
        require(type(handlers) is dict and set(handlers) <= ACTIONS and all(callable(fn) for fn in handlers.values()), "local handler allowlist required")
        require(callable(resolve_record) and callable(consume_grant), "trusted resolver and durable grant consumer required")
        self.policy = clone(policy); self.issuers = dict(issuer_keys)
        self.resolve_record = resolve_record; self.handlers = dict(handlers); self.consume_grant = consume_grant

    def invoke(self, request, signed_grant, *, authenticated_principal, now):
        clock(now)
        # Clone before validation so handlers/callers cannot mutate checked objects.
        request = clone(request); signed_grant = clone(signed_grant)
        try:
            keys(request, REQUEST_FIELDS); version(request)
            for field in ("request_id", "workspace_id", "principal_id", "resource_id"): text(request[field])
            choice(request["action"], ACTIONS, "local action"); strings(request["fields"], nonempty=True)
            keys(signed_grant, {"payload", "signature"}); grant = signed_grant["payload"]
            keys(grant, GRANT_FIELDS); version(grant); hexdigest(signed_grant["signature"])
            for field in GRANT_FIELDS - {"schema_version", "resource_hash", "request_hash", "not_before", "expires_at"}: text(grant[field])
            hexdigest(grant["resource_hash"]); hexdigest(grant["request_hash"])
            key = self.issuers.get(grant["issuer_id"])
            if key is None or not hmac.compare_digest(signed_grant["signature"], hmac.new(key, canonical(grant), hashlib.sha256).hexdigest()):
                raise Denied("UNTRUSTED_GRANT")
            if authenticated_principal != request["principal_id"] or grant["principal_id"] != authenticated_principal:
                raise Denied("PRINCIPAL_MISMATCH")
            for field in ("workspace_id", "action", "resource_id"):
                if grant[field] != request[field]: raise Denied("GRANT_SCOPE_MISMATCH")
            if request["workspace_id"] != self.policy["workspace_id"] or grant["policy_revision"] != self.policy["revision"]:
                raise Denied("POLICY_OR_WORKSPACE_CHANGED")
            if grant["request_hash"] != digest(request): raise Denied("REQUEST_CHANGED")
            begins, expires = timestamp(grant["not_before"]), timestamp(grant["expires_at"])
            if not begins <= now < expires or not 0 < (expires - begins).total_seconds() <= 900:
                raise Denied("GRANT_EXPIRED_FUTURE_OR_TOO_LONG")
            action = request["action"]
            if self.policy["paused"] and action not in self.policy["essential_actions"]: raise Denied("AUTOMATION_PAUSED")
            allowed = self.policy["principals"].get(authenticated_principal, {}).get(action)
            if allowed is None or not set(request["fields"]) <= set(allowed): raise Denied("FIELD_OR_ACTION_NOT_PERMITTED")
            if action not in self.handlers: raise Denied("HANDLER_NOT_REGISTERED")
            # Only now may the trusted resolver read the resource.
            record = clone(self.resolve_record(request["resource_id"]))
            keys(record, {"schema_version", "record_id", "workspace_id", "revision", "observed_at", "fields"}); version(record)
            text(record["revision"]); require(type(record["fields"]) is dict, "record fields must be an object")
            if record["record_id"] != request["resource_id"] or record["workspace_id"] != self.policy["workspace_id"]:
                raise Denied("RESOURCE_IDENTITY_MISMATCH")
            if record["revision"] != grant["resource_revision"] or digest(record) != grant["resource_hash"]:
                raise Denied("RESOURCE_CHANGED")
            if not fresh(record["observed_at"], now, self.policy["max_record_age_seconds"]): raise Denied("RESOURCE_STALE_OR_FUTURE")
            if not set(request["fields"]) <= record["fields"].keys(): raise Denied("RESOURCE_FIELDS_MISSING")
            view = {name: clone(record["fields"][name]) for name in request["fields"]}
            if self.consume_grant(clone(grant), clone(request)) is not True: raise Denied("GRANT_ALREADY_USED_OR_UNCONFIRMED")
        except (ValueError, TypeError, KeyError) as exc:
            raise Denied("INVALID_ADMISSION_INPUT") from exc
        # No grant, private policy, issuer key or full record reaches the handler.
        result = self.handlers[action](view)
        return {"admission": "LOCAL_HANDLER_COMPLETED", "request_hash": digest(request),
                "disclosed_fields": list(request["fields"]), "result": result,
                "external_action_authorized": False, "provider_acceptance_verified": False}
