#!/usr/bin/env python3
"""Compression metrics.

Primary metric: leads_unlocked_per_decision — the number of leads whose tray
state improves per human decision (tap-list item). Secondary constraint:
zero fabricated application data (enforced by blocker.py fail-closed design;
this module only counts).

Supporting rates: false_blocker_rate, duplicate_question_rate,
answer_bank_reuse_rate, agent_resolvable_blocker_rate.
"""
from __future__ import annotations

from dataclasses import dataclass

from .blocker import Blocker
from .dedup import DecisionFamily


@dataclass
class CompressionReport:
    n_leads: int
    n_blockers: int
    auto_resolved: int
    false_blockers_removed: int
    answer_bank_resolutions: int
    duplicates_collapsed: int
    policy_conflicts: int
    manual_actions: int
    legal_attestation: int
    human_only_writing: int
    true_applicant_decisions: int     # tap-list items
    leads_with_outcome: int           # leads with >=1 resolved/collapsed/decidable blocker
    compression_rate: float           # 1 - tap_items / blockers
    leads_unlocked_per_decision: float
    false_blocker_rate: float
    duplicate_question_rate: float
    answer_bank_reuse_rate: float
    agent_resolvable_blocker_rate: float


HUMAN_ONLY = {"human_authorship_required", "human_completion_required",
              "legal_attestation", "essay_applicant_only"}
MANUAL = {"manual_takeover"}
TAP_TRUTHY = {"user_fact", "user_preference", "user_self_assessment",
              "user_compensation", "user_role_exception", "essay_applicant_only",
              "legal_attestation", "human_authorship_required",
              "human_completion_required"}


def compute(blockers: list[Blocker], families: list[DecisionFamily]) -> CompressionReport:
    n = len(blockers)
    leads = {b.lead_id for b in blockers}
    auto = sum(1 for b in blockers if b.classification == "resolved_auto")
    false = sum(1 for b in blockers if b.classification == "false_blocker")
    bank = sum(1 for b in blockers if b.resolution and b.resolution.startswith("answer_bank:"))
    dups = sum(1 for b in blockers if b.classification == "duplicate")
    policy = sum(1 for b in blockers if b.classification == "policy_conflict")
    manual = sum(1 for b in blockers if b.classification in MANUAL)
    legal = sum(1 for b in blockers if b.classification == "legal_attestation")
    human_only = sum(1 for b in blockers if b.classification in HUMAN_ONLY)

    # Tap list = decision families requiring the applicant + one-off genuine
    # singles. Families already encode collapse; count each family once.
    tap_families = [f for f in families if f.requires_applicant]
    true_decisions = len(tap_families)

    # Leads with outcome: >=1 blocker resolved, collapsed, parked-by-policy,
    # or folded into an applicant decision family (the decision will clear it).
    decided_ids = set()
    for f in families:
        decided_ids.update(f.blocker_ids)
    outcome_leads = {b.lead_id for b in blockers
                     if b.classification in {"resolved_auto", "false_blocker",
                                             "policy_conflict", "duplicate"}
                     or b.lead_id in decided_ids}
    leads_outcome = len(outcome_leads)

    compression = 1.0 - (true_decisions / n) if n else 0.0
    lupd = (leads_outcome / true_decisions) if true_decisions else 0.0

    def rate(x):
        return x / n if n else 0.0

    return CompressionReport(
        n_leads=len(leads),
        n_blockers=n,
        auto_resolved=auto,
        false_blockers_removed=false,
        answer_bank_resolutions=bank,
        duplicates_collapsed=dups,
        policy_conflicts=policy,
        manual_actions=manual,
        legal_attestation=legal,
        human_only_writing=human_only,
        true_applicant_decisions=true_decisions,
        leads_with_outcome=leads_outcome,
        compression_rate=compression,
        leads_unlocked_per_decision=lupd,
        false_blocker_rate=rate(false),
        duplicate_question_rate=rate(dups),
        answer_bank_reuse_rate=rate(bank),
        agent_resolvable_blocker_rate=rate(auto + false),
    )


def optimization_log_entry(report: CompressionReport, baseline_lupd: float,
                           evidence: list[str]) -> dict:
    """Build (do NOT append) an optimization-log entry. Appending is a
    parent-authorized live write; dry-run only prepares the entry."""
    return {
        "name": "input_resolution_compression",
        "title": "Input Resolution & Blocker Compression Engine (dry-run)",
        "hypothesis": ("Normalizing blockers with a 10-point pre-escalation proof and "
                       "collapsing duplicates across applications raises leads unlocked "
                       "per human decision without fabricating data."),
        "baseline_metric": (
            f"leads_unlocked_per_decision={baseline_lupd:.2f} "
            f"(per-lead tray, no dedup; {report.n_blockers} blockers across "
            f"{report.n_leads} leads)"
        ),
        "post_metric": (
            f"leads_unlocked_per_decision={report.leads_unlocked_per_decision:.2f}; "
            f"compression_rate={report.compression_rate:.2%}; "
            f"true_applicant_decisions={report.true_applicant_decisions}; "
            f"auto_resolved={report.auto_resolved}; "
            f"false_blocker_rate={report.false_blocker_rate:.2%}; "
            f"duplicate_question_rate={report.duplicate_question_rate:.2%}; "
            f"answer_bank_reuse_rate={report.answer_bank_reuse_rate:.2%}; "
            f"agent_resolvable_blocker_rate={report.agent_resolvable_blocker_rate:.2%}"
        ),
        "verdict": "inconclusive",  # dry-run only; parent decides after live review
        "evidence": evidence,
    }
