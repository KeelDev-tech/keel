"""Bounded, immutable exact-action envelopes. No request chooses its actor."""
from __future__ import annotations
import base64
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_PAYLOAD = 262144
MAX_ATTACHMENT = 1048576
MAX_CONTENT = 1572864
MAX_ATTACHMENTS = 4
MAX_WIRE = 2200000
MAX_TTL = 600
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
ALLOWED_ACTIONS = frozenset({"browser.submit"})

class InvalidRequest(ValueError):
    pass


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise InvalidRequest("invalid_identifier")
    return value


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def strict_json(data: bytes) -> dict:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_WIRE:
        raise InvalidRequest("message_size")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidRequest("duplicate_key")
            result[key] = value
        return result
    def constant(_):
        raise InvalidRequest("invalid_number")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InvalidRequest("invalid_json") from exc
    if type(result) is not dict:
        raise InvalidRequest("invalid_root")
    return result


def _blob(value: object, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise InvalidRequest("invalid_content")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as exc:
        raise InvalidRequest("invalid_content") from exc
    if len(decoded) > limit or base64.b64encode(decoded).decode("ascii") != value:
        raise InvalidRequest("invalid_content")
    return decoded


def _destination(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or not value.isascii():
        raise InvalidRequest("invalid_destination")
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise InvalidRequest("invalid_destination")
    try:
        parsed = urlsplit(value)
        valid = (value.startswith("https://") and parsed.scheme == "https" and
                 parsed.hostname and parsed.netloc and not parsed.username and
                 not parsed.password and not parsed.fragment and "#" not in value and
                 "@" not in parsed.netloc and parsed.port in (None, 443))
        if not valid or parsed.hostname != parsed.hostname.lower():
            raise InvalidRequest("invalid_destination")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", parsed.hostname):
            raise InvalidRequest("invalid_destination")
        if any(not part or part.startswith("-") or part.endswith("-") or len(part) > 63
               for part in parsed.hostname.split(".")):
            raise InvalidRequest("invalid_destination")
        hostname = parsed.hostname
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise InvalidRequest("literal_address_denied")
        labels = hostname.split(".")
        if (len(labels) < 2 or not re.fullmatch(r"[a-z]{2,63}", labels[-1]) or
                labels[-1] in {"localhost", "local", "internal", "lan", "home", "invalid", "test"} or
                any(label in {"localhost", "localdomain", "metadata", "instance-data"} for label in labels)):
            raise InvalidRequest("nonpublic_destination_denied")
        if parsed.netloc not in (parsed.hostname, parsed.hostname + ":443"):
            raise InvalidRequest("invalid_destination")
    except ValueError as exc:
        raise InvalidRequest("invalid_destination") from exc
    return value


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    actor: str
    role_id: str
    attempt_id: str
    approval_id: str
    action: str
    destination: str
    payload: bytes
    attachments: tuple[Attachment, ...]
    expires_at: int
    nonce: str
    policy_revision: str

    @classmethod
    def from_request(cls, request: dict, *, actor: str, now: int) -> "ActionEnvelope":
        keys = {"role_id", "attempt_id", "approval_id", "action", "destination",
                "payload_b64", "attachments", "expires_at", "nonce", "policy_revision"}
        if type(request) is not dict or set(request) != keys:
            raise InvalidRequest("invalid_fields")
        if type(now) is not int or type(request["expires_at"]) is not int:
            raise InvalidRequest("invalid_expiry")
        if not now < request["expires_at"] <= now + MAX_TTL:
            raise InvalidRequest("expired_or_overlong")
        if type(request["action"]) is not str or request["action"] not in ALLOWED_ACTIONS:
            raise InvalidRequest("action_disabled")
        attachments = request["attachments"]
        if type(attachments) is not list or len(attachments) > MAX_ATTACHMENTS:
            raise InvalidRequest("invalid_attachments")
        content = _blob(request["payload_b64"], MAX_PAYLOAD)
        result = []
        names = set()
        for item in attachments:
            if type(item) is not dict or set(item) != {"name", "content_b64"}:
                raise InvalidRequest("invalid_attachment")
            name = item["name"]
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
                raise InvalidRequest("invalid_attachment_name")
            names.add(name)
            result.append(Attachment(name, _blob(item["content_b64"], MAX_ATTACHMENT)))
        if len(content) + sum(len(item.content) for item in result) > MAX_CONTENT:
            raise InvalidRequest("content_too_large")
        return cls(actor=identifier(actor), role_id=identifier(request["role_id"]),
                   attempt_id=identifier(request["attempt_id"]),
                   approval_id=identifier(request["approval_id"]), action=request["action"],
                   destination=_destination(request["destination"]), payload=content,
                   attachments=tuple(result), expires_at=request["expires_at"],
                   nonce=identifier(request["nonce"]),
                   policy_revision=identifier(request["policy_revision"]))

    def to_request(self) -> dict:
        return {"role_id": self.role_id, "attempt_id": self.attempt_id,
                "approval_id": self.approval_id, "action": self.action,
                "destination": self.destination,
                "payload_b64": base64.b64encode(self.payload).decode("ascii"),
                "attachments": [{"name": a.name, "content_b64": base64.b64encode(a.content).decode("ascii")}
                                for a in self.attachments],
                "expires_at": self.expires_at, "nonce": self.nonce,
                "policy_revision": self.policy_revision}

    @property
    def digest(self) -> str:
        bound = {"schema": 1, "actor": self.actor, "role_id": self.role_id,
                 "attempt_id": self.attempt_id, "approval_id": self.approval_id,
                 "action": self.action, "destination": self.destination,
                 "payload_sha256": hashlib.sha256(self.payload).hexdigest(),
                 "payload_size": len(self.payload),
                 "attachments": [{"name": a.name, "sha256": hashlib.sha256(a.content).hexdigest(),
                                   "size": len(a.content)} for a in self.attachments],
                 "expires_at": self.expires_at, "nonce": self.nonce,
                 "policy_revision": self.policy_revision}
        return hashlib.sha256(canonical(bound)).hexdigest()
