"""Publication control state machine.

Keel Blocker Resolution Directive section 3 (Trent-authorized 2026-09-18).

Gate ordering (forward-only, one step at a time):
    PRIVATE_BUILD
      -- test results recorded ------------------> TESTED
      -- red-team report + clean secret/PII scan --> SECURITY_REVIEWED
      -- privacy analysis approved --------------> PRIVACY_REVIEW_READY
      -- release bytes frozen + counsel packet --> PUBLICATION_READY_PENDING_G1
      -- human G1 decision, hash-verified -------> G1_APPROVED
      -- publication enabled + G1 re-verified ---> PUBLICATION_AUTHORIZED
      -- exact approved bytes published ---------> PUBLISHED

Invariants:
  * No state jumps: exactly one forward step per transition.
  * Release bytes are frozen before the counsel packet; the packet and all
    downstream authorizations bind the frozen digest.
  * If release bytes change after freezing (mutation), the machine resets to
    PRIVACY_REVIEW_READY -- no exceptions, no shortcuts.
  * Publish emits only the exact frozen/G1-approved bytes (byte-identical).
  * Publication stays DISABLED while G1 is unresolved: `publication_enabled`
    defaults to False and the machine refuses to enter PUBLICATION_AUTHORIZED
    or PUBLISHED while disabled or while the G1 decision cannot be verified.
  * Counsel-decision verification is an injected dependency:
    `counsel_verifier(release_id, artifact_digest) -> bool`. None/False denies.
    This keeps this module decoupled from workstream B's
    keel/privacy/counsel_decision.py; the coordinator wires the real function
    at integration time.

Stdlib only. No credentials, no network, no subprocesses.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "STATES",
    "PublicationControlError",
    "IllegalTransitionError",
    "EvidenceMissingError",
    "PublicationDisabledError",
    "CounselVerificationError",
    "ByteMismatchError",
    "PublicationStateMachine",
    "sha256_hex",
]

STATES = (
    "PRIVATE_BUILD",
    "TESTED",
    "SECURITY_REVIEWED",
    "PRIVACY_REVIEW_READY",
    "PUBLICATION_READY_PENDING_G1",
    "G1_APPROVED",
    "PUBLICATION_AUTHORIZED",
    "PUBLISHED",
)

# Index from which a frozen digest exists (mutation rule applies at/after this).
FREEZE_INDEX = STATES.index("PUBLICATION_READY_PENDING_G1")


class PublicationControlError(Exception):
    """Base for all publication-control failures."""


class IllegalTransitionError(PublicationControlError):
    """Raised on any skip or backward jump that is not the mutation reset."""


class EvidenceMissingError(PublicationControlError):
    """Raised when a transition's required evidence is absent/invalid."""


class PublicationDisabledError(PublicationControlError):
    """Raised when entering PUBLICATION_AUTHORIZED/PUBLISHED while disabled."""


class CounselVerificationError(PublicationControlError):
    """Raised when the G1 counsel decision cannot be verified."""


class ByteMismatchError(PublicationControlError):
    """Raised when bytes to publish are not byte-identical to the frozen set."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> float:
    return time.time()


@dataclass
class PublicationStateMachine:
    """Enforces the publication gate sequence for one release."""

    release_id: str
    counsel_verifier: Optional[Callable[[str, str], bool]] = None
    publication_enabled: bool = False

    # --- mutable state (not constructor kwargs) ---
    state: str = field(default=STATES[0], init=False)
    evidence: Dict[str, Any] = field(default_factory=dict, init=False)
    history: List[Dict[str, Any]] = field(default_factory=list, init=False)
    _frozen_bytes: Optional[bytes] = field(default=None, init=False, repr=False)
    _frozen_digest: Optional[str] = field(default=None, init=False, repr=False)
    _g1_decision: Optional[Dict[str, Any]] = field(default=None, init=False, repr=False)
    _published_digest: Optional[str] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # generic machinery
    # ------------------------------------------------------------------
    def _log(self, event: str, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        record = {
            "t": _utc_now(),
            "event": event,
            "from": self.state,
            "reason": reason,
        }
        if extra:
            record.update(extra)
        self.history.append(record)

    def _transition_to(self, target: str, reason: str) -> None:
        """Single-step enforcer: forward exactly one state, nothing else."""
        if target not in STATES:
            raise IllegalTransitionError(f"unknown state: {target!r}")
        expected = STATES.index(self.state) + 1
        if STATES.index(target) != expected:
            raise IllegalTransitionError(
                f"illegal transition {self.state} -> {target}: "
                "forward-only, exactly one step (no jumps, no backward moves)"
            )
        old = self.state
        self.state = target
        self._log("transition", reason, {"from": old, "to": target})

    def _require_evidence(self, name: str, check: Callable[[Any], bool], why: str) -> Any:
        value = self.evidence.get(name)
        if value is None or not check(value):
            raise EvidenceMissingError(
                f"cannot leave {self.state}: missing/invalid evidence {name!r} ({why})"
            )
        return value

    # ------------------------------------------------------------------
    # evidence recording (stays in PRIVATE_BUILD-era flow until gates pass)
    # ------------------------------------------------------------------
    def record_evidence(self, name: str, data: Any) -> None:
        """Record named evidence. Does not advance the state by itself."""
        if not isinstance(name, str) or not name:
            raise ValueError("evidence name must be a non-empty string")
        self.evidence[name] = data
        self._log("evidence_recorded", f"evidence {name!r} recorded")

    # ------------------------------------------------------------------
    # the gated flow (each method: enforce current state, check gate,
    # then take exactly one step)
    # ------------------------------------------------------------------
    def advance_to_tested(self) -> None:
        """PRIVATE_BUILD -> TESTED: requires recorded passing test results."""
        if self.state != "PRIVATE_BUILD":
            raise IllegalTransitionError(f"advance_to_tested() only from PRIVATE_BUILD, now {self.state}")
        self._require_evidence(
            "test_results",
            lambda v: isinstance(v, dict) and v.get("passed") is True,
            "test_results must be a dict with passed=True",
        )
        self._transition_to("TESTED", "test results recorded")

    def advance_to_security_reviewed(self) -> None:
        """TESTED -> SECURITY_REVIEWED: red-team report + clean secret/PII scan."""
        if self.state != "TESTED":
            raise IllegalTransitionError(f"advance_to_security_reviewed() only from TESTED, now {self.state}")
        self._require_evidence(
            "red_team_report",
            lambda v: isinstance(v, dict) and bool(v.get("report")),
            "red_team_report must carry a report reference",
        )
        self._require_evidence(
            "secret_scan",
            lambda v: isinstance(v, dict) and v.get("clean") is True,
            "secret_scan must be a dict with clean=True",
        )
        self._transition_to("SECURITY_REVIEWED", "red-team + clean secret/PII scan")

    def advance_to_privacy_review_ready(self) -> None:
        """SECURITY_REVIEWED -> PRIVACY_REVIEW_READY: privacy analysis approved."""
        if self.state != "SECURITY_REVIEWED":
            raise IllegalTransitionError(
                f"advance_to_privacy_review_ready() only from SECURITY_REVIEWED, now {self.state}"
            )
        self._require_evidence(
            "privacy_analysis",
            lambda v: isinstance(v, dict) and v.get("approved") is True,
            "privacy_analysis must be a dict with approved=True",
        )
        self._transition_to("PRIVACY_REVIEW_READY", "privacy analysis approved")

    def freeze_release(self, release_bytes: bytes) -> str:
        """Freeze the exact bytes counsel will see; generates the packet.

        PRIVACY_REVIEW_READY -> PUBLICATION_READY_PENDING_G1.
        Returns the frozen sha256 digest (the pin everything downstream binds).
        """
        if self.state != "PRIVACY_REVIEW_READY":
            raise IllegalTransitionError(f"freeze_release() only from PRIVACY_REVIEW_READY, now {self.state}")
        if not isinstance(release_bytes, (bytes, bytearray)) or not release_bytes:
            raise ValueError("release_bytes must be non-empty bytes")
        frozen = bytes(release_bytes)
        digest = sha256_hex(frozen)
        self._frozen_bytes = frozen
        self._frozen_digest = digest
        self.evidence["counsel_packet"] = {
            "release_id": self.release_id,
            "artifact_digest": digest,
            "byte_length": len(frozen),
            "generated_t": _utc_now(),
        }
        self._transition_to("PUBLICATION_READY_PENDING_G1", f"release frozen, digest {digest}")
        return digest

    def record_g1_decision(self, approved: bool, decided_by: str, note: str = "") -> None:
        """Record the human G1 decision. Verification happens at advancement."""
        if self.state != "PUBLICATION_READY_PENDING_G1":
            raise IllegalTransitionError(
                f"record_g1_decision() only from PUBLICATION_READY_PENDING_G1, now {self.state}"
            )
        if not approved:
            # A denial is terminal-ish: it cannot advance, so record it and park.
            self._g1_decision = {"approved": False, "decided_by": decided_by, "note": note}
            self._log("g1_decision_denied", f"G1 denied by {decided_by}")
            raise CounselVerificationError("G1 decision was a denial; release cannot advance")
        self._g1_decision = {"approved": True, "decided_by": decided_by, "note": note, "t": _utc_now()}
        self._log("g1_decision_recorded", f"G1 approval recorded by {decided_by} (unverified)")

    def _verify_g1(self) -> None:
        """Verify the recorded G1 approval against the frozen digest."""
        if self._g1_decision is None or not self._g1_decision.get("approved"):
            raise CounselVerificationError("no recorded G1 approval to verify")
        if self.counsel_verifier is None:
            raise CounselVerificationError("no counsel_verifier injected: deny by default")
        try:
            ok = self.counsel_verifier(self.release_id, self._frozen_digest or "")
        except Exception as exc:  # a verifier that explodes denies, never passes
            raise CounselVerificationError(f"counsel verifier raised: {exc}") from exc
        if ok is not True:
            raise CounselVerificationError("counsel decision could not be verified against frozen digest")

    def advance_to_g1_approved(self) -> None:
        """PUBLICATION_READY_PENDING_G1 -> G1_APPROVED: human G1, hash-verified."""
        if self.state != "PUBLICATION_READY_PENDING_G1":
            raise IllegalTransitionError(
                f"advance_to_g1_approved() only from PUBLICATION_READY_PENDING_G1, now {self.state}"
            )
        self._verify_g1()
        self._transition_to("G1_APPROVED", "G1 decision verified against frozen digest")

    def set_publication_enabled(self, enabled: bool) -> None:
        """Flip the hard publication flag (coordinator-controlled). Never advances state."""
        self.publication_enabled = bool(enabled)
        self._log("publication_flag", f"publication_enabled set to {self.publication_enabled}")

    def authorize_publication(self) -> None:
        """G1_APPROVED -> PUBLICATION_AUTHORIZED.

        Requires publication_enabled AND a fresh G1 verification against the
        still-frozen digest. Refuses while disabled or unverifiable.
        """
        if self.state != "G1_APPROVED":
            raise IllegalTransitionError(f"authorize_publication() only from G1_APPROVED, now {self.state}")
        if not self.publication_enabled:
            raise PublicationDisabledError("publication is DISABLED: refusing PUBLICATION_AUTHORIZED")
        self._verify_g1()  # re-verify at authorization time; decision must still bind
        self._transition_to("PUBLICATION_AUTHORIZED", "G1 re-verified, publication enabled")

    def publish(self, release_bytes: bytes) -> Dict[str, Any]:
        """PUBLICATION_AUTHORIZED -> PUBLISHED. Emits ONLY the exact approved bytes.

        Verifies byte-identity with the frozen/G1-approved digest immediately
        before publishing. Any mismatch blocks the publish.
        """
        if self.state != "PUBLICATION_AUTHORIZED":
            raise IllegalTransitionError(f"publish() only from PUBLICATION_AUTHORIZED, now {self.state}")
        if not self.publication_enabled:
            raise PublicationDisabledError("publication is DISABLED: refusing PUBLISHED")
        self._verify_g1()
        if not isinstance(release_bytes, (bytes, bytearray)) or not release_bytes:
            raise ValueError("release_bytes must be non-empty bytes")
        digest = sha256_hex(bytes(release_bytes))
        if digest != self._frozen_digest:
            raise ByteMismatchError(
                "publish blocked: bytes are not byte-identical to the frozen "
                f"G1-approved digest (got {digest}, frozen {self._frozen_digest})"
            )
        self._published_digest = digest
        self._transition_to("PUBLISHED", f"published exact approved bytes, digest {digest}")
        return {
            "release_id": self.release_id,
            "artifact_digest": digest,
            "byte_length": len(release_bytes),
            "published_t": _utc_now(),
            "state": "PUBLISHED",
        }

    # ------------------------------------------------------------------
    # mutation guard
    # ------------------------------------------------------------------
    def check_mutation(self, current_bytes: Optional[bytes]) -> bool:
        """Detect post-freeze byte changes.

        If the current bytes no longer hash to the frozen digest, the release
        has mutated after freezing: reset to PRIVACY_REVIEW_READY, discard the
        frozen pin and everything downstream of it (counsel packet, G1
        decision). Returns True when a reset happened.
        """
        if self._frozen_digest is None:
            return False  # nothing frozen yet: changes are ordinary build edits
        if STATES.index(self.state) < FREEZE_INDEX:
            return False  # cannot be post-freeze before the freeze state
        if current_bytes is None:
            return False
        if sha256_hex(bytes(current_bytes)) == self._frozen_digest:
            return False
        old_state = self.state
        old_digest = self._frozen_digest
        self._frozen_bytes = None
        self._frozen_digest = None
        self._g1_decision = None
        self.evidence.pop("counsel_packet", None)
        self.state = "PRIVACY_REVIEW_READY"
        self._log(
            "mutation_reset",
            "release bytes changed after freeze; reset to PRIVACY_REVIEW_READY, "
            "frozen pin + counsel packet + G1 decision discarded",
            {"from": old_state, "to": "PRIVACY_REVIEW_READY", "old_digest": old_digest},
        )
        return True

    # ------------------------------------------------------------------
    # introspection / persistence support for integration
    # ------------------------------------------------------------------
    @property
    def frozen_digest(self) -> Optional[str]:
        return self._frozen_digest

    @property
    def is_published(self) -> bool:
        return self.state == "PUBLISHED"

    def snapshot(self) -> Dict[str, Any]:
        """JSON-serializable state for the coordinator's persistent store.

        Note: the injected counsel_verifier is NOT serializable and is NOT
        included; restore() requires it to be re-injected.
        """
        return {
            "release_id": self.release_id,
            "state": self.state,
            "publication_enabled": self.publication_enabled,
            "evidence": self.evidence,
            "frozen_digest": self._frozen_digest,
            "frozen_byte_length": len(self._frozen_bytes) if self._frozen_bytes else 0,
            "g1_decision": self._g1_decision,
            "published_digest": self._published_digest,
            "history": self.history,
        }

    @classmethod
    def restore(
        cls,
        snap: Dict[str, Any],
        counsel_verifier: Optional[Callable[[str, str], bool]] = None,
    ) -> "PublicationStateMachine":
        m = cls(
            release_id=snap["release_id"],
            counsel_verifier=counsel_verifier,
            publication_enabled=snap.get("publication_enabled", False),
        )
        m.state = snap["state"]
        m.evidence = dict(snap.get("evidence", {}))
        m._frozen_digest = snap.get("frozen_digest")
        m._g1_decision = snap.get("g1_decision")
        m._published_digest = snap.get("published_digest")
        m.history = list(snap.get("history", []))
        m._log("restored", f"machine restored from snapshot at {m.state}")
        return m
