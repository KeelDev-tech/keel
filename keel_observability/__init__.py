"""Local, host-authenticated observation persistence; no network dependencies."""
from .store import (ObservationError, ObservationStore, Verification, canonical_event,
                    event_digest, validate_event)

__all__ = ["ObservationError", "ObservationStore", "Verification", "canonical_event", "event_digest", "validate_event"]

from .auth import HMACProducerVerifier, ProducerKey, sign_context
__all__ += ["HMACProducerVerifier", "ProducerKey", "sign_context"]
