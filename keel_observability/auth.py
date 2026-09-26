"""Host-configured local producer HMAC verification; no cloud identity required.

A valid MAC establishes control of a configured producer secret. It does not
establish human presence or provider acceptance. Keys never come from imports.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from types import MappingProxyType

from .store import KINDS, Verification, canonical_event, require, sha, token


@dataclass(frozen=True)
class ProducerKey:
    key_id: str
    account_id: str
    producer_id: str
    subject: str
    secret: bytes = field(repr=False)
    allowed_kinds: tuple[str, ...] = ("source", "opportunity", "application", "attempt", "evidence", "measurement")
    not_before: int = 0
    expires_at: int = 2**53 - 1
    revoked: bool = False

    def __post_init__(self):
        for value in (self.key_id, self.account_id, self.producer_id, self.subject):
            token(value)
        require(type(self.secret) is bytes and 32 <= len(self.secret) <= 4096, "producer_key_requires_at_least_32_random_bytes")
        require(type(self.allowed_kinds) is tuple and self.allowed_kinds and
                all(type(kind) is str and kind in KINDS for kind in self.allowed_kinds), "invalid_producer_key_scope")
        require(type(self.not_before) is int and type(self.expires_at) is int and
                0 <= self.not_before < self.expires_at < 2**53, "invalid_key_lifetime")
        require(type(self.revoked) is bool, "invalid_key_revocation_flag")


FIELDS = frozenset({"schema", "store_id", "account_id", "producer_id", "key_id", "issued_at", "expires_at", "nonce", "signature"})


def _message(raw, context):
    return canonical_event({**{name: context[name] for name in FIELDS if name != "signature"},
                            "event_sha256": hashlib.sha256(raw).hexdigest()})


def sign_context(event, *, key, store_id, issued_at, expires_at, nonce):
    """Producer-side helper. The caller already needs a trusted host-owned key."""
    require(type(key) is ProducerKey and not key.revoked, "active_producer_key_required")
    token(store_id); token(nonce)
    require(type(issued_at) is int and type(expires_at) is int and key.not_before <= issued_at < expires_at <= key.expires_at,
            "invalid_signature_lifetime")
    require(event.get("account_id") == key.account_id and event.get("producer_id") == key.producer_id and event.get("kind") in key.allowed_kinds,
            "producer_key_binding_mismatch")
    context = {"schema": "keel.producer-hmac.v1", "store_id": store_id, "account_id": key.account_id,
               "producer_id": key.producer_id, "key_id": key.key_id, "issued_at": issued_at,
               "expires_at": expires_at, "nonce": nonce}
    context["signature"] = hmac.new(key.secret, _message(canonical_event(event), context), hashlib.sha256).hexdigest()
    return context


class HMACProducerVerifier:
    """Immutable host policy snapshot. Replace the verifier to rotate/revoke keys.

    Previously recorded verification expires at the signed context expiry. For
    immediate historic invalidation, ingest explicit observation revocations;
    key-policy replacement alone cannot edit past database observations.
    """
    def __init__(self, *, store_id, keys, max_lifetime_seconds=300):
        self.store_id = token(store_id)
        require(type(keys) is dict and 0 < len(keys) <= 1024 and
                all(type(key) is ProducerKey and key_id == key.key_id for key_id, key in keys.items()), "invalid_host_key_policy")
        require(type(max_lifetime_seconds) is int and 1 <= max_lifetime_seconds <= 3600, "invalid_signature_max_lifetime")
        self.keys = MappingProxyType(dict(keys))
        self.max_lifetime_seconds = max_lifetime_seconds

    def __call__(self, raw, context, now):
        require(type(raw) is bytes and type(context) is dict and set(context) == FIELDS, "invalid_signature_context")
        require(context["schema"] == "keel.producer-hmac.v1" and context["store_id"] == self.store_id, "signature_store_binding_mismatch")
        token(context["nonce"]); token(context["key_id"]); sha(context["signature"])
        key = self.keys.get(context["key_id"])
        require(key is not None and not key.revoked, "producer_key_unavailable_or_revoked")
        issued, expires = context["issued_at"], context["expires_at"]
        require(type(issued) is int and type(expires) is int and key.not_before <= issued <= now < expires <= key.expires_at and
                0 < expires - issued <= self.max_lifetime_seconds, "signature_expired_or_invalid_lifetime")
        require(context["account_id"] == key.account_id and context["producer_id"] == key.producer_id, "signature_producer_binding_mismatch")
        expected = hmac.new(key.secret, _message(raw, context), hashlib.sha256).hexdigest()
        require(hmac.compare_digest(expected, context["signature"]), "signature_mismatch")
        event = json.loads(raw)
        require(canonical_event(event) == raw and event["account_id"] == key.account_id and event["producer_id"] == key.producer_id and event["kind"] in key.allowed_kinds,
                "signature_event_binding_mismatch")
        return Verification(key.account_id, key.producer_id, key.subject, hashlib.sha256(raw).hexdigest(),
                            key.allowed_kinds, now, expires, "authenticated_producer")
