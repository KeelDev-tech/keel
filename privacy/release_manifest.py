"""Content-addressed release manifests for the Privacy Release Controller.

build_release issues a new release ID. The manifest digest detects changes
unless the manifest is resealed; it is not a signature or an authority.
Reviewer signatures bind release_id and artifact_digest, not destination or
all manifest metadata. The sender must independently bind its destination
and actual transmitted bytes. Changing artifact bytes invalidates the
existing artifact approval because the digest changes.
"""

from __future__ import annotations

import copy
import hashlib
import re
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from keel.privacy.inventory import ArtifactRecord


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _manifest_digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


@dataclass
class ReleaseManifest:
    release_id: str            # immutable once created, e.g. "PR-20260918-3f9a2c"
    created_at: str
    artifacts: list[dict]      # ArtifactRecord dicts (include sha256)
    metadata: dict             # intended_destination, purpose, version notes, ...
    manifest_digest: str = ""  # sha256 over the canonical manifest body

    def to_dict(self) -> dict:
        return asdict(self)


def build_release(artifacts: list[ArtifactRecord],
                  metadata: dict | None = None,
                  created_at: str | None = None) -> ReleaseManifest:
    """Create an immutable release over the given artifact records."""
    if not artifacts:
        raise ValueError("cannot build a release with zero artifacts")
    seen_digests = set()
    for a in artifacts:
        if not _is_digest(a.sha256):
            raise ValueError(f"artifact {a.artifact_id} has no valid sha256 digest")
        if a.sha256 in seen_digests:
            raise ValueError(f"duplicate artifact digest in release: {a.sha256[:16]}...")
        seen_digests.add(a.sha256)

    created = created_at or utcnow_iso()
    stamp = created.replace("-", "").replace(":", "").replace(".", "")[:14]
    release_id = f"PR-{stamp}-{uuid.uuid4().hex[:6]}"
    manifest = ReleaseManifest(
        release_id=release_id,
        created_at=created,
        artifacts=[a.to_dict() for a in artifacts],
        metadata=copy.deepcopy(metadata) if metadata is not None else {},
    )
    body = {
        "release_id": manifest.release_id,
        "created_at": manifest.created_at,
        "artifacts": manifest.artifacts,
        "metadata": manifest.metadata,
    }
    manifest.manifest_digest = _manifest_digest(body)
    ok, reason = verify_manifest(manifest)
    if not ok:
        raise ValueError(reason)
    return manifest


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")


def verify_manifest(manifest: ReleaseManifest | dict) -> tuple[bool, str]:
    """Validate the manifest structure and its digest; malformed input denies.

    The digest provides integrity, not reviewer authentication. A valid
    manifest still requires independently signed approval for each artifact.
    """
    try:
        d = manifest.to_dict() if isinstance(manifest, ReleaseManifest) else manifest
        if not isinstance(d, dict) or set(d) != {
                "release_id", "created_at", "artifacts", "metadata", "manifest_digest"}:
            raise ValueError("manifest fields invalid")
        _require_text(d["release_id"], "release_id")
        _require_text(d["created_at"], "created_at")
        created = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if not isinstance(d["metadata"], dict):
            raise ValueError("metadata must be an object")
        if not isinstance(d["artifacts"], list) or not d["artifacts"]:
            raise ValueError("manifest must contain at least one artifact")
        ids, digests = set(), set()
        for artifact in d["artifacts"]:
            if not isinstance(artifact, dict):
                raise ValueError("artifact must be an object")
            for key in ("artifact_id", "name", "media_type"):
                _require_text(artifact.get(key), key)
            if not _is_digest(artifact.get("sha256")):
                raise ValueError("artifact sha256 must be lowercase hexadecimal")
            size = artifact.get("size_bytes")
            if type(size) is not int or size < 0:
                raise ValueError("artifact size_bytes must be a nonnegative integer")
            if artifact["artifact_id"] in ids or artifact["sha256"] in digests:
                raise ValueError("duplicate artifact identity or digest")
            ids.add(artifact["artifact_id"])
            digests.add(artifact["sha256"])
        if not _is_digest(d["manifest_digest"]):
            raise ValueError("manifest_digest must be lowercase hexadecimal")
        body = {key: d[key] for key in ("release_id", "created_at", "artifacts", "metadata")}
        if _manifest_digest(body) != d["manifest_digest"]:
            return False, "manifest digest mismatch: manifest was altered after creation"
        return True, "manifest intact"
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return False, "manifest structure or digest invalid"


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field in manifest")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("nonfinite value in manifest")


def save_manifest(manifest: ReleaseManifest, path: str) -> None:
    ok, reason = verify_manifest(manifest)
    if not ok:
        raise ValueError(reason)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, indent=2, sort_keys=True)
        f.write("\n")


def load_manifest(path: str) -> ReleaseManifest:
    with open(path, encoding="utf-8") as f:
        d = json.load(f, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    ok, reason = verify_manifest(d)
    if not ok:
        raise ValueError(reason)
    return ReleaseManifest(**d)
