"""Data lineage for the Privacy Release Controller.

Every releasable artifact must be able to answer: where did these bytes come
from, and what transformations produced them? The LineageGraph records
source -> transform* -> artifact edges. Counsel reviews the rendered chain;
gaps (unknown sources, undocumented transforms) are reported, never papered
over — an incomplete chain is a packet defect, not a silent pass.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class LineageInput:
    """One upstream input to an artifact."""
    input_id: str            # e.g. "queue/standard-queue.json" or an artifact_id
    input_digest: str = ""   # sha256 when known; "" = UNKNOWN (flagged as a gap)
    input_kind: str = ""     # e.g. "file", "api_response", "human_entry", "unknown"


@dataclass
class LineageTransform:
    """One deterministic transformation step, in order."""
    step: int
    name: str                # e.g. "dedup by role_id", "k-anonymize bucket>=5"
    description: str = ""
    code_ref: str = ""       # path of the code that performs it, when known


@dataclass
class LineageRecord:
    artifact_id: str
    inputs: list[LineageInput] = field(default_factory=list)
    transforms: list[LineageTransform] = field(default_factory=list)
    produced_by: str = ""
    produced_at: str = field(default_factory=utcnow_iso)

    def gaps(self) -> list[str]:
        """Return human-readable lineage gaps; empty means the chain is complete."""
        gaps: list[str] = []
        if not self.inputs and not self.transforms:
            gaps.append(f"{self.artifact_id}: no inputs and no transforms recorded "
                        f"(origin unknown)")
            return gaps
        for inp in self.inputs:
            if not inp.input_digest:
                gaps.append(f"{self.artifact_id}: input {inp.input_id!r} has no digest "
                            f"(UNKNOWN provenance)")
            if not inp.input_kind or inp.input_kind == "unknown":
                gaps.append(f"{self.artifact_id}: input {inp.input_id!r} has unknown kind")
        steps = [t.step for t in self.transforms]
        if steps and (sorted(steps) != list(range(1, len(steps) + 1)) or
                      [t.step for t in self.transforms] != sorted(steps)):
            gaps.append(f"{self.artifact_id}: transform steps are not a clean 1..N sequence")
        for t in self.transforms:
            if not t.name:
                gaps.append(f"{self.artifact_id}: transform step {t.step} is unnamed")
        return gaps

    def to_dict(self) -> dict:
        return asdict(self)


class LineageGraph:
    """Collection of per-artifact lineage records with completeness checks."""

    def __init__(self) -> None:
        self.records: dict[str, LineageRecord] = {}

    def add(self, record: LineageRecord) -> None:
        self.records[record.artifact_id] = record

    def verify_chain(self, artifact_id: str) -> tuple[bool, list[str]]:
        """(complete, gaps) — complete is True only when zero gaps exist."""
        record = self.records.get(artifact_id)
        if record is None:
            return False, [f"{artifact_id}: no lineage record at all"]
        gaps = record.gaps()
        return (len(gaps) == 0), gaps

    def verify_all(self) -> dict[str, list[str]]:
        """artifact_id -> gaps, for every recorded artifact."""
        return {aid: rec.gaps() for aid, rec in self.records.items()}

    def to_dict(self) -> dict:
        return {
            "generated_at": utcnow_iso(),
            "records": {aid: rec.to_dict() for aid, rec in self.records.items()},
            "completeness": self.verify_all(),
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)
            f.write("\n")

    @classmethod
    def load(cls, path: str) -> "LineageGraph":
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        graph = cls()
        for aid, rec in payload.get("records", {}).items():
            graph.add(LineageRecord(
                artifact_id=aid,
                inputs=[LineageInput(**i) for i in rec.get("inputs", [])],
                transforms=[LineageTransform(**t) for t in rec.get("transforms", [])],
                produced_by=rec.get("produced_by", ""),
                produced_at=rec.get("produced_at", ""),
            ))
        return graph
