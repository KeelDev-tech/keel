#!/usr/bin/env python3
"""API-direct write-transport retirement policy (Keel Blocker Resolution
Directive, section 1 — Workstream A, Trent-authorized 2026-09-18).

API-direct is RETIRED as a write transport across keel. Read uses stay
open; every external write attempt consults this module and is DENIED.

Required policy (implemented exactly):
    api_direct:
      lifecycle: RETIRED_WRITE_PATH
      discovery: ALLOW
      inspection: ALLOW
      preparation: ALLOW
      dry_run: ALLOW
      external_side_effects: DENY
      submission: DENY
      retry_after_ambiguous_attempt: DENY

Enforcement shape:
  - check(purpose) is the single consult point. ALLOWED purposes return
    True; DENIED purposes raise ApiDirectRetired; an UNKNOWN purpose also
    raises (fail closed — a purpose that is not on the ALLOW list is not
    permitted).
  - retry_after_ambiguous() raises unconditionally. An ambiguous attempt
    (a POST that may already have been accepted by the provider) must
    NEVER be retried, and it must NEVER fall through to another transport
    — a browser "fallback" after an ambiguous API-direct attempt is a
    second submission attempt, i.e. the duplicate-application class the
    F22 submit-intent lane exists to prevent.
  - ats_matrix.capability(board, "api_direct_supported") consults this
    policy and can never report True while the lifecycle is
    RETIRED_WRITE_PATH — the write claim is retired, whatever the stored
    evidence says.
  - submit_intent.record_intent refuses transport "api"/"api-direct" at
    intent-mint time (see its own docstring): no new attempt identity is
    ever minted for the retired lane.

Preservation (what this module does NOT do):
  - It does not delete, alter, or reinterpret any historical API-direct
    telemetry, outcome history, compatibility adapters, provider
    intelligence, or forensic evidence. Historical outcomes stay as
    recorded; canonical attempt state stays authoritative; UNKNOWN stays
    UNKNOWN until reconciled on provider-correlated evidence.

This module has no imports outside the stdlib (nothing at all, in fact):
it cannot be broken by a sibling's import cycle, and it is importable
from both library and CLI contexts without side effects.
"""

POLICY = {
    "lifecycle": "RETIRED_WRITE_PATH",
    "discovery": "ALLOW",
    "inspection": "ALLOW",
    "preparation": "ALLOW",
    "dry_run": "ALLOW",
    "external_side_effects": "DENY",
    "submission": "DENY",
    "retry_after_ambiguous_attempt": "DENY",
}

ALLOWED_PURPOSES = ("discovery", "inspection", "preparation", "dry_run")
DENIED_PURPOSES = (
    "external_side_effects",
    "submission",
    "retry_after_ambiguous_attempt",
)


class ApiDirectRetired(RuntimeError):
    """Raised when API-direct is invoked for any write purpose.

    Subclasses RuntimeError so existing broad `except RuntimeError`
    handlers around the submission path fail closed rather than miss it.
    The message always names the denied purpose and the directive.
    """


def check(purpose):
    """Consult the retirement policy for one purpose.

    Returns True for ALLOW-listed purposes (discovery, inspection,
    preparation, dry_run). Raises ApiDirectRetired for DENY-listed
    purposes (external_side_effects, submission,
    retry_after_ambiguous_attempt) and for any unknown purpose
    (fail closed: not on the ALLOW list means not permitted).
    """
    p = (purpose or "").strip().lower()
    if p in ALLOWED_PURPOSES:
        return True
    if p in DENIED_PURPOSES:
        raise ApiDirectRetired(
            f"api_direct {p} is DENY under lifecycle {POLICY['lifecycle']} "
            f"(Keel Blocker Resolution Directive §1, Workstream A "
            f"2026-09-18) — the API-direct write transport is retired")
    raise ApiDirectRetired(
        f"api_direct purpose {purpose!r} is not on the ALLOW list — "
        f"fail closed under lifecycle {POLICY['lifecycle']}")


def describe():
    """The full policy as a plain dict (for telemetry / shadow checks)."""
    return dict(POLICY)


def retired():
    """True while the API-direct write path is retired (policy consult)."""
    return POLICY.get("lifecycle") == "RETIRED_WRITE_PATH"


def retry_after_ambiguous(*, attempt_id=None, role_id=None,
                          proposed_transport=None):
    """Ambiguous-attempt retry entry point — ALWAYS raises.

    An attempt whose outcome is ambiguous (the POST may already have been
    accepted) must NEVER be retried, and must NEVER fall through to
    another transport: issuing a *different* transport as a "fallback"
    for an ambiguous attempt is a second submission attempt and risks
    the duplicate application. The attempt stays UNKNOWN in the
    canonical submit-intent store until reconciled on
    provider-correlated evidence; that path never issues a POST and never
    launches a browser.

    Every argument is accepted and ignored except for auditability in
    the raised message. No transport is returned; nothing is started.
    """
    raise ApiDirectRetired(
        f"retry_after_ambiguous_attempt is DENY under lifecycle "
        f"{POLICY['lifecycle']} (attempt_id={attempt_id!r} "
        f"role_id={role_id!r} proposed_transport={proposed_transport!r}): "
        f"no retry is issued, no fallback transport is engaged — reconcile "
        f"the attempt on provider-correlated evidence instead")


# ---------------------------------------------------------------- registry
# Every API-direct WRITE entry point known in the keel tree. The actual
# HTTP submission implementation (api_submit.py / greenhouse_direct.py)
# lives in the private execution layer, outside the keel tree; it is
# retired by the same directive and must consult this policy (or an
# equivalent gate) before any external write. Within the keel tree, the
# retired write surface is the capability advertisement and the attempt-
# identity gate for the API lane. enumerate + assert below prove zero
# write routes survive.

WRITE_ENTRY_POINTS = (
    ("ats_matrix.capability(board, 'api_direct_supported')",
     "capability advertisement for the retired write transport — "
     "must never report True"),
    ("submit_intent.record_intent(transport='api'|'api-direct')",
     "attempt-identity mint for the retired API lane — must refuse"),
    ("api_direct_policy.retry_after_ambiguous()",
     "ambiguous-attempt retry/fallback path — must raise, no fall-through"),
)


def list_write_entry_points():
    """The registered write entry points (name, write-surface description)."""
    return list(WRITE_ENTRY_POINTS)


def assert_all_write_routes_denied():
    """Prove zero write routes survive: exercise every registered entry
    point and require DENY.

    Returns the number of entry points asserted. Raises AssertionError
    (with the surviving route named) when any route does not deny —
    a regression, not a test artifact.
    """
    import ats_matrix  # noqa: E402 — function-level: policy imports nothing
    import submit_intent  # noqa: E402 — at module scope; direction is one-way

    denied = 0

    # 1. capability advertisement must never claim write support.
    if retired():
        for board in ("greenhouse", "lever", "workday", "ashby"):
            if ats_matrix.capability(board, "api_direct_supported"):
                raise AssertionError(
                    "api_direct_supported still advertised True for "
                    f"board {board!r} — the retired write claim survives")
        denied += 1

    # 2. submission-purpose consult must deny.
    try:
        check("submission")
    except ApiDirectRetired:
        denied += 1
    else:
        raise AssertionError(
            "check('submission') did not raise — write route survives")

    # 3. record_intent must refuse the retired API-lane transport.
    refused = 0
    for transport in ("api", "api-direct"):
        try:
            submit_intent.record_intent(
                "rt-would-be-api-lane", "Audit Probe Co",
                transport, "digest-probe")
        except ApiDirectRetired:
            refused += 1
        except Exception as e:  # fail closed: any refusal counts, but
            # nothing may escape — a wrong exception type is a leak too.
            raise AssertionError(
                f"record_intent(transport={transport!r}) escaped with "
                f"{type(e).__name__} instead of ApiDirectRetired") from e
    if refused != 2:
        raise AssertionError(
            "record_intent accepted the retired API-lane transport — "
            "write route survives")
    denied += 1

    # 4. ambiguous-attempt retry must raise and never issue a fallback.
    for proposed in ("browser", "api-direct", "manual", None):
        try:
            retry_after_ambiguous(attempt_id="si-probe", role_id="rt-probe",
                                  proposed_transport=proposed)
        except ApiDirectRetired:
            pass
        else:
            raise AssertionError(
                f"retry_after_ambiguous(proposed_transport={proposed!r}) "
                f"did not raise — fall-through survives")
    denied += 1

    return denied


if __name__ == "__main__":
    import importlib
    import json
    # Delegate to the CANONICAL module object: when this file runs as
    # __main__ it is a second copy of itself, and submit_intent's
    # `import api_direct_policy` resolves to the canonical one — the
    # exception identity would differ and every refusal would look like
    # an escape. Re-importing canonically avoids the __main__ trap.
    mod = importlib.import_module("api_direct_policy")
    print(json.dumps(mod.describe(), indent=1))
    n = mod.assert_all_write_routes_denied()
    print(f"write routes denied: {n}/{len(mod.WRITE_ENTRY_POINTS) + 1} "
          f"(registry covers {len(mod.WRITE_ENTRY_POINTS)} entry points)")
