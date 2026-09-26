"""Observation-only packet dependency adapter (Keel 0.6.0 live integration).

Reads a canonical flow export snapshot and, per lead, attempts to derive the
seven evidence revisions a truthful assurance envelope requires:

    policy, form, answers, attachments, target, approval, route

When all seven are present as nonblank strings, the adapter derives a
deterministic packet_dependency_hash:

    sha256(canonical_json([policy, form, answers, attachments,
                           target, approval, route]))

When any revision is missing or blank, the adapter emits a NAMED absence
blocker (``missing_<name>_revision``) and no hash. It never invents a
revision from another field. These are caller-declared revision labels;
their presence or hash does not authenticate their origins or freshness.

Read-only contract:
  - the input snapshot is opened read-only and never modified;
  - the derived observation is written only to an explicit output path
    (default: a fresh directory under /tmp);
  - no network calls, no model calls, no canonical-state writes.

A ``packet_dependency_hash=None`` lead cannot carry a truthful assurance
envelope; downstream assurance/trust/workflow gates must fail closed on it.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

from keel_flow.common import canonical, strict_json
from tools.bench_inventory import read_file

REVISIONS = ("policy", "form", "answers", "attachments", "target", "approval", "route")

SCHEMA_VERSION = 1


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def read_snapshot(path):
    """Decode the same bounded, stable bytes whose digest is reported.

    Duplicate keys, nonfinite numbers, symlinks, special files and oversized
    documents are refused before interpretation. No input is modified.
    """
    path = Path(path).absolute()
    raw = read_file(path.parent, path.name)
    return strict_json(raw), hashlib.sha256(raw).hexdigest()


def _revision(value):
    return (type(value) is str and 0 < len(value) <= 4096 and value.strip() == value
            and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def derive_packet_dependency_hash(revisions):
    """Deterministic hash over the seven revisions in fixed order."""
    if (type(revisions) is not dict or set(revisions) != set(REVISIONS)
            or not all(_revision(revisions[name]) for name in REVISIONS)):
        raise ValueError("seven explicit bounded revision labels required")
    ordered = [revisions[name] for name in REVISIONS]
    return hashlib.sha256(_canonical(ordered).encode("utf-8")).hexdigest()


def derive_lead(lead):
    """Derive the packet dependency observation for one snapshot lead.

    Returns a dict with role_id, the evidenced revisions (only nonblank
    strings that were literally present), packet_dependency_hash (hex or
    None), and blockers (named absences).
    """
    if type(lead) is not dict or not _revision(lead.get("role_id")):
        raise ValueError("lead requires an explicit role identifier")
    role_id = lead["role_id"]
    evidenced = {}
    blockers = []
    for name in REVISIONS:
        raw = lead.get(f"{name}_revision")
        if _revision(raw):
            evidenced[name] = raw
        else:
            blockers.append(f"missing_{name}_revision")
    packet_dependency_hash = (
        derive_packet_dependency_hash(evidenced) if not blockers else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "role_id": role_id,
        "revisions": evidenced,
        "packet_dependency_hash": packet_dependency_hash,
        "blockers": blockers,
        "source_authenticity_verified": False,
        "execution_authorized": False,
    }


def observe_snapshot(snapshot):
    """Derive per-lead observations for a whole snapshot (pure function)."""
    snapshot = strict_json(canonical(snapshot))
    if type(snapshot) is not dict or type(snapshot.get("leads")) is not list:
        raise ValueError("snapshot requires an explicit lead array")
    leads = snapshot["leads"]
    observations = [derive_lead(lead) for lead in leads]
    if len({row["role_id"] for row in observations}) != len(observations):
        raise ValueError("duplicate role identifier")
    hashed = sum(1 for o in observations if o["packet_dependency_hash"])
    blocked = len(observations) - hashed
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": "packet_dependency_adapter/1.0.0",
        "snapshot_observed_at": snapshot.get("observed_at"),
        "snapshot_source_revision": snapshot.get("source_revision"),
        "leads_observed": len(observations),
        "leads_with_hash": hashed,
        "leads_blocked": blocked,
        "observations": observations,
        "source_authenticity_verified": False,
        "execution_authorized": False,
    }


def main(argv):
    if len(argv) != 3:
        print("usage: packet_dependency_adapter.py <snapshot.json> <out-dir>", file=sys.stderr)
        return 2
    snapshot_path = Path(argv[1])
    out_dir = Path(argv[2])
    # Read-only: never create or modify the input.
    snapshot, input_sha256 = read_snapshot(snapshot_path)
    observation = observe_snapshot(snapshot)
    observation["snapshot_sha256"] = input_sha256
    # Derived output only, to an explicit fresh directory.
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    out_path = out_dir / "packet-dependency-observation.json"
    with open(os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(observation, f, indent=2, sort_keys=True, allow_nan=False)
        f.write("\n")
    summary = {k: v for k, v in observation.items() if k != "observations"}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
