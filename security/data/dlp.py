"""Data-loss-prevention policy: which sensitivities may travel where.

Fail closed throughout: unknown destinations deny, and any sensitivity /
destination pair without an explicit allow rule denies.
"""
from __future__ import annotations

from .classifier import Sensitivity

KNOWN_DESTINATIONS = frozenset({
    "PROMPT",
    "INTERNAL_LOG",
    "EXTERNAL_API",
    "BROWSER_FORM",
    "SECURITY_LEDGER",
    "ARTIFACT",
})

_REDACTION_CONTEXT_KEY = "redacted"
_PROMPT_JUSTIFICATION_KEY = "pii_in_prompt_justified"
_CREDENTIAL_ACTION_KEY = "credential_action_approved"
_HASHED_CONTEXT_KEY = "hashed"


def _deny(destination, sensitivity, reason):
    return (False, ["%s %s: %s" % (sensitivity.name, destination, reason)])


def _may_travel_one(sensitivity, destination, context):
    """Core policy. Returns (allowed, reasons)."""
    sensitivity = Sensitivity(sensitivity)
    context = context or {}

    if destination not in KNOWN_DESTINATIONS:
        return (False, ["destination %r is unknown: fail closed - travel denied"
                        % (destination,)])

    # Credentials are the most restricted flow: they may never travel to
    # prompts or internal logs. Two narrow, explicitly-approved exceptions
    # exist, each requiring its flag to be explicitly True (truthy is not
    # enough): the approved browser form-fill path, and the hashed security
    # ledger. Everything else denies.
    if sensitivity is Sensitivity.CREDENTIAL:
        if destination == "BROWSER_FORM":
            if context.get(_CREDENTIAL_ACTION_KEY) is True:
                return (True, ["CREDENTIAL to BROWSER_FORM with explicit "
                               "credential_action_approved"])
            return _deny(destination, sensitivity,
                         "CREDENTIAL to BROWSER_FORM requires "
                         "credential_action_approved=True in context")
        if destination == "SECURITY_LEDGER":
            if context.get(_HASHED_CONTEXT_KEY) is True:
                return (True, ["CREDENTIAL to SECURITY_LEDGER hashed"])
            return _deny(destination, sensitivity,
                         "CREDENTIAL to SECURITY_LEDGER requires hashed=True "
                         "in context")
        return _deny(destination, sensitivity,
                     "CREDENTIAL may never travel outside the trust boundary")

    if sensitivity is Sensitivity.PII:
        if destination == "PROMPT":
            if context.get(_PROMPT_JUSTIFICATION_KEY) is True:
                return (True, ["PII in prompt justified by explicit context flag"])
            return _deny(destination, sensitivity,
                         "PII to PROMPT requires %s=True in context"
                         % _PROMPT_JUSTIFICATION_KEY)
        if destination == "BROWSER_FORM":
            # Approved fill path; credential actions carry their own approval.
            return (True, ["PII allowed to the approved form-fill path"])
        if destination == "INTERNAL_LOG":
            if context.get(_REDACTION_CONTEXT_KEY) is True:
                return (True, ["PII logged only after redaction"])
            return _deny(destination, sensitivity,
                         "PII to INTERNAL_LOG requires redacted=True in context")
        return _deny(destination, sensitivity,
                     "no allow rule for PII to %s: fail closed" % destination)

    if sensitivity is Sensitivity.FINANCIAL:
        if destination == "INTERNAL_LOG":
            if context.get(_REDACTION_CONTEXT_KEY) is True:
                return (True, ["FINANCIAL logged only after redaction"])
            return _deny(destination, sensitivity,
                         "FINANCIAL to INTERNAL_LOG requires redacted=True in context")
        return _deny(destination, sensitivity,
                     "no allow rule for FINANCIAL to %s: fail closed" % destination)

    if sensitivity in (Sensitivity.INTERNAL, Sensitivity.PUBLIC):
        if destination == "EXTERNAL_API":
            # Even INTERNAL data needs an explicit rule before leaving the org.
            return _deny(destination, sensitivity,
                         "no allow rule for %s to EXTERNAL_API: fail closed"
                         % sensitivity.name)
        return (True, ["%s allowed to %s" % (sensitivity.name, destination)])

    return _deny(destination, sensitivity, "fail closed: no applicable allow rule")


def may_travel(sensitivity, destination, context=None):
    """Decide one sensitivity -> destination flow.

    Returns ``(allowed, reasons)``; ``reasons`` lists the violations when
    denied.  Unknown destinations and unruled pairs deny (fail closed).
    """
    return _may_travel_one(sensitivity, destination, context)


def check(fields, destination, context=None):
    """Check a batch of ``{field_name: Sensitivity}`` against a destination.

    Returns a list of violation strings; an empty list means allowed.
    """
    violations = []
    for field, sensitivity in (fields or {}).items():
        allowed, reasons = _may_travel_one(sensitivity, destination, context)
        if not allowed:
            violations.extend("field %r: %s" % (field, reason) for reason in reasons)
    return violations


__all__ = ["KNOWN_DESTINATIONS", "may_travel", "check"]
