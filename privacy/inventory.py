"""Artifact inventory for the Privacy Release Controller.

Every artifact that may attempt to leave the private workspace is inventoried
first: identity, byte content digest (SHA-256), size, and media type. The
inventory is the exact artifact list the counsel packet carries, so it must be
complete and content-addressed — a filename alone is never an identity.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hash_bytes(data: bytes) -> str:
    """SHA-256 hex digest of in-memory bytes."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: str | os.PathLike) -> str:
    """SHA-256 hex digest of a file's bytes, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ArtifactRecord:
    """One inventoried artifact. Identity = sha256 of content."""

    artifact_id: str          # stable human id within the release, e.g. "artifact-001"
    name: str                 # display name
    sha256: str               # content digest (the binding identity)
    size_bytes: int
    media_type: str           # MIME guess; "application/octet-stream" when unknown
    classification: str = "UNCLASSIFIED"  # ArtifactClass name; set by classifier.py
    external_egress: str = "DENY"         # default-deny; only counsel decision lifts
    source_note: str = ""     # where the bytes came from
    inventoried_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_path(cls, artifact_id: str, path: str | os.PathLike,
                  source_note: str = "") -> "ArtifactRecord":
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"artifact not found: {path}")
        media_type, _ = mimetypes.guess_type(p.name)
        return cls(
            artifact_id=artifact_id,
            name=p.name,
            sha256=hash_file(p),
            size_bytes=p.stat().st_size,
            media_type=media_type or "application/octet-stream",
            source_note=source_note or str(p),
        )


def inventory_artifacts(paths: list[str | os.PathLike],
                        source_note: str = "") -> list[ArtifactRecord]:
    """Inventory a list of file paths into content-addressed records.

    artifact_id values are assigned deterministically in input order.
    """
    records = []
    for i, path in enumerate(paths, start=1):
        records.append(
            ArtifactRecord.from_path(
                artifact_id=f"artifact-{i:03d}",
                path=path,
                source_note=source_note,
            )
        )
    return records


def save_inventory(records: list[ArtifactRecord], path: str | os.PathLike) -> None:
    payload = {
        "inventory_version": 1,
        "generated_at": utcnow_iso(),
        "artifacts": [r.to_dict() for r in records],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def load_inventory(path: str | os.PathLike) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return payload["artifacts"]
