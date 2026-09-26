#!/usr/bin/env python3
"""Live seven-source mapping adapter (Keel 0.7.0).

Maps the REAL canonical flow export (export_flow_snapshot.py output) into the
``keel.revision_sources.v1`` normalized contract via
``keel_agent.revisions.export_revisions``.

What is mapped (field-level honesty):
  - scope: role_id (export lead row) + application_id bound per the builder's
    own convention (keel_agent/scope.py:154 requires
    action["application_id"] == lead["identity"]; tools/make_assurance_demo.py
    binds the same way). The export carries no separate application_id column;
    the identity digest IS the lane's canonical verified-posting application
    identifier -- using it is following the builder's binding, not inventing one.
  - policy/form/answers/attachments/target/route: the export emits NO source
    columns for these families (only booleans/strings like policy_pass,
    answers_resolved, packet_present, approval_valid, route="browser"). A
    missing exporter column is not evidence the candidate must act, so each
    maps to the contract's SYSTEM_NOT_EXPORTED named absence (owned by
    export_adapter, responsibility system). No record is inferred from
    lookalikes: booleans, URLs, digests and free text are never coerced into
    revisions.
  - approval: ONLY from an existing authentic recorded decision. The live lane
    records none (verified: zero approval_id / authority_record_ref /
    decision_request_ref values in live queues, input tray, and engines), and
    no decision-request reference exists. -> SOURCE_RECORD_MISSING
    (genuinely missing source record, system-owned). HUMAN_DECISION_REQUIRED
    is emitted only when a real decision-request reference exists; none does.

Fail-closed rules (enforced by construction):
  - envelope_inputs_ready means structural validity ONLY. The adapter never
    clears a canonical hold, grants execution authority, or feeds submission
    evidence. Holds from the export are carried through the observation report
    untouched; execution_authorized and source_authenticity_verified stay False.
  - Shared records are read ONCE and reused across roles with per-role scope
    binding; the adapter never mints per-lead versions of one shared record.
    (Live: no shared policy/form record exists yet, so the shared loader
    returns None and every role gets the named absence.)
  - Read-only: the snapshot is opened read-only and hashed before/after; all
    derived output goes to an explicit fresh directory. No network calls, no
    model calls, no canonical-state writes.

Usage:
    python3 seven_source_adapter.py <snapshot.json> <out-dir>
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(KEEL_DIR))

from keel_agent.revisions import COMPONENTS, SCHEMA, export_revisions, _time, _token
from keel_flow.common import canonical, strict_json
from packet_dependency_adapter import read_snapshot

ADAPTER_VERSION = "seven_source_adapter/1.0.0"
# Workspace identity for produced exports. The public repo ships with a
# synthetic default; the private host injects the real workspace id via
# KEEL_WORKSPACE_ID at runtime. Never hardcode an applicant workspace here.
WORKSPACE_ID = os.environ.get("KEEL_WORKSPACE_ID", "demo-workspace")
SNAPSHOT_SOURCE_REF = "export:canonical-snapshot"
# The flow-board observer regenerates the canonical export on its 10-minute
# cadence (keel-octopus-pulse step 1a); a snapshot is superseded by the next
# scheduled export. Validity is a configured policy, not the current clock.
SNAPSHOT_VALIDITY_S = 600
ACTION = "submit"

# Families whose exporter column is absent from the canonical export.
EXPORT_COLUMN_ABSENT = ("policy", "form", "answers", "attachments", "target", "route")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def absence(kind):
    """Contract-exact absence descriptor; never a fabricated record."""
    if kind not in {"SYSTEM_NOT_EXPORTED", "SOURCE_RECORD_MISSING",
                    "HUMAN_DECISION_REQUIRED"}:
        raise ValueError("unknown absence kind")
    return {"absence": {"kind": kind}}


class SharedSourceLoader:
    """Read-once cache for shared records (policy/form).

    The live lane has no shared policy/form source record yet, so every load
    returns None. When the lane gains one, register its read path here: it
    will be read exactly once per run and bound per role scope, never minted
    per lead.
    """

    def __init__(self):
        self._cache = {}
        self.reads = {name: 0 for name in ("policy", "form")}

    def _read_source(self, name):
        # Live lane: no shared source record exists (verified 2026-09-18: no
        # versioned policy/form record with capture metadata in the canonical
        # store). Extension point for the lane's future shared records.
        return None

    def load(self, name):
        if name not in self._cache:
            self.reads[name] += 1
            self._cache[name] = self._read_source(name)
        return self._cache[name]


def load_approval_store():
    """Authentic recorded human approval decisions, keyed by scope.

    The live lane records no human approval decisions (verified 2026-09-18:
    zero approval_id / authority_record_ref / decision_request_ref in live
    queues, input tray, and engines). Returns {} until an authentic approval
    workflow lands. The adapter NEVER invents an approval record: a decision
    not found in this store is not mapped.
    """
    return {}


def map_approval(scope, approval_store):
    """Map approval ONLY from an authentic recorded decision.

    Returns a source descriptor when the store holds a real decision for this
    exact scope, else the SOURCE_RECORD_MISSING named absence. A record that
    is not in the authentic store is refused -- never mapped as a convenience.
    """
    key = (scope["role_id"], scope["application_id"], scope["action"])
    record = approval_store.get(key)
    if record is None:
        return absence("SOURCE_RECORD_MISSING")
    # The store is the authority; anything else is fabrication. (Validity,
    # binding, expiry, and revocation are enforced by export_revisions.)
    return record


def build_normalized_snapshot(export, *, now, workspace_id=WORKSPACE_ID,
                              snapshot_validity_s=SNAPSHOT_VALIDITY_S,
                              approval_store=None, shared_loader=None,
                              source_overrides=None):
    """Pure function: canonical export -> keel.revision_sources.v1 snapshot.

    Raises ValueError on duplicate scopes or malformed identifiers. Unscoped
    leads are explicitly counted as skipped; identifiers are never invented.

    ``source_overrides`` maps (role_id, component) -> source descriptor and
    exists for tests: the live lane has no real source records, so the live
    path always passes None and every family maps to its named absence.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    export = strict_json(canonical(export))
    if type(export) is not dict or type(export.get("leads")) is not list:
        raise ValueError("export requires an explicit lead array")
    if type(snapshot_validity_s) is not int or not 1 <= snapshot_validity_s <= 86400:
        raise ValueError("snapshot validity must be 1 to 86400 seconds")
    _token(workspace_id)
    approval_store = approval_store if approval_store is not None else load_approval_store()
    shared_loader = shared_loader if shared_loader is not None else SharedSourceLoader()
    source_overrides = source_overrides or {}

    observed_at = export.get("observed_at")
    observed_dt = _time(observed_at)
    source_revision = _token(export.get("source_revision"))
    snapshot = {
        "source_ref": SNAPSHOT_SOURCE_REF,
        "source_version": source_revision,
        "observed_at": observed_at,
        "expires_at": (observed_dt + timedelta(seconds=snapshot_validity_s)).isoformat(),
    }

    roles, seen, skipped = [], set(), []
    for lead in export["leads"]:
        if type(lead) is not dict:
            raise ValueError("each lead must be an object")
        role_id = lead.get("role_id")
        application_id = lead.get("identity")  # builder convention: scope.py:154
        if not role_id or not application_id:
            skipped.append(role_id or "<unscoped>")
            continue
        _token(role_id)
        _token(application_id)
        scope_key = (role_id, application_id, ACTION)
        if scope_key in seen:
            raise ValueError(f"duplicate scope for role {role_id}")
        seen.add(scope_key)
        sources = {}
        for component in EXPORT_COLUMN_ABSENT:
            override = source_overrides.get((role_id, component))
            if override is not None:
                sources[component] = override
                continue
            shared = shared_loader.load(component) if component in ("policy", "form") else None
            sources[component] = shared if shared is not None else absence("SYSTEM_NOT_EXPORTED")
        scope = {"workspace_id": workspace_id, "role_id": role_id,
                 "application_id": application_id, "action": ACTION}
        sources["approval"] = map_approval(scope, approval_store)
        roles.append({"role_id": role_id, "application_id": application_id,
                      "action": ACTION, "sources": sources})
    return {"schema": SCHEMA, "workspace_id": workspace_id, "snapshot": snapshot,
            "roles": roles, "_skipped_unscoped": skipped}


def summarize(revision_export, *, export_path, export_sha256, export_observed_at,
              snapshot, shared_reads):
    """Counts per family: real revisions vs named blockers; holds kept visible."""
    per_family_ready = {name: 0 for name in COMPONENTS}
    holds_by_family = {}
    for row in revision_export["roles"]:
        for name, component in row["components"].items():
            if component["status"] == "READY":
                per_family_ready[name] += 1
    for lead in snapshot.get("leads", []):
        for family in lead.get("holds", []) or []:
            holds_by_family[family] = holds_by_family.get(family, 0) + 1
    return {
        "schema_version": 1,
        "adapter": ADAPTER_VERSION,
        "export_path": str(export_path),
        "export_sha256": export_sha256,
        "export_observed_at": export_observed_at,
        "export_source_revision": snapshot.get("source_revision"),
        "leads_observed": revision_export["role_count"],
        "leads_skipped_unscoped": len(snapshot.get("leads", [])) - revision_export["role_count"],
        "real_revisions_per_family": per_family_ready,
        # Preserve source, distinct-role counts, snapshot failures and actual
        # human/system ownership from the authoritative reducer output.
        "named_blockers_per_category": [dict(row) for row in revision_export["root_causes"]],
        "ready_role_count": revision_export["ready_role_count"],
        "blocked_role_count": revision_export["blocked_role_count"],
        "root_causes": revision_export["root_causes"],
        "system_root_cause_count": revision_export["system_root_cause_count"],
        "human_root_cause_count": revision_export["human_root_cause_count"],
        # envelope_inputs_ready is STRUCTURAL ONLY: never readiness, permission,
        # or authority. Holds below are the export's original holds, untouched.
        "envelope_inputs_ready_is_structural_only": True,
        "execution_authorized": revision_export["execution_authorized"],
        "source_authenticity_verified": revision_export["source_authenticity_verified"],
        "original_holds_by_family": holds_by_family,
        "original_holds_cleared": 0,
        "shared_source_reads": shared_reads,
        "genuinely_absent_verified": [],
        "absence_boundary": "Named absences describe inputs unavailable through this adapter; "
                            "they do not establish absence from a live host or authenticate a decision.",
        "effects": {
            "canonical_writes": 0,
            "network_calls": 0,
            "model_calls": 0,
            "browser_actions": 0,
            "messages_sent": 0,
            "note": "pure local computation over a read-only snapshot; input "
                    "bytes verified unchanged before/after",
        },
    }


def main(argv):
    if len(argv) != 3:
        print("usage: seven_source_adapter.py <snapshot.json> <out-dir>", file=sys.stderr)
        return 2
    snapshot_path = Path(argv[1])
    out_dir = Path(argv[2])

    snapshot, sha_before = read_snapshot(snapshot_path)

    now = datetime.now(timezone.utc)
    shared_loader = SharedSourceLoader()
    normalized = build_normalized_snapshot(snapshot, now=now, shared_loader=shared_loader)

    attachment_root = out_dir / "attachments"
    revision_export = export_revisions(normalized, attachment_root=attachment_root, now=now)

    _, sha_after = read_snapshot(snapshot_path)
    if sha_before != sha_after:
        print("seven_source_adapter: input snapshot changed during observation",
              file=sys.stderr)
        return 3

    out_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    attachment_root.mkdir(mode=0o700, exist_ok=True)

    def write(name, value):
        path = out_dir / name
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")

    public_normalized = {k: v for k, v in normalized.items() if not k.startswith("_")}
    write("normalized-sources.json", public_normalized)
    write("revision-export.json", revision_export)
    report = summarize(revision_export, export_path=snapshot_path, export_sha256=sha_after,
                       export_observed_at=snapshot.get("observed_at"), snapshot=snapshot,
                       shared_reads=dict(shared_loader.reads))
    write("seven-source-report.json", report)

    print(json.dumps({
        "adapter": ADAPTER_VERSION,
        "export_sha256": sha_after,
        "leads_observed": report["leads_observed"],
        "real_revisions_per_family": report["real_revisions_per_family"],
        "named_blockers_per_category": report["named_blockers_per_category"],
        "ready_role_count": report["ready_role_count"],
        "blocked_role_count": report["blocked_role_count"],
        "execution_authorized": report["execution_authorized"],
        "input_bytes_unchanged": sha_before == sha_after,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
