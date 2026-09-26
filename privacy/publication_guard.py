"""Publication guard: the egress enforcement point.

Every artifact attempting to leave the private workspace passes through
check_egress(). The rule is structural, not advisory:

    default = DENY.
    allow ONLY when counsel_decision.verify_decision(release_id,
    artifact_digest) is True — i.e. a real, separately-authorized counsel
    unconditional approval binds this exact artifact digest and is effective,
    unexpired and unrevoked.

This module performs no network I/O itself; it is the gate other code
(publish pipelines, export flows, distribution builders) must call BEFORE any
egress. An egress path that does not call this guard is a bypass and must be
treated as a defect (see the controller's bypass audit).
Scope boundary (F8, 2026-09-20): this guard is for KEEL RELEASE ARTIFACTS —
release means a counsel-authorized artifact with an exact digest binding
(verified through counsel_decision), enforced here. Instagram publishing
does NOT use check_egress: it has its own digest-bound publish gate
(IGGraphClient.approval_digest plus actual media-byte hashing in
keel/marketing-engine/integrations/instagram-graph/client.py), where the
approval is Trent's per-post sign-off bound to the exact media bytes — the
counsel release process does not apply to the Instagram content path.
OUT OF SCOPE (not release artifacts, and routing them here would be a
category error, not a hardening): export_flow_snapshot.py (read-only
operator-visible state snapshots), the federated telemetry exporter
(aggregate telemetry, no release artifact), and GET-only ATS discovery
reads (no artifact leaves the workspace).
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timezone

from keel.privacy import counsel_decision as _cd
from keel.privacy.release_manifest import ReleaseManifest, verify_manifest


class EgressDenied(Exception):
    """Raised by assert_egress_allowed() when egress is not authorized."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def check_egress(release_id: str, artifact_digest: str, *,
                 destination: str = "",
                 state_dir: str | os.PathLike | None = None) -> dict:
    """Egress verdict for one artifact. Allowed only with a verified decision.

    Returns a verdict dict (never raises on deny — deny is data, callers log
    it). Keys: allowed, reason, release_id, artifact_digest, destination,
    decision (effective record summary or None), checked_at. The destination
    is descriptive context: it is NOT covered by the existing reviewer
    signature. This function verifies approval, not the eventual sender.
    """
    rec = _cd.verified_decision_record(release_id, artifact_digest, state_dir=state_dir)
    # Natural-language conditions are not executable policy. A signed
    # conditional decision cannot be silently upgraded to unrestricted egress.
    allowed = bool(rec is not None and rec.get("decision") == "approved"
                   and rec.get("conditions") == [])
    verdict = {
        "allowed": allowed,
        "release_id": release_id,
        "artifact_digest": artifact_digest,
        "destination": destination,
        "checked_at": _utcnow_iso(),
        "decision": None,
        "reason": "",
    }
    if rec is not None:
        verdict["decision"] = {
            "decision": rec.get("decision"),
            "actor": rec.get("actor"),
            "authority_reference": rec.get("authority_reference"),
            "decided_at": rec.get("decided_at"),
            "expires_at": rec.get("expires_at"),
            "conditions": rec.get("conditions", []),
        }
    if allowed:
        verdict["reason"] = "verified unconditional counsel approval binds this exact artifact digest"
    elif rec is not None:
        verdict["reason"] = "DENY: conditional approval has no supported condition evaluator"
    else:
        verdict["reason"] = ("DENY: no valid, effective, unexpired, unrevoked counsel "
                             "approval binds this (release_id, artifact_digest)")

    return verdict


def check_release(manifest: ReleaseManifest | dict, *,
                  destination: str = "",
                  state_dir: str | os.PathLike | None = None) -> dict:
    """Verdict for every artifact in a release manifest.

    allowed=True only when EVERY artifact verifies. One denied artifact
    denies the release (no partial egress).
    """
    # Validate and consume a detached snapshot, so a caller cannot mutate the
    # artifact set between validation and the approval lookups.
    try:
        d = manifest.to_dict() if isinstance(manifest, ReleaseManifest) else copy.deepcopy(manifest)
        intact, reason = verify_manifest(d)
    except Exception:
        d, intact, reason = None, False, "manifest validation failed"
    if not intact:
        return {
            "allowed": False,
            "release_id": d.get("release_id") if isinstance(d, dict) else None,
            "destination": destination, "checked_at": _utcnow_iso(),
            "artifacts_checked": 0, "artifacts_denied": 0,
            "denied_artifact_ids": [], "per_artifact": [],
            "reason": f"DENY: {reason}", "manifest_valid": False,
        }
    release_id = d["release_id"]
    per_artifact = [
        check_egress(release_id, a["sha256"], destination=destination,
                     state_dir=state_dir)
        for a in d["artifacts"]
    ]
    denied = [v for v in per_artifact if not v["allowed"]]
    return {
        "allowed": not denied,
        "release_id": release_id,
        "destination": destination,
        "checked_at": _utcnow_iso(),
        "artifacts_checked": len(per_artifact),
        "artifacts_denied": len(denied),
        "denied_artifact_ids": [d["artifacts"][i]["artifact_id"]
                                for i, v in enumerate(per_artifact)
                                if not v["allowed"]],
        "per_artifact": per_artifact,
        "reason": ("all artifacts verified" if not denied
                   else f"DENY: {len(denied)}/{len(per_artifact)} artifacts lack "
                        f"verified counsel approval"),
    }


def assert_egress_allowed(release_id: str, artifact_digest: str, *,
                          destination: str = "",
                          state_dir: str | os.PathLike | None = None) -> dict:
    """check_egress(), but raise EgressDenied instead of returning a deny."""
    verdict = check_egress(release_id, artifact_digest, destination=destination,
                           state_dir=state_dir)
    if not verdict["allowed"]:
        raise EgressDenied(
            f"egress DENIED for release {release_id} artifact "
            f"{artifact_digest[:16]}...: {verdict['reason']}")
    return verdict
