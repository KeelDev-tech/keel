"""Fail-closed protected execution boundary; production host integration is unbound."""
from .envelope import ActionEnvelope, Attachment, InvalidRequest
from .host import HostAdapter, Reservation, Outcome, Denied, Unbound
from .boundary import Boundary, ExecutionResult
__all__ = ["ActionEnvelope", "Attachment", "InvalidRequest", "HostAdapter", "Reservation",
           "Outcome", "Denied", "Unbound", "Boundary", "ExecutionResult"]
