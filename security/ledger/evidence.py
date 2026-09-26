"""Security event helpers: hashing artifacts and bundling evidence.

Evidence bundles bind an evaluation to the exact inputs it saw:
input artifact hashes, the policy version, the decision, and the result
hash. Anyone can re-verify a bundle against the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os


def hash_text(text: str) -> str:
    return hashlib.sha256(
        text.encode("utf-8", errors="replace")).hexdigest()


def hash_dict(d: dict) -> str:
    canonical = json.dumps(d, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle_evidence(*, agent_id: str, action_name: str,
                    input_hashes: dict, policy_version: str,
                    decision: str, reasons: list,
                    result_hash: str = "") -> dict:
    """Build a self-describing evidence bundle for one decision."""
    bundle = {"agent_id": agent_id, "action_name": action_name,
              "input_hashes": dict(input_hashes),
              "policy_version": policy_version, "decision": decision,
              "reasons": list(reasons), "result_hash": result_hash}
    bundle["bundle_hash"] = hash_dict(
        {k: v for k, v in bundle.items() if k != "bundle_hash"})
    return bundle


def verify_bundle(bundle: dict) -> bool:
    """Recompute the bundle hash. True iff the bundle is intact."""
    want = bundle.get("bundle_hash")
    if not want:
        return False
    recomputed = hash_dict(
        {k: v for k, v in bundle.items() if k != "bundle_hash"})
    return recomputed == want


def artifact_exists_and_hashes(path: str) -> tuple[bool, str]:
    """(exists, sha256-or-""). Never raises on missing files."""
    if not path or not os.path.exists(path):
        return False, ""
    try:
        return True, hash_file(path)
    except OSError:
        return False, ""
