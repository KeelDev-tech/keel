"""Record and verify separately supplied privacy-reviewer decisions.

Decision records require an Ed25519 signature over release ID, artifact
digest, decision, reviewer identity, authority reference, validity window,
conditions and packet digest. The registered reviewer key must be canonical,
nonidentity and prime order. The registry ships empty and verification
returns denial when records, signatures or authority bindings are invalid.

The registry and controller code remain trusted local inputs. The seal
chain detects inconsistent edits, but has no independent rollback anchor:
it does not prove a privileged writer could not replace or truncate state.
Signatures do not establish that a registered person is authorized counsel;
registration remains the owner's separately evidenced ceremony.

verify_decision validates the effective signed approval record. Conditional
approval is not itself unrestricted egress permission; publication_guard
requires unconditional approval until a condition evaluator exists. The
existing signature does not bind destination or all release metadata, and
every actual sender still needs a trusted enforcement integration.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keel.privacy import _ed25519

DEFAULT_STATE_DIR = os.path.expanduser("~/workspace/keel/privacy/state")
GENESIS_SEAL = "0" * 64
HEX64 = set("0123456789abcdef")

APPROVING_DECISIONS = {"approved", "approved_with_conditions"}
ALL_DECISIONS = APPROVING_DECISIONS | {"denied", "needs_information"}


class DecisionError(ValueError):
    """Raised when a decision (or authority) record is invalid."""


class AuthorityNotRegistered(DecisionError):
    """Raised when a decision names an authority not in the registry."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _parse_ts(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DecisionError(f"{field_name}: required ISO-8601 timestamp string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DecisionError(f"{field_name}: not a parseable timestamp: {value!r}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise DecisionError(f"{field_name}: timezone is required")
    return dt


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _is_hex64(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in HEX64 for c in value))


# ---------------------------------------------------------------------------
# Seal-chained append-only ledger.
# ---------------------------------------------------------------------------

class _SealedLedger:
    """Append-only JSONL ledger where each record seals its predecessor.

    record = {"seq": N, "prev_seal": S, "seal": H, "body": {...}}
    seal = sha256(prev_seal + canonical(body)).
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def replay(self) -> tuple[bool, list[dict], str]:
        """(ok, bodies, reason). ok=False if the chain is broken in any way."""
        raw = self._read_all()
        bodies: list[dict] = []
        prev = GENESIS_SEAL
        for i, rec in enumerate(raw, start=1):
            if rec.get("seq") != i:
                return False, [], f"seq break at record {i}"
            if rec.get("prev_seal") != prev:
                return False, [], f"prev_seal mismatch at record {i}"
            expected = hashlib.sha256(
                (prev + _canonical(rec.get("body", {}))).encode("utf-8")
            ).hexdigest()
            if rec.get("seal") != expected:
                return False, [], f"seal mismatch at record {i} (record tampered)"
            bodies.append(rec["body"])
            prev = rec["seal"]
        return True, bodies, "chain intact"

    def append(self, body: dict) -> dict:
        ok, _, reason = self.replay()
        if not ok:
            raise DecisionError(f"ledger tampered, refusing append: {reason}")
        raw = self._read_all()
        prev = raw[-1]["seal"] if raw else GENESIS_SEAL
        record = {
            "seq": len(raw) + 1,
            "prev_seal": prev,
            "seal": hashlib.sha256(
                (prev + _canonical(body)).encode("utf-8")).hexdigest(),
            "body": body,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canonical(record) + "\n")
        return body


def _ledgers(state_dir: str | os.PathLike | None) -> tuple[_SealedLedger, _SealedLedger]:
    base = Path(state_dir) if state_dir else Path(DEFAULT_STATE_DIR)
    return (_SealedLedger(base / "authorities.jsonl"),
            _SealedLedger(base / "decisions.jsonl"))


# ---------------------------------------------------------------------------
# Authority registry. Ships EMPTY — fail closed.
# ---------------------------------------------------------------------------

REQUIRED_AUTHORITY_FIELDS = [
    "authority_reference",   # stable id, e.g. "ENG-2026-0042" from engagement letter
    "authority_name",        # human/org name
    "authority_type",        # "external_privacy_counsel" | "internal_privacy_officer"
    "scope_note",            # what this authority may decide on
    "engagement_start",      # ISO-8601
    "registered_by",         # identity performing the owner ceremony
    "registration_evidence", # free-text pointer to the out-of-band evidence
    "reviewer_public_key",   # 64-hex Ed25519 public key, supplied out-of-band
                             # by the reviewer; signatures verify against it
]


def register_authority(record: dict,
                       state_dir: str | os.PathLike | None = None) -> dict:
    """Record a counsel authority. This is the workspace OWNER's ceremony:
    the authority_reference must come from the real engagement (letter,
    contract, or the counsel's own hand), never invented by an agent.

    Returns the stored authority body. The registry is append-only; an
    authority_reference can never be re-registered (typos need a new
    reference, recorded explicitly).
    """
    authorities, _ = _ledgers(state_dir)
    missing = [f for f in REQUIRED_AUTHORITY_FIELDS if not record.get(f)]
    if missing:
        raise DecisionError(f"authority missing required fields: {missing}")
    if record.get("authority_type") not in {"external_privacy_counsel",
                                            "internal_privacy_officer"}:
        raise DecisionError(
            f"authority_type must be external_privacy_counsel or "
            f"internal_privacy_officer, got {record.get('authority_type')!r}")
    pubkey = record.get("reviewer_public_key", "")
    if not (isinstance(pubkey, str) and len(pubkey) == 64
            and all(c in HEX64 for c in pubkey)):
        raise DecisionError(
            "reviewer_public_key must be a 64-char hex Ed25519 public key "
            "supplied out-of-band by the reviewer")
    if not _ed25519.is_valid_public_key(bytes.fromhex(pubkey)):
        raise DecisionError("reviewer_public_key must encode a nonidentity prime-order point")
    _parse_ts(record["engagement_start"], "engagement_start")
    if record.get("engagement_end"):
        start = _parse_ts(record["engagement_start"], "engagement_start")
        end = _parse_ts(record["engagement_end"], "engagement_end")
        if end <= start:
            raise DecisionError("engagement_end must be after engagement_start")

    ok, bodies, reason = authorities.replay()
    if not ok:
        raise DecisionError(f"authority registry tampered: {reason}")
    if any(b.get("authority_reference") == record["authority_reference"]
           for b in bodies):
        raise DecisionError(
            f"authority_reference {record['authority_reference']!r} already "
            f"registered (registry is append-only)")

    body = {
        "record_type": "authority",
        "authority_reference": record["authority_reference"],
        "authority_name": record["authority_name"],
        "authority_type": record["authority_type"],
        "scope_note": record["scope_note"],
        "engagement_start": record["engagement_start"],
        "engagement_end": record.get("engagement_end", ""),
        "registered_by": record["registered_by"],
        "registration_evidence": record["registration_evidence"],
        "reviewer_public_key": pubkey,
        "registered_at": _utcnow_iso(),
        "status": "active",
    }
    return authorities.append(body)


def get_authority(authority_reference: str,
                  state_dir: str | os.PathLike | None = None) -> dict | None:
    authorities, _ = _ledgers(state_dir)
    ok, bodies, _ = authorities.replay()
    if not ok:
        return None
    for b in bodies:
        if b.get("authority_reference") == authority_reference:
            return b
    return None


def list_authorities(state_dir: str | os.PathLike | None = None) -> list[dict]:
    authorities, _ = _ledgers(state_dir)
    ok, bodies, _ = authorities.replay()
    return bodies if ok else []


# ---------------------------------------------------------------------------
# Decisions.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reviewer signatures.
# ---------------------------------------------------------------------------

def decision_signing_payload(*, release_id: str, artifact_digest: str,
                             decision: str, actor: str,
                             authority_reference: str, decided_at: str,
                             expires_at: str, conditions: list,
                             packet_digest: str) -> bytes:
    """Canonical bytes the reviewer signs (reviewer-side, off this machine).

    The payload binds every field that makes the decision meaningful. The
    reviewer signs the RESOLVED expires_at (after review_interval_days is
    expanded), so the signature covers the exact authorization window.
    Counsel tooling reproduces this construction byte-for-byte; see
    REVIEWER_GUIDE.md for the reference signing snippet.
    """
    return _canonical({
        "schema": "keel.privacy.decision-signature/v1",
        "release_id": release_id,
        "artifact_digest": artifact_digest,
        "decision": decision,
        "actor": actor,
        "authority_reference": authority_reference,
        "decided_at": decided_at,
        "expires_at": expires_at,
        "conditions": list(conditions),
        "packet_digest": packet_digest,
    }).encode("utf-8")


def _verify_reviewer_signature(authority: dict, payload: bytes,
                                signature_hex: str) -> bool:
    try:
        pubkey = bytes.fromhex(authority["reviewer_public_key"])
        sig = bytes.fromhex(signature_hex)
    except (ValueError, KeyError, TypeError):
        return False
    return _ed25519.verify(pubkey, payload, sig)


REQUIRED_DECISION_FIELDS = [
    "release_id",
    "artifact_digest",     # exact 64-hex sha256 the decision binds to
    "decision",            # approved | approved_with_conditions | denied | needs_information
    "actor",               # reviewer name (human)
    "authority_reference", # must be registered
    "conditions",          # list; empty list when unconditional
    "decided_at",          # ISO-8601
    "packet_digest",       # the counsel packet that was reviewed
    "reviewer_signature",  # 128-hex Ed25519 signature over decision_signing_payload
]


def _resolve_expiry(decision: dict, decided_dt: datetime) -> datetime:
    expires_at = decision.get("expires_at")
    interval = decision.get("review_interval_days")
    if expires_at:
        exp = _parse_ts(expires_at, "expires_at")
    elif interval is not None:
        if not isinstance(interval, int) or interval <= 0:
            raise DecisionError("review_interval_days must be a positive integer")
        exp = decided_dt + timedelta(days=interval)
    else:
        raise DecisionError(
            "a valid decision must bind an expiry: supply expires_at or "
            "review_interval_days")
    if exp <= decided_dt:
        raise DecisionError("expiry must be after decided_at")
    return exp


def record_decision(decision: dict,
                    state_dir: str | os.PathLike | None = None) -> dict:
    """Record a reviewer-supplied decision after full validation.

    This function VALIDATES and STORES. It never invents, upgrades, or
    defaults a decision toward approval — the 'decision' value arrives from
    the reviewer and is stored verbatim after checks pass.
    """
    authorities, decisions = _ledgers(state_dir)
    missing = [f for f in REQUIRED_DECISION_FIELDS if decision.get(f) in (None, "")]
    if missing:
        raise DecisionError(f"decision missing required fields: {missing}")
    if not _is_hex64(decision["artifact_digest"]):
        raise DecisionError("artifact_digest must be a 64-char hex sha256")
    if decision["decision"] not in ALL_DECISIONS:
        raise DecisionError(
            f"decision must be one of {sorted(ALL_DECISIONS)}, "
            f"got {decision['decision']!r}")
    if not isinstance(decision["conditions"], list):
        raise DecisionError("conditions must be a list (empty when unconditional)")
    decided_dt = _parse_ts(decision["decided_at"], "decided_at")

    ok, auth_bodies, reason = authorities.replay()
    if not ok:
        raise DecisionError(f"authority registry tampered: {reason}")
    authority = next(
        (b for b in auth_bodies
         if b.get("authority_reference") == decision["authority_reference"]), None)
    if authority is None:
        raise AuthorityNotRegistered(
            f"authority_reference {decision['authority_reference']!r} is not "
            f"registered — decision refused (registry holds "
            f"{len(auth_bodies)} authorities)")
    if authority.get("status") != "active":
        raise DecisionError(
            f"authority {decision['authority_reference']!r} is not active")
    engagement_start = _parse_ts(authority["engagement_start"], "engagement_start")
    registered_at = _parse_ts(authority["registered_at"], "registered_at")
    if decided_dt < engagement_start:
        raise DecisionError("decided_at predates the authority's engagement_start")
    if authority.get("engagement_end"):
        if decided_dt > _parse_ts(authority["engagement_end"], "engagement_end"):
            raise DecisionError("decided_at is after the authority's engagement_end")
    # Tripwire: a decision cannot be simultaneous with (or predate) the
    # authority's own registration — a review takes nonzero time.
    if decided_dt <= registered_at:
        raise DecisionError(
            "decided_at must be strictly after the authority's registered_at "
            "(instant self-registration + approval is refused)")

    expiry_dt = _resolve_expiry(decision, decided_dt)
    expires_at_iso = expiry_dt.isoformat().replace("+00:00", "Z")

    # The reviewer's signature must cover the exact resolved authorization.
    # Without the reviewer's private key this signature cannot be produced —
    # no agent on this machine can mint a decision that records.
    payload = decision_signing_payload(
        release_id=decision["release_id"],
        artifact_digest=decision["artifact_digest"],
        decision=decision["decision"],
        actor=decision["actor"],
        authority_reference=decision["authority_reference"],
        decided_at=decision["decided_at"],
        expires_at=expires_at_iso,
        conditions=decision["conditions"],
        packet_digest=decision["packet_digest"],
    )
    sig_hex = decision.get("reviewer_signature", "")
    if not (isinstance(sig_hex, str) and len(sig_hex) == 128
            and all(c in HEX64 for c in sig_hex)):
        raise DecisionError(
            "reviewer_signature must be a 128-char hex Ed25519 signature")
    if not _verify_reviewer_signature(authority, payload, sig_hex):
        raise DecisionError(
            "reviewer signature INVALID for this decision payload — refused. "
            "(The signature must be produced by the reviewer's private key, "
            "off this machine.)")

    body = {
        "record_type": "decision",
        "release_id": decision["release_id"],
        "artifact_digest": decision["artifact_digest"],
        "decision": decision["decision"],
        "actor": decision["actor"],
        "actor_contact": decision.get("actor_contact", ""),
        "authority_reference": decision["authority_reference"],
        "conditions": list(decision["conditions"]),
        "decided_at": decision["decided_at"],
        "expires_at": expires_at_iso,
        "packet_digest": decision["packet_digest"],
        "reviewer_signature": sig_hex,
        "notes": decision.get("notes", ""),
        "recorded_at": _utcnow_iso(),
    }
    return decisions.append(body)


def revoke_decision(release_id: str, artifact_digest: str, *,
                    actor: str, authority_reference: str, reason: str,
                    state_dir: str | os.PathLike | None = None) -> dict:
    """Append a revocation. After revocation, verify_decision() is False."""
    _, decisions = _ledgers(state_dir)
    ok, bodies, why = decisions.replay()
    if not ok:
        raise DecisionError(f"decision ledger tampered: {why}")
    target = next(
        (b for b in bodies
         if b.get("record_type") == "decision"
         and b.get("release_id") == release_id
         and b.get("artifact_digest") == artifact_digest), None)
    if target is None:
        raise DecisionError(
            f"no decision to revoke for release {release_id} digest "
            f"{artifact_digest[:16]}...")
    body = {
        "record_type": "revocation",
        "release_id": release_id,
        "artifact_digest": artifact_digest,
        "actor": actor,
        "authority_reference": authority_reference,
        "reason": reason,
        "revoked_at": _utcnow_iso(),
    }
    return decisions.append(body)


def _effective_record(bodies: list[dict], release_id: str,
                      artifact_digest: str) -> dict | None:
    """Latest ledger record binding this (release_id, digest)."""
    match = None
    for b in bodies:
        if (b.get("release_id") == release_id
                and b.get("artifact_digest") == artifact_digest
                and b.get("record_type") in ("decision", "revocation")):
            match = b
    return match


def verify_decision(release_id: str, artifact_digest: str,
                    state_dir: str | os.PathLike | None = None,
                    now: datetime | str | None = None) -> bool:
    """Return True ONLY if a valid, unexpired, unrevoked counsel approval
    binds this exact (release_id, artifact_digest).

    Any failure — no record, denied decision, tampered ledger, unknown or
    inactive authority, expired, revoked, digest mismatch — returns False.
    Never raises on verification: denial is the safe answer.
    """
    return verified_decision_record(release_id, artifact_digest, state_dir, now) is not None


def verified_decision_record(release_id: str, artifact_digest: str,
                             state_dir: str | os.PathLike | None = None,
                             now: datetime | str | None = None) -> dict | None:
    """Return the signed effective record from the snapshot actually verified.

    This is a point-in-time decision, not a transferable publication token.
    Callers must use it at their egress boundary. Conditional approval proves
    reviewer authorship only; the publication guard separately denies egress
    until an applicable condition evaluator exists.
    """
    try:
        return _verified_record_or_raise(release_id, artifact_digest, state_dir, now)
    except Exception:
        return None


def _verified_record_or_raise(release_id: str, artifact_digest: str,
                              state_dir: str | os.PathLike | None,
                              now: datetime | str | None) -> dict | None:
    if not isinstance(release_id, str) or not release_id.strip() or not _is_hex64(artifact_digest):
        return None
    authorities, decisions = _ledgers(state_dir)

    ok, auth_bodies, _ = authorities.replay()
    if not ok:
        return None  # tampered registry -> fail closed
    ok, dec_bodies, _ = decisions.replay()
    if not ok:
        return None  # tampered ledger -> fail closed

    rec = _effective_record(dec_bodies, release_id, artifact_digest)
    if rec is None:
        return None
    if rec.get("record_type") == "revocation":
        return None
    if rec.get("decision") not in APPROVING_DECISIONS:
        return None
    # Exact digest binding, re-asserted (defense in depth).
    if rec.get("artifact_digest") != artifact_digest:
        return None

    authority = next(
        (b for b in auth_bodies
         if b.get("authority_reference") == rec.get("authority_reference")), None)
    if authority is None or authority.get("status") != "active":
        return None

    # Re-verify the reviewer's signature over the stored binding fields.
    # A record whose signature does not check out — forged, or tampered after
    # signing — never authorizes egress.
    payload = decision_signing_payload(
        release_id=rec["release_id"],
        artifact_digest=rec["artifact_digest"],
        decision=rec["decision"],
        actor=rec["actor"],
        authority_reference=rec["authority_reference"],
        decided_at=rec["decided_at"],
        expires_at=rec["expires_at"],
        conditions=rec.get("conditions", []),
        packet_digest=rec["packet_digest"],
    )
    if not _verify_reviewer_signature(authority, payload,
                                       rec.get("reviewer_signature", "")):
        return None

    if now is None:
        now_dt = _utcnow()
    elif isinstance(now, str):
        now_dt = _parse_ts(now, "now")
    else:
        now_dt = now
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
    # Authorization is effective on [decided_at, expires_at). A future
    # signed decision cannot authorize an action before its decision time.
    decided_dt = _parse_ts(rec["decided_at"], "decided_at")
    expires_dt = _parse_ts(rec["expires_at"], "expires_at")
    if now_dt < decided_dt or now_dt >= expires_dt or expires_dt <= decided_dt:
        return None
    if decided_dt <= _parse_ts(authority["registered_at"], "registered_at"):
        return None
    if decided_dt < _parse_ts(authority["engagement_start"], "engagement_start"):
        return None
    if authority.get("engagement_end") and decided_dt > _parse_ts(
            authority["engagement_end"], "engagement_end"):
        return None
    return copy.deepcopy(rec)
