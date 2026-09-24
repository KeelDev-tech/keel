"""Quarantine: untrusted memory sits here until a trusted verifier vouches.

hold() stores an entry as quarantined. promote() requires a trusted
verifier identity plus non-empty evidence; anything else stays put.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..errors import MemoryViolation
from ..injection.content_classifier import (TRUSTED_ORIGINS, Origin,
                                            classify_source)
from .provenance import MemoryEntry


@dataclass
class QuarantinedItem:
    quarantine_id: str
    entry: MemoryEntry
    reason: str
    quarantined_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    promoted: bool = False
    promoted_by: str = ""
    promote_evidence: str = ""


class QuarantineStore:
    def __init__(self):
        self._items: dict[str, QuarantinedItem] = {}

    def hold(self, entry: MemoryEntry, reason: str) -> QuarantinedItem:
        qid = "q_" + secrets.token_hex(8)
        item = QuarantinedItem(quarantine_id=qid, entry=entry, reason=reason)
        # The stored copy is always marked quarantined, never verified.
        item.entry.provenance.verification = "quarantined"
        self._items[qid] = item
        return item

    def promote(self, quarantine_id: str, verifier_origin: Origin,
                verifier_id: str, evidence: str) -> MemoryEntry:
        """Promote a quarantined entry to verified memory.

        Requires: a TRUSTED verifier origin, a verifier identity, and
        non-empty evidence. Anything else raises and the item stays.
        """
        item = self._items.get(quarantine_id)
        if item is None:
            raise MemoryViolation(
                f"quarantine: unknown id {quarantine_id!r}")
        if item.promoted:
            raise MemoryViolation("quarantine: item already promoted")
        if verifier_origin not in TRUSTED_ORIGINS:
            raise MemoryViolation(
                "quarantine: promoter must be a trusted origin "
                f"(got {verifier_origin.value})")
        if not verifier_id or not verifier_id.strip():
            raise MemoryViolation("quarantine: verifier identity required")
        if not evidence or not evidence.strip():
            raise MemoryViolation(
                "quarantine: promotion requires evidence — refusing")
        item.promoted = True
        item.promoted_by = verifier_id
        item.promote_evidence = evidence.strip()
        item.entry.provenance.verification = "verified"
        return item.entry

    def get(self, quarantine_id: str) -> QuarantinedItem | None:
        return self._items.get(quarantine_id)

    def pending(self) -> list[QuarantinedItem]:
        return [i for i in self._items.values() if not i.promoted]

    def clear(self) -> None:
        """Test support only — never call in production paths."""
        self._items.clear()
