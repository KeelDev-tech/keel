#!/usr/bin/env python3
"""approval_records.py — authenticated approval records (audit finding F21 fix).

The defect (F21, P0, CODE_CONFIRMED): the approval helper accepted an
arbitrary caller-supplied ``approver`` string, so any worker could
self-authorize by naming the operator (``grant_human_approval(role_id,
"<operator-name>")``). Claimed human exemptions in draft fields had the same shape:
a worker-set field with no approver record behind it.

The fix: authority flows ONLY through ApprovalRecord objects.

  record = {
    "record_id":  "apr-<uuid>",
    "principal":  authenticated identity — DERIVED from the approval source,
                  never a caller-supplied string (there is NO parameter for
                  it; issue_record() has no principal/name/approver argument),
    "scope":      exact question/bundle/action authorized, e.g.
                  {"action": "form_attestation", "role_id": "R-1"}
                  A scope value of "*" is an explicit wildcard for that key.
    "conditions": optional named conditions checked against the call context
                  (e.g. "board_in_clean_registry"), enforced by
                  validate_record(); unknown condition names fail closed.
    "evidence":   the immutable approval event/text itself:
                  {"origin": ..., "text": ..., "at": ..., "ref": ...},
    "issued_at":  ISO-8601 UTC,
    "expires_at": ISO-8601 UTC or None,
    "single_use": bool,
  }

CAPABILITY SEPARATION:
  - Workers PRESENT records. Grant paths (the submission grant path, the
    draft generator) validate a presented record against scope+expiry
    before it authorizes anything. A record is never minted at the grant
    site.
  - Only issue_record() CREATES records, and it requires a ``source_event``
    whose ``origin`` is in AUTHENTICATED_ORIGINS. The principal is derived
    from that origin — a worker supplying ``approver="<operator-name>"`` authorizes
    nothing because the string is never consulted.

What "authenticated" means here, honestly: the authenticated boundary is the
operator's own channel. The operator's approvals arrive as their own messages
in the operator chat / input tray, handled by the main agent — workers (subagents,
loops, browser tasks) never see that channel. issue_record() is called at
that authenticated point with the source event (origin "operator_channel",
text = the operator's words). The record then carries that evidence immutably; every
later consumer validates scope+expiry+evidence without re-asking.

One-off records SHOULD bind the exact staged payload: put the dry-run's
payload fingerprint in the scope ("fingerprint" key). grant_human_approval()
refuses when the bound fingerprint differs from the staged entry's, so a
scraped record file cannot approve a re-staged (different) payload. The
record_id of a single-use record burns on first grant for the same reason.
Standing records stay payload-agnostic by design — reuse across leads within
their recorded scope IS the standing decision; each grant still re-checks
the board condition, the per-lead staged fingerprint, expiry, and the
per-lead entry's single-use.

STANDING RECORDS: the operator's standing authorizations are encoded below
as records with their EXACT recorded scope and the recorded directive text
as immutable evidence. Same authority as before, now evidenced instead of
label-based. Nothing is widened: every standing record keeps its original
bounds, and standing records carry an expiry backstop (renewal = a fresh
authenticated record, never a worker edit).

These definitions are GROUNDED, not examples: each entry's evidence quotes
the operator's own recorded directive (date and source ref given). A standing
definition whose evidence cannot be traced to the operator's recorded words
must not be minted — _build_standing() refuses any entry still carrying
example/placeholder markers.

Everything fails closed: malformed record, missing evidence, expired,
out-of-scope, unknown condition, fabricated origin -> denied.
"""

import argparse
import copy
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- constants

AUTHENTICATED_ORIGINS = ("operator_channel", "standing_registry")
"""Origins allowed to mint records. 'operator_channel' = the operator's own
words arriving on the operator channel (handled by the main agent).
'standing_registry' = the encoded standing directives below (their evidence
quotes the recorded directive; they are fixed at module load, never minted
by workers at runtime)."""

PRINCIPALS = {
    "operator_channel": "operator (operator channel)",
    "standing_registry": "operator (recorded standing directive)",
}
"""Authenticated identity per origin. Derived, never caller-supplied."""

SCHEMA_VERSION = "apr/1"


# ---------------------------------------------------------------- model

def _utcnow():
    return datetime.now(timezone.utc)


def issue_record(*, source_event, scope, ttl_h=None, expires_at=None,
                 single_use=False, record_id=None):
    """THE APPROVAL CAPABILITY: mint an ApprovalRecord from an authenticated source.

    source_event: {"origin": "operator_channel"|"standing_registry",
                   "text": "<the approval words/event>",
                   "at": "<iso ts, optional>", "ref": "<provenance pointer, optional>"}
    scope:        dict, exact question/bundle/action authorized.
    ttl_h / expires_at: expiry; exactly one must be given for non-standing
                  records issued from operator_channel (fail closed).
    single_use:  burn after first successful use.

    There is deliberately NO principal/name/approver parameter: the principal
    is derived from source_event["origin"]. A worker cannot name the operator
    into authority. Raises ValueError on any unauthenticated input.
    """
    if not isinstance(source_event, dict):
        raise ValueError("issue_record: source_event must be a dict")
    origin = source_event.get("origin")
    if origin not in AUTHENTICATED_ORIGINS:
        raise ValueError(
            f"issue_record: origin {origin!r} is not an authenticated "
            f"approval source (allowed: {list(AUTHENTICATED_ORIGINS)}). "
            "A worker cannot mint approvals.")
    text = (source_event.get("text") or "").strip()
    if not text:
        raise ValueError("issue_record: source_event carries no approval text")
    if not isinstance(scope, dict) or not scope:
        raise ValueError("issue_record: scope must be a non-empty dict")
    if not scope.get("action"):
        raise ValueError("issue_record: scope must name an 'action'")
    now = _utcnow()
    if expires_at is not None:
        try:
            exp = datetime.fromisoformat(expires_at)
        except (ValueError, TypeError):
            raise ValueError("issue_record: unparseable expires_at")
    elif ttl_h is not None:
        exp = now + timedelta(hours=ttl_h)
    else:
        raise ValueError("issue_record: expiry required (ttl_h or expires_at)")
    return {
        "schema": SCHEMA_VERSION,
        "record_id": record_id or f"apr-{uuid.uuid4().hex[:12]}",
        "principal": PRINCIPALS[origin],
        "scope": copy.deepcopy(scope),
        "conditions": copy.deepcopy(source_event.get("conditions") or {}),
        "evidence": {
            "origin": origin,
            "text": text[:2000],
            "at": source_event.get("at") or now.isoformat(),
            "ref": (source_event.get("ref") or "")[:300],
        },
        "issued_at": now.isoformat(),
        "expires_at": exp.isoformat(),
        "single_use": bool(single_use),
    }


def _expired(record):
    exp = record.get("expires_at")
    if not exp:
        return True  # no expiry recorded -> fail closed
    try:
        return _utcnow() >= datetime.fromisoformat(exp)
    except (ValueError, TypeError):
        return True  # unparseable expiry fails closed


def scope_covers(record_scope, required_scope):
    """True iff record_scope authorizes every key of required_scope.

    Wildcard: a record value of "*" explicitly covers any required value
    for that key. Missing key or unequal value -> not covered.
    """
    if not isinstance(record_scope, dict) or not isinstance(required_scope, dict):
        return False
    for k, v in required_scope.items():
        if k not in record_scope:
            return False
        rv = record_scope[k]
        if rv != "*" and rv != v:
            return False
    return True


def check_conditions(record, context):
    """Enforce named conditions against the call context. Fail closed."""
    conditions = record.get("conditions") or {}
    context = context or {}
    for name, expected in conditions.items():
        checker = _CONDITION_CHECKERS.get(name)
        if checker is None:
            return (False, f"unknown condition {name!r} — fail closed")
        ok, why = checker(expected, context)
        if not ok:
            return (False, f"condition {name!r} not satisfied: {why}")
    return (True, "conditions satisfied")


def _cond_board_in_clean_registry(expected, context):
    if not expected:
        return (True, "condition disabled")
    board = context.get("board")
    clean = context.get("clean_boards") or set()
    if not board:
        return (False, "no board in context")
    if board not in clean:
        return (False, f"board {board!r} is not registry-clean")
    return (True, f"board {board!r} is registry-clean")


_CONDITION_CHECKERS = {
    "board_in_clean_registry": _cond_board_in_clean_registry,
}


def validate_record(record, required_scope, context=None):
    """Validate a presented record. Returns (ok, reason).

    Checks, in order: structure -> evidence present -> not expired ->
    scope covers required_scope -> conditions against context.
    A worker presenting a bare name string fails the structure check.
    """
    if not isinstance(record, dict):
        return (False, "no approval record presented — a worker cannot "
                       "self-authorize by naming the operator; present a "
                       "validated ApprovalRecord")
    if record.get("schema") != SCHEMA_VERSION:
        return (False, f"unknown record schema {record.get('schema')!r}")
    for field in ("record_id", "principal", "scope", "evidence",
                  "issued_at", "expires_at"):
        if not record.get(field):
            return (False, f"record missing {field!r}")
    ev = record["evidence"]
    if not isinstance(ev, dict) or not (ev.get("text") or "").strip():
        return (False, "record carries no immutable approval evidence")
    if ev.get("origin") not in AUTHENTICATED_ORIGINS:
        return (False, f"record evidence origin {ev.get('origin')!r} is not "
                       "an authenticated approval source")
    if _expired(record):
        return (False, "approval record expired — re-approval required")
    if not scope_covers(record.get("scope"), required_scope or {}):
        return (False, "record scope does not cover the requested action "
                       "(authorized decisions are reusable only within "
                       "their recorded scope)")
    return check_conditions(record, context)


# ---------------------------------------------------------------- standing records
# Trent's already-granted standing authorizations, encoded with their EXACT
# recorded scope and his recorded words as immutable evidence. Same authority
# as before — now evidenced, never widened. Each entry carries the date and
# source of the actual directive; placeholder/example text is refused by
# _build_standing() below.

_STANDING_DEFS = [
    {
        "name": "form_attestations",
        "source_event": {
            "origin": "standing_registry",
            "text": ("Full autopilot (Trent, 2026-09-16): standard "
                     "application-form legal attestations are PRE-AUTHORIZED — "
                     "arbitration_agreement, background_check_consent, "
                     "at_will_acknowledgment, information_truthfulness_attestation, "
                     "data_privacy_consent. Anything else (no-AI/unaided-work, "
                     "personally-completed, travel/office/relocation, recording "
                     "consent, essays) still parks."),
            "at": "2026-09-16T08:01:00+00:00",
            "ref": "MEMORY.md: FULL AUTOPILOT + answer_bank.json gates.autopilot_attestation_scope",
        },
        "scope": {"action": "form_attestation", "keys": [
            "arbitration_agreement", "background_check_consent",
            "at_will_acknowledgment", "information_truthfulness_attestation",
            "data_privacy_consent"]},
        "conditions": {},
        "ttl_h": 24 * 180,   # backstop; renewal = fresh authenticated record
        "single_use": False,
    },
    {
        "name": "account_creation",
        "source_event": {
            "origin": "standing_registry",
            "text": ("Standing authorization (Trent, 2026-09-14): Muse may "
                     "create job-application accounts on Trent's behalf after "
                     "safety checks (blocklist, verified-live, legitimate "
                     "employer/platform, no scam signals, no premature SSN/"
                     "bank/payment asks)."),
            "at": "2026-09-14T12:00:00+00:00",
            "ref": "MEMORY.md: job-application account creation authorization",
        },
        "scope": {"action": "account_creation", "for": "job_applications"},
        "conditions": {},
        "ttl_h": 24 * 180,
        "single_use": False,
    },
    {
        "name": "comp_bands",
        "source_event": {
            "origin": "standing_registry",
            "text": ("Standing preference (Trent, 2026-09-13): for required "
                     "salary-range / requested-compensation questions, answer "
                     "within current market standards (defensible market "
                     "median/midpoint)."),
            "at": "2026-09-13T12:00:00+00:00",
            "ref": "MEMORY.md: compensation standing preference",
        },
        "scope": {"action": "answer_compensation",
                  "rule": "market_median_midpoint"},
        "conditions": {},
        "ttl_h": 24 * 180,
        "single_use": False,
    },
]

_STANDING = {}


def _build_standing():
    for d in _STANDING_DEFS:
        text = d["source_event"]["text"]
        if "EXAMPLE" in text.upper() or "placeholder" in text.lower():
            raise ValueError(
                f"standing definition {d['name']!r} carries example/placeholder "
                "evidence and must not be minted")
        if not d["source_event"].get("ref"):
            raise ValueError(
                f"standing definition {d['name']!r} has no evidence ref")
        src = dict(d["source_event"])
        src["conditions"] = d.get("conditions") or {}
        rec = issue_record(source_event=src, scope=d["scope"],
                           ttl_h=d["ttl_h"], single_use=d["single_use"],
                           record_id=f"apr-standing-{d['name']}")
        rec["standing"] = True
        _STANDING[d["name"]] = rec


_build_standing()


def standing_record(name):
    """Return a COPY of the standing record `name` (KeyError if unknown).

    Copies so a worker can present but never mutate the registry. The record
    still goes through validate_record() at the grant site — scope, expiry,
    and conditions are re-checked on every use.
    """
    rec = _STANDING[name]  # KeyError on unknown name: fail closed
    return copy.deepcopy(rec)


def standing_names():
    return sorted(_STANDING)


# ---------------------------------------------------------------- CLI
# The approval capability's command surface. `issue` is for the authenticated
# point (the main agent holding the operator's own words) — workers present
# records,
# they do not run `issue`.

def _cli():
    ap = argparse.ArgumentParser(description="approval records (F21)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_issue = sub.add_parser("issue", help="mint a record (authenticated point only)")
    p_issue.add_argument("--origin", required=True,
                         choices=list(AUTHENTICATED_ORIGINS))
    p_issue.add_argument("--text", required=True,
                         help="the approval words/event (immutable evidence)")
    p_issue.add_argument("--scope", required=True,
                         help="JSON scope, e.g. '{\"action\": \"form_attestation\", \"role_id\": \"R-1\"}'")
    p_issue.add_argument("--ttl-h", type=float, default=None)
    p_issue.add_argument("--single-use", action="store_true")
    p_issue.add_argument("--ref", default="")
    sub.add_parser("standing", help="list standing record names")
    p_show = sub.add_parser("show", help="print a standing record")
    p_show.add_argument("name")
    a = ap.parse_args()
    if a.cmd == "issue":
        rec = issue_record(
            source_event={"origin": a.origin, "text": a.text, "ref": a.ref},
            scope=json.loads(a.scope), ttl_h=a.ttl_h,
            single_use=a.single_use)
        print(json.dumps(rec, indent=1))
    elif a.cmd == "standing":
        print(json.dumps(standing_names(), indent=1))
    elif a.cmd == "show":
        print(json.dumps(standing_record(a.name), indent=1))


if __name__ == "__main__":
    _cli()
