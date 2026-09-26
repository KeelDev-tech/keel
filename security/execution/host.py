"""Host-owned authority interface. The exported production default authorizes nothing."""
from dataclasses import dataclass
from .envelope import ActionEnvelope

class Unbound(RuntimeError):
    pass

class Denied(RuntimeError):
    """Only raise before the operation being denied can occur."""

@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    envelope_digest: str
    attempt_id: str

@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    evidence_id: str

class HostAdapter:
    """Implement in the real host, against existing canonical stores, not another journal.

    reserve atomically validates exact stored approval, actor/role/attempt, nonce,
    policy revision, revocation, consent, absolute 429 hold, lease, intent state,
    destination/content and expiry, and durably reserves the one existing attempt.
    Concurrent/replayed reservation must never authorize another dispatch.

    begin_dispatch repeats all mutable checks and commits a durable UNKNOWN state
    BEFORE returning. Restart must preserve this state and forbid automatic resend.
    Its returned digest must be the exact stored approval digest.

    record_outcome stores the real observed result using host evidence validation.
    note_unknown is idempotent, preserves existing verified terminal evidence and
    must never release a submission lease or erase an unresolved dispatch.
    """
    def reserve(self, envelope: ActionEnvelope, now: int) -> Reservation:
        raise Unbound("host_reservation_unbound")

    def begin_dispatch(self, reservation: Reservation, envelope: ActionEnvelope, now: int) -> str:
        raise Unbound("host_dispatch_unbound")

    def record_outcome(self, reservation: Reservation, envelope: ActionEnvelope,
                       outcome: Outcome, now: int) -> None:
        raise Unbound("host_outcome_unbound")

    def note_unknown(self, reservation: Reservation, envelope: ActionEnvelope, now: int) -> None:
        raise Unbound("host_unknown_unbound")
