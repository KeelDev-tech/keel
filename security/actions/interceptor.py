"""Action interceptor: the single enforcement choke point.

request() runs the full deterministic flow:

    IDENTITY/CAPABILITY -> ACTION CLASSIFICATION -> INJECTION CHECK
      -> DATA/SECRET CHECK -> POLICY ENGINE -> RISK -> APPROVAL GATE
      -> SECURITY LEDGER -> result

Completed policy evaluations append one security-ledger event, including
denials. Identity/input validation may refuse before that point; a ledger
failure refuses without returning ALLOW. Non-ALLOW results raise on
enforce(); callers must not execute unless result.allowed.

Authority boundary (F8, 2026-09-20): this module is the EXECUTION-TIME
security authority — it answers "may this agent execute this action NOW?"
Identity/capability verification, action classification, injection
scanning, DLP/secret checks, deterministic policy evaluation, and the
hash-chained security ledger all live here, and every denial raises.
It is NOT the content-approval authority. Content approval lives in
approval_records.py and the F21 check_human_approval gate: an exact
content fingerprint binding, single-use, 24-hour TTL — it answers "was
this EXACT content approved?" A human content approval never substitutes
for this execution-time check, and this check never substitutes for a
content approval; the same path may require both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

from ..data import dlp as _dlp
from ..data.classifier import Sensitivity, classify_text, max_sensitivity
from ..data.redactor import redact
from ..errors import PolicyDenied
from ..identity.agent_identity import (Identity, IdentityRegistry,
                                       mint_identity_id, register_system_identity)
from ..identity.capabilities import CapabilityManifest
from ..injection.content_classifier import Content, classify_source
from ..injection.scanner import scan
from ..policy.engine import Decision, Evaluation, evaluate
from .approval_gate import ApprovalStore
from .classifier import ActionClass, classify

# The engine process is the spawning authority: it registers its own
# system identity once per process (idempotent, deterministic id).
_ENGINE_IDENTITY_NAME = "keel-application-engine"
_ENGINE_IDENTITY_ID = mint_identity_id(
    "service", _ENGINE_IDENTITY_NAME,
    nonce=f"system:service:{_ENGINE_IDENTITY_NAME}")
_ENGINE_CAPABILITIES = (
    "read_leads", "write_packet", "read_telemetry", "write_telemetry",
    "record_submission", "manage_queue",
)
_bootstrapped = False


def _ensure_bootstrap() -> Identity:
    global _bootstrapped
    existing = IdentityRegistry.get(_ENGINE_IDENTITY_ID)
    if existing is not None:
        return existing
    ident = register_system_identity(_ENGINE_IDENTITY_NAME, kind="service",
                                     note="Keel engine process identity")
    if not _bootstrapped:
        _bootstrapped = True
    return ident


def _resolve_identity(agent) -> Identity:
    """Resolve a canonical registered identity; revoked/altered -> raise.

    Registry administration is trusted in-process code, not authentication
    against a hostile process with arbitrary Python execution.
    """
    identity = None
    if isinstance(agent, Identity):
        if IdentityRegistry.get(agent.id) == agent:
            identity = agent
    elif isinstance(agent, str):
        if agent == _ENGINE_IDENTITY_NAME:
            identity = _ensure_bootstrap()
        else:
            identity = IdentityRegistry.get(agent)
    if identity is None or IdentityRegistry.is_revoked(identity.id):
        raise PolicyDenied("unknown, altered, or revoked identity — fail closed")
    return identity


@dataclass
class InterceptionResult:
    decision: Decision
    evaluation: Evaluation
    injection_findings: list = field(default_factory=list)
    dlp_violations: list = field(default_factory=list)
    ledger_seq: int = -1
    approval_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    def enforce(self) -> "InterceptionResult":
        """Raise on anything but ALLOW. Callers must enforce()."""
        if self.decision == Decision.ALLOW:
            return self
        raise PolicyDenied(
            f"security authority refused action: {self.decision.value} — "
            f"{'; '.join(self.evaluation.reasons)}")


def request(agent, action_name: str, resource: dict | None = None,
            content: list | None = None,
            action_class: ActionClass | None = None,
            destination: str | None = None,
            evidence: dict | None = None,
            facts: dict | None = None,
            approvals: ApprovalStore | None = None,
            manifest: CapabilityManifest | None = None,
            safe_mode: bool | None = None,
            ledger_path: str | None = None) -> InterceptionResult:
    """Run one action through the full enforcement flow.

    agent:      Identity instance or registered identity name.
    content:    list of Content or (text, source_type) tuples — untrusted
                items are injection-scanned.
    facts:      caller-asserted facts for conditional policy rules
                (e.g. blocker-resolution conditions).
    manifest:   caller's capability manifest; defaults to the engine
                identity's manifest for the bootstrap identity, else empty
                (default deny).
    safe_mode:  True may engage locally; False cannot disable the flag.
    """
    from ..ledger.events import SecurityLedger
    from ..response.safe_mode import SafeMode

    identity = _resolve_identity(agent)
    cls, cls_reason = (classify(action_name) if action_class is None
                       else (action_class, "explicit"))
    if manifest is None:
        manifest = (CapabilityManifest(_ENGINE_CAPABILITIES)
                    if (identity.id == _ENGINE_IDENTITY_ID
                        and identity.kind == "service"
                        and identity.name == _ENGINE_IDENTITY_NAME
                        and not identity.parent_id)
                    else CapabilityManifest(()))

    # --- injection check: scan every untrusted content item ---
    items: list[Content] = []
    for c in content or []:
        if isinstance(c, Content):
            items.append(c)
        else:
            text, source_type = c
            items.append(Content.from_source(text, source_type))
    injection_findings = []
    highest_sev = None
    sev_order = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    for item in items:
        if item.is_trusted():
            continue
        res = scan(item.text)
        for f in res.findings:
            injection_findings.append(
                {"source": item.source_type or item.origin.value,
                 "finding": f.to_dict()})
        if res.highest_severity and (
                highest_sev is None or
                sev_order[res.highest_severity] > sev_order[highest_sev]):
            highest_sev = res.highest_severity

    # --- data/secret check: DLP on detected sensitivities ---
    dlp_violations: list[str] = []
    if destination is not None:
        field_sens = {}
        for i, item in enumerate(items):
            field_sens[f"content[{i}]"] = max_sensitivity(classify_text(item.text))
        if not field_sens:
            field_sens["request"] = Sensitivity.INTERNAL
        dlp_violations = _dlp.check(field_sens, destination)

    # --- policy engine ---
    safe_mode = SafeMode().is_engaged() or safe_mode is True
    evaluation = evaluate(
        agent={"identity": identity, "manifest": manifest,
               "delegation": None},
        action={"name": action_name, "action_class": cls},
        resource=resource or {"type": "unspecified",
                              "sensitivity": "INTERNAL"},
        context={"safe_mode": safe_mode,
                 "injection_severity": highest_sev,
                 "evidence": evidence,
                 "facts": facts or {},
                 "environment": "production"})

    # DLP is a hard refusal; an ordinary approval cannot override it.
    if dlp_violations:
        evaluation.decision = Decision.DENY
        evaluation.reasons.extend(dlp_violations)

    # --- approval gate ---
    approval_id = ""
    decision = evaluation.decision
    if decision == Decision.REQUIRE_APPROVAL and approvals is not None:
        from .approval_gate import digest_resource
        match = approvals.find_match(identity.id, action_name, resource)
        if match is not None and approvals.consume(match.approval_id):
            approval_id = match.approval_id
            decision = Decision.ALLOW
            evaluation.decision = decision
            evaluation.reasons.append(
                f"human approval {approval_id} presented and consumed")

    # --- security ledger (always, including denials) ---
    redacted_inputs, _ = redact(
        ";".join(i.text[:200] for i in items), level="log")
    ledger = SecurityLedger(ledger_path)
    event = ledger.record(
        agent_id=identity.id, parent_agent_id=identity.parent_id,
        requested_capability=evaluation.required_capability,
        action_name=action_name, action_class=cls,
        input_hashes={f"content[{i}]": it.sha256
                      for i, it in enumerate(items)},
        decision=decision, reasons=evaluation.reasons,
        risk_score=evaluation.risk_score,
        risk_factors=evaluation.risk_factors,
        resulting_action="authorized" if decision == Decision.ALLOW
        else "refused",
        approval_id=approval_id,
        redacted_input_preview=redacted_inputs[:500])

    return InterceptionResult(
        decision=decision, evaluation=evaluation,
        injection_findings=injection_findings, dlp_violations=dlp_violations,
        ledger_seq=event["seq"], approval_id=approval_id)


_PLACEHOLDER_NOTES = frozenset({"", "note", "placeholder", "n/a", "tbd"})


def request_submission_authorization(
        agent_hint: str = _ENGINE_IDENTITY_NAME,
        ats: str = "", technique: str = "", note: str = "",
        role_id: str = "", company: str = "",
        ledger_path: str | None = None) -> InterceptionResult:
    """Choke-point helper for the submission path (record_outcome).

    The note carries the confirmation quote — ATS-derived, therefore
    UNTRUSTED content: it is injection-scanned before any submission is
    recorded. Empty or placeholder-literal notes are not confirmation
    evidence and refuse. Raises PolicyDenied unless the authority ALLOWs.
    """
    clean_note = (note or "").strip()
    has_evidence = (clean_note and
                    clean_note.lower() not in _PLACEHOLDER_NOTES)
    evidence = ({"ats": ats, "technique": technique, "note": note,
                 "role_id": role_id, "company": company}
                if has_evidence else None)
    result = request(
        agent_hint, "record_submission",
        resource={"type": "telemetry_event", "id": role_id or "unknown",
                  "sensitivity": "INTERNAL"},
        content=[(note or "", "ats_response"),
                 (technique or "", "ats_response"),
                 (company or "", "ats_response")],
        action_class=ActionClass.SUBMISSION,
        evidence=evidence,
        ledger_path=ledger_path)
    return result.enforce()


def request_dispatch_authorization(
        agent_hint: str = _ENGINE_IDENTITY_NAME,
        ats: str = "", technique: str = "", role_id: str = "",
        company: str = "", fingerprint: str = "",
        ledger_path: str | None = None) -> InterceptionResult:
    """Choke-point helper for the DISPATCH path (api_submit, browser briefs).

    Called BEFORE any network request is made. The evidence is the
    content-addressed submission bundle fingerprint — the exact bytes the
    authority is being asked to let out. Same record_submission action and
    capability as request_submission_authorization (the engine identity
    already holds it); this is the same action enforced earlier, at the
    last moment the request can still be stopped.

    Empty/missing fingerprint -> no evidence -> the SUBMISSION class rule
    (allow_if_capability_and_evidence) DENYs (fail closed). Raises
    PolicyDenied unless the authority ALLOWs.
    """
    clean_fp = (fingerprint or "").strip()
    evidence = ({"ats": ats, "technique": technique, "role_id": role_id,
                 "company": company, "bundle_fingerprint": clean_fp}
                if clean_fp else None)
    result = request(
        agent_hint, "record_submission",
        resource={"type": "submission_bundle", "id": role_id or "unknown",
                  "sensitivity": "INTERNAL"},
        content=[(clean_fp, "dispatch_bundle"),
                 (technique or "", "dispatch_context"),
                 (company or "", "dispatch_context")],
        action_class=ActionClass.SUBMISSION,
        evidence=evidence,
        ledger_path=ledger_path)
    return result.enforce()
