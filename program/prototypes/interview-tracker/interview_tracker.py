"""Interview tracker + outcome feedback loop (prototype, G-4/B-26 + G-7/PP-12).

PROTOTYPE ONLY — never wired to production. No live ledger, no mailbox
connector, no real outcome ingestion. G1 privacy-counsel sign-off gates all
of the real data handling.

Tracker rules (pinned so tests guard them):
  States: SUBMITTED -> REJECTED | WITHDRAWN | INTERVIEW_SCHEDULED | OFFER
          | HIRED | EXPIRED_UNKNOWN.
  Evidence kinds: user_tap | authorized_signal | cohort_reconciliation.
  A state change with any other kind (vendor claim, inference, guess) is
  REJECTED with ValueError — never recorded.
  authorized_signal requires details={"source": <s>} where <s> is an
  authorized source; mailbox defaults to UNAUTHORIZED (fail closed) until the
  user authorizes the connection.
  Terminal states (REJECTED/WITHDRAWN/HIRED/EXPIRED_UNKNOWN) cannot be left
  except by cohort_reconciliation (honest correction path, fully logged).
  EXPIRED_UNKNOWN is first-class: stale SUBMITTED rows are reported as
  unknown-with-denominator, never dropped, never counted as successes.

Funnel view (PP-12): derived ONLY from tracker + cohort data. Every figure
carries a provenance label or is labeled UNMEASURED. No inferred claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

SUBMITTED = "SUBMITTED"
REJECTED = "REJECTED"
WITHDRAWN = "WITHDRAWN"
INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
OFFER = "OFFER"
HIRED = "HIRED"
EXPIRED_UNKNOWN = "EXPIRED_UNKNOWN"

ALL_STATES = {SUBMITTED, REJECTED, WITHDRAWN, INTERVIEW_SCHEDULED, OFFER, HIRED, EXPIRED_UNKNOWN}
TERMINAL = {REJECTED, WITHDRAWN, HIRED, EXPIRED_UNKNOWN}

EVIDENCE_USER_TAP = "user_tap"
EVIDENCE_AUTHORIZED_SIGNAL = "authorized_signal"
EVIDENCE_COHORT_RECONCILIATION = "cohort_reconciliation"
EVIDENCE_KINDS = {EVIDENCE_USER_TAP, EVIDENCE_AUTHORIZED_SIGNAL, EVIDENCE_COHORT_RECONCILIATION}


@dataclass
class Submission:
    submission_id: str
    bundle_id: str
    submitted_at: datetime
    state: str = SUBMITTED
    history: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        self.history.append({
            "from": None,
            "to": SUBMITTED,
            "at": self.submitted_at.isoformat(),
            "evidence": EVIDENCE_COHORT_RECONCILIATION,
            "details": "initial submission recorded from ledger",
        })


class Tracker:
    def __init__(self, authorized_sources: Optional[Dict[str, bool]] = None):
        # Fail closed: nothing is an authorized signal source until listed True.
        # Mailbox signals are disabled until the user authorizes the connection.
        self.authorized_sources: Dict[str, bool] = dict(authorized_sources or {})
        self._subs: Dict[str, Submission] = {}

    # ---- registration ----

    def register(self, submission_id: str, bundle_id: str, submitted_at: datetime) -> Submission:
        if submission_id in self._subs:
            raise ValueError(f"duplicate submission_id: {submission_id}")
        sub = Submission(submission_id=submission_id, bundle_id=bundle_id, submitted_at=submitted_at)
        self._subs[submission_id] = sub
        return sub

    def authorize_source(self, source: str) -> None:
        """User authorizes a signal source (e.g. mailbox). Nothing else may call this."""
        self.authorized_sources[source] = True

    # ---- state transitions ----

    def _validate_evidence(self, evidence: Dict[str, Any]) -> str:
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be a dict with kind + details — refusing to advance")
        kind = evidence.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ValueError(
                f"unauthorized/inferred evidence kind rejected: {kind!r} "
                f"(allowed: {sorted(EVIDENCE_KINDS)})"
            )
        if kind == EVIDENCE_AUTHORIZED_SIGNAL:
            source = (evidence.get("details") or {}).get("source")
            if not self.authorized_sources.get(source):
                raise ValueError(
                    f"signal source {source!r} is not authorized — mailbox and all "
                    "unauthorized sources fail closed"
                )
        if kind == EVIDENCE_USER_TAP and not (evidence.get("details") or {}).get("at"):
            raise ValueError("user_tap evidence must carry a timestamp in details")
        return kind

    def advance(self, submission_id: str, new_state: str, evidence: Dict[str, Any]) -> Submission:
        """Advance one submission. Rejects invalid states, terminal-state
        escapes without cohort_reconciliation, and any bad evidence."""
        if submission_id not in self._subs:
            raise ValueError(f"unknown submission_id: {submission_id}")
        if new_state not in ALL_STATES:
            raise ValueError(f"invalid state: {new_state}")
        kind = self._validate_evidence(evidence)
        sub = self._subs[submission_id]
        if sub.state in TERMINAL and new_state != sub.state and kind != EVIDENCE_COHORT_RECONCILIATION:
            raise ValueError(
                f"{submission_id} is in terminal state {sub.state}: only "
                "cohort_reconciliation may correct it"
            )
        if new_state == sub.state:
            raise ValueError(f"{submission_id} already in {new_state} — no-op transitions are not recorded")
        sub.history.append({
            "from": sub.state,
            "to": new_state,
            "at": datetime.utcnow().isoformat() + "Z",
            "evidence": kind,
            "details": evidence.get("details"),
        })
        sub.state = new_state
        return sub

    # ---- expiry ----

    def expire_unknown(self, as_of: datetime, window_days: int) -> Dict[str, Any]:
        """Cohort operation: stale SUBMITTED rows become EXPIRED_UNKNOWN.
        Returns the cohort with its denominator. Never drops, never counts
        unknowns as successes."""
        cutoff = as_of - timedelta(days=window_days)
        expired: List[str] = []
        for sub in self._subs.values():
            if sub.state == SUBMITTED and sub.submitted_at < cutoff:
                self.advance(sub.submission_id, EXPIRED_UNKNOWN, {
                    "kind": EVIDENCE_COHORT_RECONCILIATION,
                    "details": {
                        "reason": "expired_unknown",
                        "window_days": window_days,
                        "cutoff": cutoff.isoformat(),
                    },
                })
                expired.append(sub.submission_id)
        return {
            "state": EXPIRED_UNKNOWN,
            "submission_ids": expired,
            "denominator": len(self._subs),
            "window_days": window_days,
            "as_of": as_of.isoformat(),
        }

    # ---- reconciliation ----

    def reconcile(self, ledger_submission_ids: List[str]) -> Tuple[bool, List[str], List[str]]:
        """Assert the tracker and the ledger agree 1:1.
        Returns (ok, missing_from_tracker, extra_in_tracker)."""
        ledger: Set[str] = set(ledger_submission_ids)
        if len(ledger) != len(ledger_submission_ids):
            raise ValueError("ledger contains duplicate submission_ids")
        tracked = set(self._subs.keys())
        missing = sorted(ledger - tracked)
        extra = sorted(tracked - ledger)
        return (not missing and not extra, missing, extra)

    # ---- funnel view (PP-12) ----

    def _provenance_for(self, sub: Submission) -> str:
        kinds = {h["evidence"] for h in sub.history if h["from"] is not None}
        if not kinds:
            return "ledger_registered"
        if kinds == {EVIDENCE_USER_TAP}:
            return "user_tap"
        if kinds == {EVIDENCE_AUTHORIZED_SIGNAL}:
            return "authorized_signal"
        if kinds == {EVIDENCE_COHORT_RECONCILIATION}:
            return "cohort_reconciliation"
        return "mixed_evidence"

    def funnel_view(self) -> Dict[str, Any]:
        """Funnel derived only from tracker + cohort data.
        applications -> responses -> interviews; unknowns shown, never folded
        into successes. Every figure carries a provenance label."""
        counts = {s: 0 for s in ALL_STATES}
        for sub in self._subs.values():
            counts[sub.state] += 1
        total = len(self._subs)
        interviews = counts[INTERVIEW_SCHEDULED] + counts[OFFER] + counts[HIRED]
        responses = interviews + counts[REJECTED]
        successes = counts[OFFER] + counts[HIRED]

        def cell(n: int) -> Dict[str, Any]:
            return {
                "count": n,
                "denominator": total,
                "share_of_applications": (n / total) if total else None,
                "provenance": "tracker_states" if total else "UNMEASURED",
            }

        return {
            "applications": cell(total),
            "responses": cell(responses),            # any employer response observed
            "interviews": cell(interviews),          # scheduled + offer + hired
            "offers": cell(successes),               # offer + hired
            "hires": cell(counts[HIRED]),
            "rejected": cell(counts[REJECTED]),
            "withdrawn": cell(counts[WITHDRAWN]),
            "expired_unknown": {**cell(counts[EXPIRED_UNKNOWN]),
                                "note": "unknown-with-denominator; never counted as successes"},
            "submitted_pending": cell(counts[SUBMITTED]),
            "uncertainty_note": ("State changes require user_tap, authorized_signal, "
                                 "or cohort_reconciliation. Anything else is rejected, not inferred."),
        }
