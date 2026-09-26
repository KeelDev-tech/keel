"""Keel security authority — Phase 1.

A separate security AUTHORITY over the agent ecosystem, not a peer agent.
Deterministic code is the security boundary; LLMs may analyze and
summarize but never permit, deny, or approve.

Enforcement flow:
    AGENT -> ACTION REQUEST -> IDENTITY/CAPABILITY -> INJECTION CHECK
      -> DATA/SECRET CHECK -> POLICY ENGINE -> RISK
      -> ALLOW | DENY | QUARANTINE | REQUIRE_APPROVAL
      -> EXECUTION -> RESULT VALIDATION -> SECURITY LEDGER
"""

__version__ = "1.0.0-phase1"
