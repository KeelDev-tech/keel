"""Canary-evidence adapter for the Privacy Release Controller.

Consumes the canary workstream's export schema EXACTLY (no defensive
guessing):

  Bundle envelope: {"schema": "keel.canary.bundle/v1", "capture_version",
                    "record_count", "records"}
  Record: workspace_id, role_id, application_id, action, component,
          source_ref, source_version, observed_at,
          provenance ("authentic_observation" | "synthetic_fixture" | "unknown"),
          provenance_reason, expires_at, revoked, revocation_ref,
          material_digest, capture_version, payload
  Verdict envelope: {"schema": "keel.canary.verdict/v1", "validator_version",
                     "verdict", "role_id", "application_id", "missing_families",
                     "families_without_records",
                     "checks": [{"name", "passed", "detail"}],
                     "failed_checks", "check_count", "failed_count"}

Evidence gate (structural, per brief): a verdict that is not a clean PASS —
or any record that is not an authentic, unrevoked, unexpired observation —
yields status BLOCKED-PENDING-EVIDENCE, and assert_evidence_ready() REFUSES
to let a genuine approval request be issued on it. The packet records the
blocked status explicitly (data_lineage.evidence_status + known_risks +
unresolved_questions) so counsel sees it three times, never zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

BUNDLE_SCHEMA = "keel.canary.bundle/v1"
VERDICT_SCHEMA = "keel.canary.verdict/v1"

AUTHENTIC = "authentic_observation"

EVIDENCE_READY = "READY_FOR_REVIEW"
EVIDENCE_BLOCKED = "BLOCKED-PENDING-EVIDENCE"

REQUIRED_RECORD_FIELDS = [
    "workspace_id", "role_id", "application_id", "action", "component",
    "source_ref", "source_version", "observed_at", "provenance",
    "provenance_reason", "expires_at", "revoked", "revocation_ref",
    "material_digest", "capture_version", "payload",
]
REQUIRED_VERDICT_FIELDS = [
    "schema", "validator_version", "verdict", "role_id", "application_id",
    "missing_families", "families_without_records", "checks",
    "failed_checks", "check_count", "failed_count",
]


class CanarySchemaError(ValueError):
    """Raised when canary evidence does not match the exact export schema."""


class EvidenceBlocked(Exception):
    """Raised when an approval request is attempted on incomplete evidence."""


@dataclass
class CanaryEvidence:
    bundle: dict
    verdict: dict

    @property
    def records(self) -> list[dict]:
        return self.bundle["records"]


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise CanarySchemaError(f"{path}: top-level JSON must be an object")
    return data


def load_canary_evidence(bundle_path: str, verdict_path: str) -> CanaryEvidence:
    """Load and strictly validate canary evidence against the exact schema."""
    bundle = _load_json(bundle_path)
    verdict = _load_json(verdict_path)

    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise CanarySchemaError(
            f"bundle schema must be {BUNDLE_SCHEMA!r}, got {bundle.get('schema')!r}")
    for key in ("capture_version", "record_count", "records"):
        if key not in bundle:
            raise CanarySchemaError(f"bundle missing envelope key: {key}")
    records = bundle["records"]
    if not isinstance(records, list):
        raise CanarySchemaError("bundle.records must be a list")
    if bundle["record_count"] != len(records):
        raise CanarySchemaError(
            f"bundle.record_count={bundle['record_count']} != len(records)={len(records)}")
    for i, rec in enumerate(records):
        missing = [f for f in REQUIRED_RECORD_FIELDS if f not in rec]
        if missing:
            raise CanarySchemaError(f"record[{i}] missing fields: {missing}")
        if rec["provenance"] not in (AUTHENTIC, "synthetic_fixture", "unknown"):
            raise CanarySchemaError(
                f"record[{i}].provenance has unknown value {rec['provenance']!r}")

    if verdict.get("schema") != VERDICT_SCHEMA:
        raise CanarySchemaError(
            f"verdict schema must be {VERDICT_SCHEMA!r}, got {verdict.get('schema')!r}")
    missing = [f for f in REQUIRED_VERDICT_FIELDS if f not in verdict]
    if missing:
        raise CanarySchemaError(f"verdict missing fields: {missing}")
    checks = verdict["checks"]
    if not isinstance(checks, list) or any(
            not isinstance(c, dict) or
            any(k not in c for k in ("name", "passed", "detail"))
            for c in checks):
        raise CanarySchemaError("verdict.checks must be [{name, passed, detail}]")
    if verdict["check_count"] != len(checks):
        raise CanarySchemaError("verdict.check_count != len(checks)")
    if verdict["failed_count"] != len(verdict["failed_checks"]):
        raise CanarySchemaError("verdict.failed_count != len(failed_checks)")

    return CanaryEvidence(bundle=bundle, verdict=verdict)


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evidence_status(evidence: CanaryEvidence,
                     now: datetime | None = None) -> str:
    """READY_FOR_REVIEW only on fully authentic, complete, passing evidence.

    Anything else — non-PASS verdict, any failed check, any missing family,
    any non-authentic / revoked / expired record — is BLOCKED-PENDING-EVIDENCE.
    """
    now = now or datetime.now(timezone.utc)
    v = evidence.verdict
    if v.get("verdict") != "PASS":
        return EVIDENCE_BLOCKED
    if v.get("failed_count") or v.get("failed_checks") or v.get("missing_families") \
            or v.get("families_without_records"):
        return EVIDENCE_BLOCKED
    for rec in evidence.records:
        if rec.get("provenance") != AUTHENTIC:
            return EVIDENCE_BLOCKED
        if rec.get("revoked"):
            return EVIDENCE_BLOCKED
        exp = _parse_ts(rec.get("expires_at"))
        if exp is not None and exp <= now:
            return EVIDENCE_BLOCKED
    return EVIDENCE_READY


def assert_evidence_ready(evidence: CanaryEvidence,
                          now: datetime | None = None) -> None:
    """Structural gate: refuse to issue a genuine approval request on
    incomplete evidence. Raises EvidenceBlocked with the concrete defects."""
    status = evidence_status(evidence, now)
    if status == EVIDENCE_READY:
        return
    v = evidence.verdict
    defects = [
        f"verdict={v.get('verdict')}",
        f"failed_checks={v.get('failed_checks')}",
        f"missing_families={v.get('missing_families')}",
        f"families_without_records={v.get('families_without_records')}",
    ]
    non_auth = sorted({r.get("provenance", "?") for r in evidence.records
                       if r.get("provenance") != AUTHENTIC})
    if non_auth:
        defects.append(f"non_authentic_provenance={non_auth}")
    revoked = [r.get("component") for r in evidence.records if r.get("revoked")]
    if revoked:
        defects.append(f"revoked_components={revoked}")
    raise EvidenceBlocked(
        "BLOCKED-PENDING-EVIDENCE: no genuine approval request may be issued "
        "on incomplete evidence — " + "; ".join(defects))


def summarize_evidence(evidence: CanaryEvidence,
                       now: datetime | None = None) -> dict:
    """Evidence summary for the packet's data_lineage section."""
    v = evidence.verdict
    per_record = []
    for r in evidence.records:
        per_record.append({
            "component": r.get("component"),
            "action": r.get("action"),
            "role_id": r.get("role_id"),
            "application_id": r.get("application_id"),
            "provenance": r.get("provenance"),
            "provenance_reason": r.get("provenance_reason"),
            "material_digest": r.get("material_digest"),
            "source_ref": r.get("source_ref"),
            "source_version": r.get("source_version"),
            "observed_at": r.get("observed_at"),
            "expires_at": r.get("expires_at"),
            "revoked": r.get("revoked"),
            "revocation_ref": r.get("revocation_ref"),
        })
    return {
        "canary_bundle_schema": evidence.bundle.get("schema"),
        "canary_verdict_schema": v.get("schema"),
        "capture_version": evidence.bundle.get("capture_version"),
        "validator_version": v.get("validator_version"),
        "evidence_status": evidence_status(evidence, now),
        "verdict": v.get("verdict"),
        "role_id": v.get("role_id"),
        "application_id": v.get("application_id"),
        "check_count": v.get("check_count"),
        "failed_count": v.get("failed_count"),
        "failed_checks": list(v.get("failed_checks") or []),
        "check_details": [
            {"name": c["name"], "passed": c["passed"], "detail": c["detail"]}
            for c in v.get("checks") or []
        ],
        "missing_families": list(v.get("missing_families") or []),
        "families_without_records": list(v.get("families_without_records") or []),
        "records": per_record,
    }


def canary_packet_inputs(evidence: CanaryEvidence,
                         now: datetime | None = None) -> dict:
    """Map validated canary evidence onto prepare_packet() kwargs.

    Returns the packet sections this evidence determines: data_lineage,
    known_risks, unresolved_questions. When evidence is blocked, the packet
    records BLOCKED-PENDING-EVIDENCE in all three — counsel sees the block,
    and assert_evidence_ready() (called by the coordinator before presenting
    the packet as an approval request) refuses to proceed.
    """
    summary = summarize_evidence(evidence, now)
    status = summary["evidence_status"]

    known_risks: list[dict] = []
    unresolved: list[str] = []

    if status == EVIDENCE_BLOCKED:
        known_risks.append({
            "risk": "evidence BLOCKED-PENDING-EVIDENCE: canary validation did "
                    "not pass; no approval request may be issued on this evidence",
            "severity": "high",
            "verdict": summary["verdict"],
            "failed_checks": summary["failed_checks"],
        })
    for name in summary["failed_checks"]:
        detail = next((c["detail"] for c in summary["check_details"]
                       if c["name"] == name), "")
        known_risks.append({
            "risk": f"canary check failed: {name}",
            "severity": "high" if status == EVIDENCE_BLOCKED else "medium",
            "detail": detail,
        })
        unresolved.append(
            f"Canary check '{name}' failed ({detail}) — what evidence would "
            f"close it before counsel review?")
    for family in summary["missing_families"]:
        unresolved.append(
            f"Canary evidence family '{family}' is missing "
            f"(explicit UNKNOWN marker) — authentic evidence required before "
            f"any approval request.")
    for r in summary["records"]:
        if r["provenance"] != AUTHENTIC:
            known_risks.append({
                "risk": f"record for component '{r['component']}' has "
                        f"provenance='{r['provenance']}' (not an authentic "
                        f"observation)",
                "severity": "medium",
                "provenance_reason": r["provenance_reason"],
            })
        if r["revoked"]:
            known_risks.append({
                "risk": f"record for component '{r['component']}' is revoked",
                "severity": "high",
                "revocation_ref": r["revocation_ref"],
            })

    data_lineage = {
        "canary_evidence": summary,
        "note": "lineage below is the canary capture record; artifact-level "
                "transform lineage (lineage.py) is recorded separately",
    }
    return {
        "data_lineage": data_lineage,
        "known_risks": known_risks,
        "unresolved_questions": unresolved,
    }
