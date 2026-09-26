"""Productized blocker inbox (prototype, G-3/B-24).

One decision inbox over parked blockers: every blocker appears once with its
gate citation, recurrence across leads, and answer provenance. Answering one
question retro-resolves every lead waiting on it.

Integrity pins (prototype enforces what the policy requires):
- A machine-suggested answer can NEVER enter the bank and can NEVER appear
  in a packet. The resolver abstains until a human confirms.
- Banked answers carry provenance (his_words | user_edited) AND the exact
  source message that confirmed them — traceable, never invented.
- Retro-resolution unblocks exactly the blocked set: no more, no fewer.
- Rejected suggestions leave the blocker open; nothing is silently closed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

HIS_WORDS = "his_words"
MACHINE_SUGGESTED = "machine_suggested"
USER_EDITED = "user_edited"
USER_REJECTED = "user_rejected"

BANKABLE = {HIS_WORDS, USER_EDITED}


def question_hash(question: str) -> str:
    normalized = " ".join(question.strip().lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


@dataclass
class Blocker:
    blocker_id: str
    lead_id: str
    question: str
    gate: str  # e.g. "needs_input:essay" — why it blocked, cited
    qhash: str = field(init=False)

    def __post_init__(self):
        self.qhash = question_hash(self.question)


@dataclass
class ProposedAnswer:
    qhash: str
    text: str
    provenance: str  # his_words | machine_suggested | user_edited
    source_message_id: Optional[str] = None  # the exact user message that confirmed it


@dataclass
class BankEntry:
    qhash: str
    text: str
    provenance: str
    source_message_id: str
    banked_at: str


class AnswerBank:
    def __init__(self):
        self.entries: Dict[str, BankEntry] = {}

    def bank(self, answer: ProposedAnswer, banked_at: str) -> BankEntry:
        """Bank an answer. Refuses machine-suggested and anything unconfirmed."""
        if answer.provenance not in BANKABLE:
            raise ValueError(
                f"refused: provenance '{answer.provenance}' is not bankable "
                "(suggested-but-unconfirmed answers never enter the bank)"
            )
        if not answer.source_message_id:
            raise ValueError("refused: banked answers must trace to a confirming user message")
        entry = BankEntry(
            qhash=answer.qhash,
            text=answer.text,
            provenance=answer.provenance,
            source_message_id=answer.source_message_id,
            banked_at=banked_at,
        )
        self.entries[answer.qhash] = entry
        return entry

    def packet_fill(self, qhash: str) -> Optional[str]:
        """Packet resolver: banked + confirmed only. Otherwise abstain."""
        entry = self.entries.get(qhash)
        if entry is None or entry.provenance not in BANKABLE:
            return None
        return entry.text


class BlockerInbox:
    def __init__(self):
        self.blockers: Dict[str, Blocker] = {}
        self.bank = AnswerBank()
        self.rejected: Dict[str, str] = {}  # qhash -> rejected suggestion text

    def add_blocker(self, blocker: Blocker) -> None:
        self.blockers[blocker.blocker_id] = blocker

    def cards(self) -> List[Dict]:
        """One card per distinct question: question, gate, recurrence, lead set."""
        by_q: Dict[str, Dict] = {}
        for b in self.blockers.values():
            card = by_q.setdefault(b.qhash, {
                "question": b.question,
                "qhash": b.qhash,
                "gate": b.gate,
                "lead_ids": [],
            })
            if b.lead_id not in card["lead_ids"]:
                card["lead_ids"].append(b.lead_id)
        for card in by_q.values():
            card["recurrence"] = len(card["lead_ids"])
            card["lead_ids"] = sorted(card["lead_ids"])
        return sorted(by_q.values(), key=lambda c: (-c["recurrence"], c["qhash"]))

    def recurrence_view(self) -> List[Dict]:
        """Patterns first: the questions that repeat across leads."""
        return self.cards()

    def answer_question(self, answer: ProposedAnswer, banked_at: str) -> Dict:
        """User answers once -> bank (provenance-checked) -> retro-resolve.

        Returns the honest before/after: how many leads unblocked, how many
        blockers remain. Unblocks exactly the blocked set: no more, no fewer.
        """
        before = len(self.blockers)
        entry = self.bank.bank(answer, banked_at)
        unblocked = sorted({b.lead_id for b in self.blockers.values() if b.qhash == answer.qhash})
        resolved_ids = [bid for bid, b in self.blockers.items() if b.qhash == answer.qhash]
        for bid in resolved_ids:
            del self.blockers[bid]
        after = len(self.blockers)
        return {
            "banked_provenance": entry.provenance,
            "source_message_id": entry.source_message_id,
            "unblocked_leads": unblocked,
            "unblocked_count": len(unblocked),
            "blockers_before": before,
            "blockers_after": after,
        }

    def reject_suggestion(self, qhash: str, suggestion_text: str) -> None:
        """User rejects a machine suggestion: recorded, blocker stays open."""
        self.rejected[qhash] = suggestion_text
