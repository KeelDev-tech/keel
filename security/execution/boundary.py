"""A trusted fixed handler receives only validated immutable bytes, never worker secrets."""
from __future__ import annotations
from dataclasses import dataclass
import time
from types import MappingProxyType
from collections.abc import Callable, Mapping
from .envelope import ActionEnvelope, ALLOWED_ACTIONS, InvalidRequest, identifier
from .host import HostAdapter, Reservation, Outcome, Denied, Unbound

@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    code: str
    envelope_digest: str = ""
    evidence_id: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "code": self.code,
                "envelope_digest": self.envelope_digest, "evidence_id": self.evidence_id}

class Boundary:
    def __init__(self, host: HostAdapter | None = None,
                 handlers: Mapping[str, Callable[[ActionEnvelope], Outcome]] | None = None,
                 clock: Callable[[], float] = time.time):
        self._host = host if host is not None else HostAdapter()
        supplied = dict(handlers or {})
        if any(action not in ALLOWED_ACTIONS or not callable(handler)
               for action, handler in supplied.items()):
            raise ValueError("invalid_fixed_handler")
        self._handlers = MappingProxyType(supplied)
        self._clock = clock

    def _unknown(self, reservation, envelope, code):
        try:
            self._host.note_unknown(reservation, envelope, int(self._clock()))
        except Exception:
            pass
        return ExecutionResult("unknown", code, envelope.digest)

    def execute(self, envelope: ActionEnvelope) -> ExecutionResult:
        # Reparse even a locally constructed dataclass: only validated wire values
        # enter host authority and handlers. No subclass-provided digest is trusted.
        try:
            if type(envelope) is not ActionEnvelope:
                raise InvalidRequest("invalid_envelope")
            envelope = ActionEnvelope.from_request(envelope.to_request(), actor=envelope.actor,
                                                   now=int(self._clock()))
        except (InvalidRequest, TypeError, ValueError, AttributeError, OverflowError):
            return ExecutionResult("held", "invalid_envelope")
        digest = envelope.digest
        handler = self._handlers.get(envelope.action)
        if handler is None:
            return ExecutionResult("held", "handler_unbound", digest)
        try:
            reservation = self._host.reserve(envelope, int(self._clock()))
        except Unbound:
            return ExecutionResult("held", "host_unbound", digest)
        except Denied:
            return ExecutionResult("held", "authority_denied", digest)
        except Exception:
            return ExecutionResult("held", "reservation_unconfirmed", digest)
        if (type(reservation) is not Reservation or reservation.envelope_digest != digest
                or reservation.attempt_id != envelope.attempt_id):
            return ExecutionResult("held", "reservation_mismatch", digest)
        try:
            identifier(reservation.reservation_id)
        except InvalidRequest:
            return ExecutionResult("held", "reservation_mismatch", digest)
        try:
            approved_digest = self._host.begin_dispatch(reservation, envelope, int(self._clock()))
        except (Denied, Unbound):
            return ExecutionResult("held", "dispatch_denied", digest)
        except Exception:
            return self._unknown(reservation, envelope, "dispatch_commit_unconfirmed")
        if approved_digest != digest:
            return self._unknown(reservation, envelope, "dispatch_binding_mismatch")
        # From here any exception or missing evidence is UNKNOWN, never retryable.
        try:
            outcome = handler(envelope)
            if type(outcome) is not Outcome or outcome.status not in {"submitted", "not_submitted"}:
                raise ValueError("invalid_handler_outcome")
            identifier(outcome.evidence_id)
            self._host.record_outcome(reservation, envelope, outcome, int(self._clock()))
        except Exception:
            return self._unknown(reservation, envelope, "dispatch_outcome_unconfirmed")
        return ExecutionResult(outcome.status, "outcome_recorded", digest, outcome.evidence_id)
