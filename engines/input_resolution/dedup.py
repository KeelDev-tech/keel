#!/usr/bin/env python3
"""Cross-application blocker dedup.

Collapses semantically identical blockers across leads into decision families.
Paraphrase-robust: "Can we record the interview?" and "Do you consent to
transcription?" collapse — subject to employer wording. Materially
different wording (video vs audio; stored vs not; different percentages)
creates separate variants that do NOT collapse.

Deterministic, token-based, no ML. O(n) average via family bucketing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .blocker import Blocker


@dataclass
class DecisionFamily:
    family: str
    variant: str
    blocker_ids: list = field(default_factory=list)   # lead_id list
    member_indices: list = field(default_factory=list)
    representative_text: str = ""
    requires_applicant: bool = False
    classification: str = ""
    decision_prompt: str = ""
    affected_roles: list = field(default_factory=list)  # (company, role)


DECISION_PROMPTS = {
    "travel_commitment": ("TRAVEL DECISION REQUIRED — one judgment covers every listed "
                          "application. These roles list generic, regular, unspecified, or "
                          "open-ended travel, which parks for your judgment per the standing "
                          "travel rule (defined travel within the standing cap on a set "
                          "schedule is already auto-accepted; it never reaches this prompt). "
                          "Reply ACCEPT as described, KEEP PARKED, or set a standing "
                          "travel rule."),
    "interview_recording_consent": ("INTERVIEW RECORDING CONSENT — applies to all listed applications. "
                                    "Choose: Allow / Decline / Case-by-case"),
    "arbitration_agreement": ("ARBITRATION AGREEMENT — employer/legal-specific, shown per employer. "
                              "Never blanket-accepted."),
    "generic_attestation": ("ATTESTATION — verify the exact attestation text per employer before the applicant attests. "
                            "Never blanket-accepted; one decision per identical attestation text."),
    "ai_evaluation_consent": ("AI EVALUATION CONSENT — personal attestation, per employer. "
                               "Choose per application."),
    "office_frequency": ("OFFICE FREQUENCY — standing rule is the applicant's own hybrid cap. Listed applications "
                         "exceed it or are ambiguous; confirm exceptions per role or park."),
    "relocation_willingness": ("RELOCATION — standing rule is the applicant's own relocation willingness. "
                                "Confirm or add exceptions."),
    "recruiting_sms_consent": ("RECRUITING SMS CONSENT — choose: Allow / Decline (one decision, all applications)."),
    "recruiting_whatsapp_consent": ("RECRUITING WHATSAPP CONSENT — choose: Allow / Decline (one decision, all applications)."),
    "no_ai_unaided_writing": ("HUMAN-ONLY WRITING — employer requires unaided writing. Factual memory aids only; no drafting."),
    "personally_completed_certification": ("PERSONALLY-COMPLETED CERTIFICATION — human completion path required."),
    "truthfulness_certification": ("TRUTHFULNESS CERTIFICATION — review application, then certify personally."),
    "compensation_expectation": ("COMPENSATION — one reusable strategy resolves all: confirm the market-standing rule or set exception."),
    "how_heard_source": ("HOW DID YOU HEAR ABOUT US — standing answer 'Other'; confirm or map provenance per employer."),
    "apply_by_email_authorization": ("APPLY-BY-EMAIL — standing rule: agents never send under the applicant's identity. Authorize once globally or keep manual."),
    "work_authorization": ("WORK AUTHORIZATION — verify once from records; one answer reused."),
    "start_timeframe": ("START TIMEFRAME — standing answer 'Next day'; confirm or set exception."),
}


def _canonical_index(blockers: list[Blocker]) -> dict[int, int]:
    """First-seen index per (family, variant, classification-compat) group.

    Only blockers that survived the proof as applicant-requiring participate
    in decision families. Resolved/false/duplicate/tool/policy/manual
    blockers never enter the tap list (policy conflicts and manual actions
    are reported separately, not as decisions).
    """
    SKIP = {"resolved_auto", "false_blocker", "duplicate", "dropped_stalled",
            "automation_retry", "agent_verification_required",
            "form_verification_required", "policy_conflict", "manual_takeover"}
    groups: dict[tuple, list[int]] = {}
    for i, b in enumerate(blockers):
        if b.classification in SKIP:
            continue
        key = (b.normalized_family, b.family_variant)
        groups.setdefault(key, []).append(i)
    return groups


def collapse(blockers: list[Blocker]) -> list[DecisionFamily]:
    """Collapse blockers into decision families. Marks duplicates in place.

    The first member of each multi-member group is the canonical decision;
    later members get duplicate_of set to the canonical lead_id and
    classification 'duplicate' (requires_applicant=False).

    INTEGRITY RULE: 'unknown'-family blockers NEVER collapse with each other.
    An unclassifiable blocker is its own decision — collapsing unknowns would
    manufacture a fake decision the applicant cannot act on.
    """
    groups = _canonical_index(blockers)
    families: list[DecisionFamily] = []
    for (fam, variant), idxs in sorted(groups.items()):
        members = [blockers[i] for i in idxs]
        if fam == "unknown":
            for m in members:
                families.append(DecisionFamily(
                    family=fam, variant=m.raw_blocker[:60],
                    blocker_ids=[m.lead_id], member_indices=[],
                    representative_text=m.raw_blocker,
                    requires_applicant=m.requires_applicant,
                    classification=m.classification,
                    decision_prompt="INDIVIDUAL REVIEW — unclassified blocker; one ask per item.",
                    affected_roles=[(m.company, m.role)],
                ))
            continue
        canon = members[0]
        for dup in members[1:]:
            dup.duplicate_of = canon.lead_id
            dup.classification = "duplicate"
            dup.requires_applicant = False
            dup.reason = (f"duplicate of {canon.lead_id} — collapsed into "
                          f"decision family '{fam}/{variant}'")
            dup.resolution = "collapsed"
            dup.agent_next_action = "covered by canonical family decision"
        families.append(DecisionFamily(
            family=fam,
            variant=variant,
            # Dedupe by lead: a lead with two blockers in one family counts
            # as ONE affected application.
            blocker_ids=list(dict.fromkeys(m.lead_id for m in members)),
            member_indices=idxs,
            representative_text=canon.raw_blocker,
            requires_applicant=canon.requires_applicant,
            classification=canon.classification,
            decision_prompt=DECISION_PROMPTS.get(fam, f"DECISION REQUIRED — {fam}"),
            affected_roles=list(dict.fromkeys((m.company, m.role) for m in members))[:8],
        ))
    return families


def singleton_decisions(blockers: list[Blocker]) -> list[DecisionFamily]:
    """One-off genuine decisions that did not collapse (single-member groups
    are already DecisionFamily entries; this is a convenience alias)."""
    return [f for f in collapse(blockers) if len(f.blocker_ids) == 1]
