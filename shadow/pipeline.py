#!/usr/bin/env python3
"""Shadow validation pipeline (Workstream F, Keel shadow lane).

Seven-revision export -> revision adapter -> assurance -> trust -> consent ->
execution policy -> SHADOW result.

HARD RULE (fail-closed, tested):
    execution_authorized is False UNLESS a COMPLETELY SEPARATE valid
    execution authorization exists. Source completeness is NEVER execution
    authorization. A synthetic fixture NEVER grants consent and NEVER
    authorizes execution.

No genuine human decision exists yet, so the pipeline validates its
mechanism on clearly-labeled SYNTHETIC fixtures that live in memory only and
are never persisted. Every shadow run honestly reports
``SHADOW_VALIDATED=false`` with the reason ``pending genuine decision``.

Deliberately, this module ships NO writer for execution authorizations: the
separate execution authorization is a human grant recorded by the
coordinator (artifact schema documented in AUTHZ_SCHEMA below, store at
review/execution-authorizations.jsonl). Nothing in this workstream can mint
one, so none can be manufactured here.

Stdlib only. Read-only against all stores; the shadow run writes nothing
except its returned result dict (callers may persist that result wherever
they choose).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import sys

KEEL_DIR = Path(__file__).resolve().parent.parent
if str(KEEL_DIR) not in sys.path:
    sys.path.insert(0, str(KEEL_DIR))

from review.packet import (  # noqa: E402
    FAMILIES,
    PacketError,
    read_packet,
    sha256_hex,
    verify_material_digest,
)
from review.decision import (  # noqa: E402
    DECISION_APPROVE,
    HUMAN_DECISION_REQUIRED,
    list_genuine_decisions,
)

PIPELINE_VERSION = "keel.shadow_pipeline.v1"
CANARY_BUNDLE_SCHEMA = "keel.canary.bundle/v1"
EVIDENCE_DIR = KEEL_DIR / "canary" / "evidence" / "keel-canary-001"
REVIEW_DIR = KEEL_DIR / "review"
AUTHZ_STORE = REVIEW_DIR / "execution-authorizations.jsonl"

# Required fields of the SEPARATE execution authorization artifact. It must be
# a different record from the decision: different id, its own actor and
# authority reference, explicitly binding the decision AND the packet.
AUTHZ_SCHEMA = "keel.execution_authorization.v1"
AUTHZ_REQUIRED_FIELDS = (
    "authz_id",
    "decision_id",
    "packet_sha256",
    "scope",
    "actor",
    "authority_ref",
    "issued_at",
    "expires_at",
    "revoked",
)


class ShadowError(ValueError):
    """A shadow-pipeline contract violation. Never silently defaulted."""


# ---------------------------------------------------------------------------
# Stage 1: seven-revision export
# ---------------------------------------------------------------------------

def _read_canary_bundle(evidence_dir: str | Path) -> list[dict]:
    """Read canary bundle files defensively (no import of sibling code).

    Returns the record list; an absent/empty evidence dir yields [].
    Files that parse as JSON but are not canary bundles (e.g. the sibling's
    verdict artifacts) are ignored -- they are not evidence input. A file
    that fails to parse raises ShadowError: a corrupt file in the evidence
    dir is never silently treated as empty.
    """
    records: list[dict] = []
    evidence = Path(evidence_dir)
    if not evidence.is_dir():
        return records
    for bundle_path in sorted(evidence.glob("*.json")):
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ShadowError(f"canary_bundle_unreadable: {exc}") from exc
        if not isinstance(bundle, dict) or bundle.get("schema") != CANARY_BUNDLE_SCHEMA:
            continue  # sibling auxiliary artifact, not the evidence bundle
        bundle_records = bundle.get("records")
        if not isinstance(bundle_records, list):
            raise ShadowError(f"canary_bundle_records_not_a_list: {bundle_path.name}")
        records.extend(bundle_records)
    return records


def export_stage(*, evidence_dir: str | Path | None = None,
                 synthetic_families: dict | None = None) -> dict:
    """Seven-revision export.

    Six families come from the canary evidence dir (or an in-memory synthetic
    fixture labeled as such); the seventh -- approval -- comes ONLY from the
    genuine decision store. A synthetic decision fixture is attached by the
    caller at the consent stage, never here.
    """
    synthetic = synthetic_families is not None
    raw_records = (list(synthetic_families.values()) if synthetic
                   else _read_canary_bundle(evidence_dir or EVIDENCE_DIR))
    families: dict[str, dict] = {}
    for family in FAMILIES:
        match = next((r for r in raw_records
                      if isinstance(r, dict) and r.get("component") == family),
                     None)
        if match is None:
            families[family] = {"status": "MISSING", "record": None,
                                "synthetic": synthetic}
        else:
            families[family] = {"status": "EXPORTED", "record": match,
                                "synthetic": synthetic}

    genuine = [d for d in list_genuine_decisions()
               if d.get("decision") == DECISION_APPROVE]
    approval = ({"status": "APPROVED", "decision": genuine[-1]}
                if genuine else
                {"status": HUMAN_DECISION_REQUIRED, "decision": None})
    return {"families": families, "approval": approval,
            "synthetic_evidence": synthetic}


# ---------------------------------------------------------------------------
# Stage 2: revision adapter
# ---------------------------------------------------------------------------

def revision_adapter_stage(export: dict) -> dict:
    """Normalize exported records to revision digests.

    ``envelope_inputs_ready`` means STRUCTURAL completeness only -- the six
    families exported without transport errors. It is NOT authorization, NOT
    consent, and NOT evidence quality; assurance judges those next.
    """
    revisions: dict[str, str | None] = {}
    for family in FAMILIES:
        entry = export["families"][family]
        record = entry.get("record")
        if entry["status"] == "EXPORTED" and isinstance(record, dict):
            digest = record.get("material_digest")
            revisions[family] = digest if isinstance(digest, str) else None
        else:
            revisions[family] = None
    envelope_inputs_ready = all(
        export["families"][f]["status"] == "EXPORTED" for f in FAMILIES)
    return {"revisions": revisions,
            "envelope_inputs_ready": envelope_inputs_ready,
            # Explicit: structural readiness is not authority of any kind.
            "envelope_inputs_ready_means": "structural completeness ONLY"}


# ---------------------------------------------------------------------------
# Stage 3: assurance
# ---------------------------------------------------------------------------

def _parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ShadowError("timestamp_invalid")
    return result.astimezone(timezone.utc)


def assurance_stage(export: dict, packet: dict, *,
                    now: datetime | None = None) -> dict:
    """Assurance: evidence quality against the sealed packet.

    Passes only if every family is a verified authentic observation whose
    material digest recomputes, is unexpired and unrevoked, matches the
    packet's revision digests, and the packet carries no unresolved warnings.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    findings: list[str] = []
    for family in FAMILIES:
        entry = export["families"][family]
        record = entry.get("record")
        if entry["status"] != "EXPORTED" or not isinstance(record, dict):
            findings.append(f"{family}: not exported")
            continue
        if entry.get("synthetic"):
            findings.append(f"{family}: synthetic fixture is not evidence")
            continue
        if record.get("provenance") != "authentic_observation":
            findings.append(f"{family}: provenance not authentic "
                            f"({record.get('provenance')})")
            continue
        if record.get("revoked"):
            findings.append(f"{family}: revoked")
            continue
        expires_at = record.get("expires_at")
        if isinstance(expires_at, str) and expires_at:
            try:
                if _parse_time(expires_at) <= now:
                    findings.append(f"{family}: expired")
                    continue
            except ShadowError:
                findings.append(f"{family}: bad expires_at")
                continue
        if not verify_material_digest(record):
            findings.append(f"{family}: material digest mismatch")
            continue
        if record.get("material_digest") != packet.get("revisions", {}).get(family):
            findings.append(f"{family}: digest differs from sealed packet")
    if packet.get("unresolved_warnings"):
        findings.append("packet carries unresolved warnings")
    if packet.get("status") != "READY_FOR_REVIEW":
        findings.append(f"packet status is {packet.get('status')}")
    return {"passed": not findings, "findings": findings}


# ---------------------------------------------------------------------------
# Stage 4: trust
# ---------------------------------------------------------------------------

def trust_stage(export: dict) -> dict:
    """Trust: origin authentication.

    The shadow lane has no authenticity oracle: it cannot authenticate a
    source's origin or Trent's identity from inside the machine. It therefore
    honestly reports source_authenticity_verified=False and names the human
    trust step that remains. A True here would be a fabrication.
    """
    return {"source_authenticity_verified": False,
            "findings": ["origin authentication is a human trust step; "
                         "the shadow lane cannot verify it"],
            "approval_on_file": export["approval"]["status"]}


# ---------------------------------------------------------------------------
# Stage 5: consent
# ---------------------------------------------------------------------------

def _decision_fresh(decision: dict, now) -> bool:
    try:
        return _parse_time(decision["expires_at"]) > now
    except (KeyError, ShadowError, ValueError):
        return False


def consent_stage(packet: dict, export: dict, *,
                  synthetic_decision: dict | None = None,
                  now: datetime | None = None) -> dict:
    """Consent: a genuine APPROVE decision bound to the sealed packet.

    A synthetic decision fixture -- however complete -- NEVER grants consent.
    It is accepted only to exercise the mechanism, and is labeled as such.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sealed = packet.get("packet_sha256")
    if synthetic_decision is not None:
        return {"consent_granted": False,
                "synthetic": True,
                "reason": "synthetic decision fixture cannot grant consent; "
                          "mechanism exercised only"}
    decision = export["approval"].get("decision")
    if decision is None:
        return {"consent_granted": False, "synthetic": False,
                "reason": HUMAN_DECISION_REQUIRED}
    if decision.get("decision") != DECISION_APPROVE:
        return {"consent_granted": False, "synthetic": False,
                "reason": "genuine decision is not APPROVE"}
    if decision.get("reviewed_sha256") != sealed:
        return {"consent_granted": False, "synthetic": False,
                "reason": "decision does not bind to this sealed packet"}
    if not _decision_fresh(decision, now):
        return {"consent_granted": False, "synthetic": False,
                "reason": "genuine decision expired"}
    return {"consent_granted": True, "synthetic": False,
            "decision_id": decision.get("decision_id"),
            "reason": "genuine APPROVE bound to sealed packet"}


# ---------------------------------------------------------------------------
# Stage 6: execution policy (the hard rule)
# ---------------------------------------------------------------------------

def validate_execution_authorization(authz: dict, *, packet: dict,
                                     decision: dict | None,
                                     now: datetime | None = None) -> dict:
    """Validate a SEPARATE execution authorization artifact.

    The authorization must be a different record from the decision
    (different id, own actor/authority), explicitly binding the decision id,
    the packet SHA-256, and the scope; unexpired and unrevoked.
    Returns {"valid": bool, "reasons": [...]}. Pure validation -- no writes.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reasons: list[str] = []
    if not isinstance(authz, dict):
        return {"valid": False, "reasons": ["authorization_not_a_record"]}
    missing = [f for f in AUTHZ_REQUIRED_FIELDS if f not in authz]
    if missing:
        reasons.append(f"missing_fields: {missing}")
    if authz.get("schema") != AUTHZ_SCHEMA:
        reasons.append(f"wrong_schema: {authz.get('schema')}")
    if decision is not None and authz.get("decision_id") != decision.get("decision_id"):
        reasons.append("decision_id_mismatch")
    if authz.get("packet_sha256") != packet.get("packet_sha256"):
        reasons.append("packet_sha256_mismatch")
    if authz.get("scope") != packet.get("scope"):
        reasons.append("scope_mismatch")
    if not isinstance(authz.get("actor"), str) or not authz["actor"].strip():
        reasons.append("actor_required")
    if not isinstance(authz.get("authority_ref"), str) or not authz["authority_ref"].strip():
        reasons.append("authority_ref_required")
    if authz.get("revoked"):
        reasons.append("authorization_revoked")
    try:
        issued = _parse_time(authz["issued_at"])
        expires = _parse_time(authz["expires_at"])
        if not (issued <= now < expires):
            reasons.append("authorization_not_currently_valid")
    except (KeyError, ShadowError, ValueError):
        reasons.append("bad_issued_or_expires_at")
    return {"valid": not reasons, "reasons": reasons}


def read_execution_authorizations() -> list[dict]:
    """Read the separate-authorization store. Currently: none on file."""
    if not AUTHZ_STORE.exists():
        return []
    authorizations = []
    for line in AUTHZ_STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            authorizations.append(json.loads(line))
    return authorizations


def execution_policy_stage(*, consent: dict,
                           execution_authorization: dict | None,
                           packet: dict, decision: dict | None,
                           attested_genuine: bool,
                           now: datetime | None = None) -> dict:
    """The hard rule, as a pure function.

    execution_authorized is True ONLY when ALL hold:
      1. genuine consent (a real APPROVE bound to the sealed packet),
      2. a COMPLETELY SEPARATE valid execution authorization exists,
      3. the artifacts are attested genuine (came from the real stores,
         not a synthetic fixture).

    ``attested_genuine`` is set ONLY by run paths that sourced artifacts
    from the real stores. run_shadow NEVER sets it True. Unit tests may pass
    True with clearly-labeled synthetic inputs to verify the mechanism path.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not attested_genuine:
        return {"execution_authorized": False,
                "reason": "artifacts not attested genuine "
                          "(synthetic fixture or unattested path)"}
    if not consent.get("consent_granted"):
        return {"execution_authorized": False,
                "reason": f"no genuine consent: {consent.get('reason')}"}
    if execution_authorization is None:
        return {"execution_authorized": False,
                "reason": "no separate execution authorization: "
                          "source completeness alone does not authorize "
                          "execution"}
    validation = validate_execution_authorization(
        execution_authorization, packet=packet, decision=decision, now=now)
    if not validation["valid"]:
        return {"execution_authorized": False,
                "reason": "separate execution authorization invalid: "
                          + "; ".join(validation["reasons"]),
                "validation": validation}
    return {"execution_authorized": True,
            "reason": "genuine consent + separate valid execution "
                      "authorization",
            "validation": validation}


# ---------------------------------------------------------------------------
# Full shadow run
# ---------------------------------------------------------------------------

def _synthetic_label(fixture: dict) -> str:
    return fixture.get("label") or "SYNTHETIC fixture"


def run_shadow(packet_path: str | Path, *,
               evidence_dir: str | Path | None = None,
               synthetic_fixture: dict | None = None,
               now: datetime | None = None) -> dict:
    """Run the full shadow pipeline. READ-ONLY against all stores.

    - Without a fixture: runs against the real evidence dir and the genuine
      decision store. (Currently: empty evidence, no decisions.)
    - With ``synthetic_fixture``: exercises the mechanism on in-memory
      synthetic evidence + a synthetic decision fixture. The fixture and all
      stage results are labeled synthetic; nothing is persisted.

    ALWAYS reports SHADOW_VALIDATED=false: a shadow run can validate the
    mechanism, never the genuine decision, which belongs to Trent only.
    ``attested_genuine`` is ALWAYS False here -- run_shadow never attests.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    packet = read_packet(packet_path)  # integrity verified on every read
    synthetic = synthetic_fixture is not None
    fixture_families = (synthetic_fixture or {}).get("families")
    fixture_decision = (synthetic_fixture or {}).get("decision")
    fixture_authz = (synthetic_fixture or {}).get("execution_authorization")

    export = export_stage(evidence_dir=evidence_dir,
                          synthetic_families=fixture_families)
    adapted = revision_adapter_stage(export)
    assurance = assurance_stage(export, packet, now=now)
    trust = trust_stage(export)
    consent = consent_stage(packet, export,
                            synthetic_decision=fixture_decision, now=now)
    genuine_decision = None if synthetic else export["approval"].get("decision")
    policy = execution_policy_stage(
        consent=consent,
        execution_authorization=fixture_authz,
        packet=packet,
        decision=genuine_decision,
        attested_genuine=False,  # run_shadow never attests; see docstring
        now=now)

    if synthetic:
        reason = (f"synthetic fixture exercised ({_synthetic_label(synthetic_fixture)}); "
                  "no genuine human decision exists -- SHADOW_VALIDATED=false "
                  "pending Trent's genuine decision")
    else:
        reason = ("no genuine human decision exists; evidence incomplete "
                  "(packet BLOCKED-PENDING-EVIDENCE) -- SHADOW_VALIDATED=false "
                  "pending Trent's genuine decision")

    return {
        "pipeline": PIPELINE_VERSION,
        "packet_id": packet.get("packet_id"),
        "packet_sha256": packet.get("packet_sha256"),
        "packet_status": packet.get("status"),
        "synthetic_fixture": synthetic,
        "synthetic_label": _synthetic_label(synthetic_fixture) if synthetic else None,
        "stages": {
            "export": export,
            "revision_adapter": adapted,
            "assurance": assurance,
            "trust": trust,
            "consent": consent,
            "execution_policy": policy,
        },
        "execution_authorized": policy["execution_authorized"],
        "execution_authorized_reason": policy["reason"],
        "shadow_validated": False,
        "shadow_validated_reason": reason,
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Keel shadow validation pipeline")
    parser.add_argument("packet", help="path to the sealed review packet JSON")
    parser.add_argument("--evidence-dir", default=None)
    args = parser.parse_args(argv)
    result = run_shadow(args.packet, evidence_dir=args.evidence_dir)
    print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
