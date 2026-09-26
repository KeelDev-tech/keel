"""Deterministic risk scoring for the policy engine.

Evidence-based, formulaic, no LLM: base risk by action class, adjusted by
resource sensitivity, injection severity, and delegation hops. The score is
an input to the ledger and to a hard ceiling (risk_deny_threshold) — it
never *permits* anything on its own.
"""

from __future__ import annotations

from ..actions.classifier import ActionClass

_CLASS_BASE = {
    ActionClass.READ_ONLY: 5,
    ActionClass.REVERSIBLE_WRITE: 20,
    ActionClass.EXTERNAL_COMMUNICATION: 45,
    ActionClass.CREDENTIAL: 60,
    ActionClass.FINANCIAL: 65,
    ActionClass.SUBMISSION: 40,
    ActionClass.SYSTEM_CHANGE: 70,
    # 75: below the 85 risk ceiling on its own, so a human approval can
    # still permit a plain destructive action; combined with CREDENTIAL
    # (+25) or HIGH injection (+30) it hits the ceiling and denies.
    ActionClass.DESTRUCTIVE: 75,
}

_SENSITIVITY_DELTA = {
    "PUBLIC": 0,
    "INTERNAL": 5,
    "PII": 15,
    "FINANCIAL": 20,
    "CREDENTIAL": 25,
}

_INJECTION_DELTA = {"LOW": 5, "MEDIUM": 15, "HIGH": 30}


def risk_score(action_class: ActionClass,
               resource_sensitivity: str = "INTERNAL",
               injection_severity: str | None = None,
               delegated: bool = False) -> tuple[int, list[str]]:
    """Return (score 0-100, factor list). Fully deterministic."""
    factors: list[str] = []
    score = _CLASS_BASE.get(action_class, 45)
    factors.append(f"class_base:{action_class.value}={score}")
    sens = (resource_sensitivity or "INTERNAL").upper()
    d = _SENSITIVITY_DELTA.get(sens, 5)
    score += d
    factors.append(f"sensitivity:{sens}=+{d}")
    if injection_severity:
        sev = injection_severity.upper()
        d = _INJECTION_DELTA.get(sev, 0)
        score += d
        factors.append(f"injection:{sev}=+{d}")
    if delegated:
        score += 10
        factors.append("delegation_hop=+10")
    return min(score, 100), factors
