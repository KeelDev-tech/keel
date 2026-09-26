#!/usr/bin/env python3
"""gate_release.py — egress gate for release artifacts (bypass-3 fix).

Default DENY: a release artifact (dist/*.zip) may leave the private
workspace ONLY when check_egress() verifies a valid, unexpired, unrevoked
counsel decision binding the exact (release_id, artifact_digest).

Usage:
    python3 -m keel.privacy.gate_release --release-id keel-0.7.0 \
        --artifact dist/keel-0.7.0.zip [--state-dir ...]

Exit 0: egress ALLOWED (verified counsel approval binds this digest).
Exit 1: egress DENIED (no approval, expired, revoked, tampered ledger,
        or digest mismatch) — the artifact must stay private until
        privacy-counsel sign-off (G1) is recorded via counsel_decision.

Called by package.sh --release after the integrity verification step.
This performs a point-in-time local check. No sender is implemented here;
a later uploader must bind authorization to the actual bytes and destination
at its own enforced boundary. This CLI cannot prove all egress paths call it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy.publication_guard import (  # noqa: E402
    EgressDenied, assert_egress_allowed)


class ArtifactChanged(ValueError):
    """The local artifact could not be hashed as one stable regular file."""


def sha256_file(path: str) -> str:
    """Hash a stable regular file; refuse links, devices and observed changes.

    The result describes these bytes at check time. It does not bind a later
    uploader to this file descriptor or prevent a later path replacement.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactChanged("artifact must be a regular file")
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                h.update(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        expected = tuple(getattr(before, key) for key in fields)
        if (tuple(getattr(after, key) for key in fields) != expected or
                tuple(getattr(named, key) for key in fields) != expected or
                not stat.S_ISREG(named.st_mode)):
            raise ArtifactChanged("artifact changed while hashing")
        return h.hexdigest()
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Egress gate for release artifacts.")
    ap.add_argument("--release-id", required=True,
                    help="Release id, e.g. keel-0.7.0")
    ap.add_argument("--artifact", required=True,
                    help="Path to the release artifact file")
    ap.add_argument("--state-dir", default=None,
                    help="Counsel decision state dir (default: privacy/state)")
    ap.add_argument("--destination", default="",
                    help="Where the artifact would be published")
    args = ap.parse_args(argv)

    try:
        digest = sha256_file(args.artifact)
    except (OSError, ValueError):
        print(json.dumps({"allowed": False,
                          "error": "artifact unavailable, unsafe, or changed during hashing"}))
        return 1
    try:
        verdict = assert_egress_allowed(
            args.release_id, digest, destination=args.destination,
            state_dir=args.state_dir)
    except EgressDenied as e:
        print(json.dumps({
            "allowed": False,
            "release_id": args.release_id,
            "artifact": args.artifact,
            "artifact_digest": digest,
            "reason": str(e),
            "next": ("obtain privacy-counsel sign-off (G1) binding this "
                     "exact digest, then re-run"),
        }, indent=2))
        return 1
    print(json.dumps({
        "allowed": True,
        "release_id": args.release_id,
        "artifact": args.artifact,
        "artifact_digest": digest,
        "decision": verdict.get("decision"),
        "scope": "point-in-time local artifact check; sender binding is separate",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
