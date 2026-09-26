"""Synthetic fixture builders. In-memory only. Never persisted to real stores."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

SYNTHETIC_LABEL = "SYNTHETIC fixture -- mechanism test only, never evidence"

SCOPE = {
    "workspace_id": "synthetic-workspace",
    "role_id": "synthetic-role",
    "application_id": "synthetic-application",
    "action": "submit",
}


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest_of(payload) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def canary_record(family: str, payload: dict, *, provenance="authentic_observation",
                  expires_at=None, revoked=False, source_ref="synthetic-src",
                  source_version="v1-test") -> dict:
    """One canary-shaped record with a correct material digest."""
    return {
        **SCOPE,
        "component": family,
        "source_ref": source_ref,
        "source_version": source_version,
        "observed_at": iso_now(),
        "provenance": provenance,
        "provenance_reason": None,
        "expires_at": expires_at if expires_at is not None else iso_in(30),
        "revoked": revoked,
        "revocation_ref": None,
        "material_digest": digest_of(payload),
        "capture_version": "keel-canary-capture/0.8.0-test",
        "payload": payload,
    }


def six_family_records(**overrides) -> list[dict]:
    """Six authentic synthetic records, one per family."""
    from review.packet import FAMILIES
    return [canary_record(f, {"family": f, "value": f"test-{f}"},
                          **overrides.get(f, {})) for f in FAMILIES]


def synthetic_decision_fixture(packet_sha256: str) -> dict:
    return {
        "synthetic": True,
        "label": SYNTHETIC_LABEL,
        "decision": "APPROVE",
        "actor": "synthetic-actor",
        "authority_ref": "synthetic-fixture-authority",
        "reviewed_sha256": packet_sha256,
        "request_id": "synthetic-request",
        "decision_id": "synthetic-decision",
        "expires_at": iso_in(30),
    }


def synthetic_authz_fixture(*, decision_id: str, packet_sha256: str,
                            scope: dict) -> dict:
    return {
        "schema": "keel.execution_authorization.v1",
        "authz_id": "synthetic-authz",
        "decision_id": decision_id,
        "packet_sha256": packet_sha256,
        "scope": scope,
        "actor": "synthetic-actor",
        "authority_ref": "synthetic-fixture-authority",
        "issued_at": iso_now(),
        "expires_at": iso_in(30),
        "revoked": False,
        "synthetic": True,
        "label": SYNTHETIC_LABEL,
    }


def synthetic_shadow_fixture(packet, families: dict) -> dict:
    """Full synthetic shadow fixture: families + decision + authz."""
    return {
        "synthetic": True,
        "label": SYNTHETIC_LABEL,
        "families": families,
        "decision": synthetic_decision_fixture(packet["packet_sha256"]),
        "execution_authorization": synthetic_authz_fixture(
            decision_id="synthetic-decision",
            packet_sha256=packet["packet_sha256"],
            scope=packet["scope"]),
    }
