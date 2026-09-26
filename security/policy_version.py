"""Keel security subsystem — policy version.

Every policy evaluation stamps this version into the security ledger so a
decision can always be traced back to the exact rule set that produced it.
Bump POLICY_VERSION whenever rules/base_rules.json changes.
"""

POLICY_VERSION = "2026-09-21.1"
POLICY_ISSUED = "2026-09-21"
