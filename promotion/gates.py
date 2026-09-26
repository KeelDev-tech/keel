"""Deterministic promotion gates (Workstream G, Directive S8).

A promotion ladder runs:

    canary (1 real canary)
      -> hetero5 (5 heterogeneous cases)
      -> stage10 -> stage50 -> stage100
      -> fleet

No stage may be skipped: each stage's evidence report must name the
immediately-preceding stage as its prior stage and carry that prior stage's
verified evidence-report digest.

Six zero-tolerance invariants (Directive S8). Promotion is BLOCKED when ANY
of them is nonzero in the stage evidence report:

    fabricated_provenance
    synthetic_to_live
    unauthorized_egress
    false_human_approval
    unknown_to_pass_without_evidence
    wrong_target_execution

API
---
evaluate(stage_evidence_report: dict) -> {"verdict": "PROMOTE"|"BLOCK", "reasons": [...]}

Pure function: no I/O, no LLM, no randomness, no clock. Fully deterministic
for a given input mapping.

Evidence-report schema (fail-closed: anything missing, malformed, or
unknown blocks promotion):

    stage                   str, one of STAGES
    invariants              mapping with EXACTLY the six INVARIANTS keys,
                            each a non-negative int (bool rejected)
    prior_stage_name        canary: absent or None
                            later stages: str == the immediately preceding stage
    prior_stage_digest      canary: absent, None, or ""
                            later stages: non-empty str (digest of the prior
                            stage's verified evidence report)
    case_count              non-negative int, >= STAGE_MIN_CASES[stage]
    authentic_source_count  non-negative int, >= 1
    genuine_decision_count  non-negative int, >= 1

Any other top-level field, or any other key inside "invariants", is an
unknown evidence field and BLOCKS (unknown is never converted to PASS).

Integration note on digests
---------------------------
evaluate() checks digest *presence and form* only; it cannot re-verify a
digest against the prior stage's report because it is a pure function of a
single report. Integrators MUST bind the digest themselves (e.g. sha256 of
the canonical JSON of the prior stage's verified evidence report -- see
evidence_digest() below) and MUST verify the digest matches the prior
stage's verified report before calling evaluate(). A gate verdict of
PROMOTE is only meaningful on a report whose digest binding was verified
upstream.
"""

from __future__ import annotations

import hashlib
import json

__all__ = [
    "STAGES",
    "INVARIANTS",
    "STAGE_MIN_CASES",
    "VERDICT_PROMOTE",
    "VERDICT_BLOCK",
    "evaluate",
    "evidence_digest",
    "evidence_schema",
]

VERDICT_PROMOTE = "PROMOTE"
VERDICT_BLOCK = "BLOCK"

STAGES = ("canary", "hetero5", "stage10", "stage50", "stage100", "fleet")

INVARIANTS = (
    "fabricated_provenance",
    "synthetic_to_live",
    "unauthorized_egress",
    "false_human_approval",
    "unknown_to_pass_without_evidence",
    "wrong_target_execution",
)

STAGE_MIN_CASES = {
    "canary": 1,
    "hetero5": 5,
    "stage10": 10,
    "stage50": 50,
    "stage100": 100,
    "fleet": 100,
}

_TOP_LEVEL_FIELDS = frozenset(
    {
        "stage",
        "invariants",
        "prior_stage_name",
        "prior_stage_digest",
        "case_count",
        "authentic_source_count",
        "genuine_decision_count",
    }
)

_FIRST_STAGE = STAGES[0]


def _is_count(value) -> bool:
    """True for a genuine non-negative int; bool is rejected on purpose."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def evidence_schema() -> dict:
    """Machine-readable description of the evidence-report schema."""
    return {
        "stages": list(STAGES),
        "stage_min_cases": dict(STAGE_MIN_CASES),
        "invariants": list(INVARIANTS),
        "required_top_level_fields": sorted(_TOP_LEVEL_FIELDS),
        "fail_closed": True,
    }


def evidence_digest(canonical_json: bytes) -> str:
    """sha256 digest (hex) over canonical evidence-report bytes.

    Intended for binding a stage's verified evidence report into the next
    stage's ``prior_stage_digest`` field. Pure and deterministic.
    """
    if not isinstance(canonical_json, (bytes, bytearray)):
        raise TypeError("evidence_digest requires bytes")
    return hashlib.sha256(bytes(canonical_json)).hexdigest()


def _check_stage(report: dict, reasons: list) -> str | None:
    stage = report.get("stage", _MISSING)
    if stage is _MISSING:
        reasons.append("missing evidence field 'stage'")
        return None
    if not isinstance(stage, str):
        reasons.append("malformed evidence field 'stage': expected string")
        return None
    if stage not in STAGES:
        reasons.append("unknown stage %r" % (stage,))
        return None
    return stage


def _check_prior_stage(report: dict, stage: str | None, reasons: list) -> None:
    name = report.get("prior_stage_name", _MISSING)
    digest = report.get("prior_stage_digest", _MISSING)

    if stage is None or stage == _FIRST_STAGE:
        # First stage (or unknown stage): there must be no prior stage named.
        if name not in (_MISSING, None):
            reasons.append(
                "malformed evidence: stage %r must not name a prior stage"
                % (_FIRST_STAGE,)
            )
        if digest not in (_MISSING, None, ""):
            reasons.append(
                "malformed evidence: stage %r must not carry a prior stage digest"
                % (_FIRST_STAGE,)
            )
        return

    expected = STAGES[STAGES.index(stage) - 1]
    if name in (_MISSING, None):
        reasons.append("missing evidence field 'prior_stage_name'")
    elif not isinstance(name, str):
        reasons.append("malformed evidence field 'prior_stage_name': expected string")
    elif name != expected:
        reasons.append(
            "stage skipping rejected: stage %r requires prior stage %r, got %r"
            % (stage, expected, name)
        )
    if digest in (_MISSING, None, ""):
        reasons.append("missing evidence field 'prior_stage_digest'")
    elif not isinstance(digest, str):
        reasons.append(
            "malformed evidence field 'prior_stage_digest': expected string"
        )


def _check_invariants(report: dict, reasons: list) -> None:
    inv = report.get("invariants", _MISSING)
    if inv is _MISSING:
        reasons.append("missing evidence field 'invariants'")
        return
    if not isinstance(inv, dict):
        reasons.append("malformed evidence field 'invariants': expected mapping")
        return
    for key in sorted(inv):
        if key not in INVARIANTS:
            reasons.append("unknown invariant %r" % (key,))
    for name in INVARIANTS:
        if name not in inv:
            reasons.append("missing invariant count %r" % (name,))
            continue
        value = inv[name]
        if not _is_count(value):
            reasons.append(
                "malformed invariant %r: expected non-negative integer" % (name,)
            )
            continue
        if value != 0:
            reasons.append(
                "BLOCKED: invariant %r nonzero (count=%d)" % (name, value)
            )


def _check_metadata(report: dict, stage: str | None, reasons: list) -> None:
    minimums = {
        "case_count": 1 if stage is None else STAGE_MIN_CASES.get(stage, 1),
        "authentic_source_count": 1,
        "genuine_decision_count": 1,
    }
    for field, minimum in minimums.items():
        value = report.get(field, _MISSING)
        if value is _MISSING:
            reasons.append("missing evidence field %r" % (field,))
            continue
        if not _is_count(value):
            reasons.append(
                "malformed evidence field %r: expected non-negative integer"
                % (field,)
            )
            continue
        if value < minimum:
            reasons.append(
                "insufficient evidence: field %r is %d, minimum %d"
                % (field, value, minimum)
            )


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


def evaluate(stage_evidence_report: dict) -> dict:
    """Evaluate one stage evidence report. Returns verdict + reasons.

    Verdict is "PROMOTE" only when every check passes; any reason at all
    yields "BLOCK". Deterministic, pure, no I/O.
    """
    reasons: list = []
    try:
        if not isinstance(stage_evidence_report, dict):
            reasons.append("malformed evidence report: expected a mapping")
            return {"verdict": VERDICT_BLOCK, "reasons": reasons}

        report = stage_evidence_report

        for key in sorted(report):
            if key not in _TOP_LEVEL_FIELDS:
                reasons.append("unknown evidence field %r" % (key,))

        stage = _check_stage(report, reasons)
        _check_prior_stage(report, stage, reasons)
        _check_invariants(report, reasons)
        _check_metadata(report, stage, reasons)
    except Exception as exc:  # fail closed on any internal error
        reasons.append(
            "internal evaluation error (%s) -- fail closed" % type(exc).__name__
        )

    verdict = VERDICT_PROMOTE if not reasons else VERDICT_BLOCK
    return {"verdict": verdict, "reasons": reasons}


def _self_check_schema_roundtrip() -> dict:
    """Deterministic sanity: canonical JSON of the schema hashes stably."""
    blob = json.dumps(
        evidence_schema(), sort_keys=True, separators=(",", ":")
    ).encode()
    return {"schema_sha256": evidence_digest(blob)}
