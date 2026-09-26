"""Memory provenance: every mutation carries its lineage.

source, writer, timestamp, confidence, sensitivity, verification,
content_hash. Untrusted web content is never silently promoted to
trusted long-term memory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

VERIFICATION_STATES = frozenset({"verified", "unverified", "quarantined"})


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8",
                                         errors="replace")).hexdigest()


@dataclass
class Provenance:
    source: str          # e.g. "ats_response", "operator_message"
    writer: str          # identity id of the writer
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence: float = 0.5
    sensitivity: str = "INTERNAL"
    verification: str = "unverified"
    content_hash: str = ""

    @classmethod
    def create(cls, content: str, source: str, writer: str,
               confidence: float = 0.5, sensitivity: str = "INTERNAL",
               verification: str = "unverified") -> "Provenance":
        return cls(source=source, writer=writer, confidence=confidence,
                   sensitivity=sensitivity.upper(), verification=verification,
                   content_hash=hash_content(content))

    def to_dict(self) -> dict:
        return {"source": self.source, "writer": self.writer,
                "timestamp": self.timestamp, "confidence": self.confidence,
                "sensitivity": self.sensitivity,
                "verification": self.verification,
                "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        return cls(source=d.get("source", ""), writer=d.get("writer", ""),
                   timestamp=d.get("timestamp", ""),
                   confidence=d.get("confidence", 0.5),
                   sensitivity=d.get("sensitivity", "INTERNAL"),
                   verification=d.get("verification", "unverified"),
                   content_hash=d.get("content_hash", ""))


@dataclass
class MemoryEntry:
    key: str
    content: str
    provenance: Provenance

    def to_dict(self) -> dict:
        return {"key": self.key, "content": self.content,
                "provenance": self.provenance.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(key=d.get("key", ""), content=d.get("content", ""),
                   provenance=Provenance.from_dict(d.get("provenance", {})))


def canonical_json(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")
