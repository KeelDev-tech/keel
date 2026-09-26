#!/usr/bin/env python3
"""Write an explicitly synthetic seven-revision demo, without network/model calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keel_agent.revisions import SCHEMA, export_revisions


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def descriptor(name, record=None):
    value = {"source_ref": "synthetic:" + name, "source_version": "fixture-v1",
             "observed_at": "2026-09-18T11:59:50+00:00",
             "expires_at": "2026-09-18T12:10:00+00:00"}
    if record is not None:
        value["record"] = record
    return value


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    # Refuse to replace prior evidence files with a new demonstration.
    if any(args.out.iterdir()):
        parser.error("demo output directory must be empty")
    missing = {"schema": SCHEMA, "workspace_id": "synthetic-workspace",
               "snapshot": descriptor("snapshot"), "roles": [
                   {"role_id": f"role-{i}", "application_id": f"application-{i}",
                    "action": "submit", "sources": {}} for i in range(837)]}
    missing_report = export_revisions(missing, attachment_root=args.out, now=NOW)
    write(args.out / "missing_sources_837.json", missing)
    write(args.out / "missing_report_837.json", missing_report)
    (args.out / "synthetic_resume.txt").write_bytes(b"SYNTHETIC TEST CANDIDATE\n")
    role = {"role_id": "role-complete", "application_id": "application-complete",
            "action": "submit", "sources": {}}
    records = {
        "policy": {"policy_id": "fixture-policy", "rules": {"requires_approval": True}},
        "form": {"form_id": "fixture-form", "fields": [{"field_id": "name", "required": True}]},
        "answers": {"fields": {"name": "Synthetic Candidate"}},
        "attachments": {"files": [{"path": "synthetic_resume.txt", "purpose": "resume"}]},
        "target": {"canonical_posting_url": "https://jobs.example.invalid/fixture",
                   "role_id": role["role_id"], "application_id": role["application_id"]},
        "route": {"account_id": "synthetic-account", "action": "submit", "transport": "simulator",
                  "destination": "https://apply.example.invalid/fixture"},
    }
    role["sources"] = {name: descriptor(name, record) for name, record in records.items()}
    complete = {"schema": SCHEMA, "workspace_id": "synthetic-workspace",
                "snapshot": descriptor("snapshot-complete"), "roles": [role]}
    initial = export_revisions(complete, attachment_root=args.out, now=NOW)["roles"][0]
    role["sources"]["approval"] = descriptor("fixture-approval", {
        "approval_id": "synthetic-approval", "actor_id": "synthetic-human",
        "authority_record_ref": "synthetic:authority", "decision": "APPROVE",
        "scope": initial["scope"], "component_revisions": initial["revisions"],
        "approved_at": "2026-09-18T11:59:40+00:00",
        "expires_at": "2026-09-18T12:05:00+00:00", "revoked": False, "synthetic": True})
    complete_report = export_revisions(complete, attachment_root=args.out, now=NOW)
    write(args.out / "complete_sources.json", complete)
    write(args.out / "complete_report.json", complete_report)
    summary = {"mode": "synthetic", "live_data_used": False,
               "missing_roles": missing_report["role_count"],
               "missing_ready_roles": missing_report["ready_role_count"],
               "missing_diagnostic_categories": len(missing_report["root_causes"]),
               "missing_human_requests": missing_report["human_root_cause_count"],
               "complete_fixture_inputs_ready": complete_report["ready_role_count"] == 1,
               "execution_authorized": False, "network_calls": 0, "model_calls": 0,
               "canonical_writes": 0}
    write(args.out / "revision_demo_summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if (summary["missing_diagnostic_categories"] == 7
                 and summary["missing_ready_roles"] == 0
                 and summary["complete_fixture_inputs_ready"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
