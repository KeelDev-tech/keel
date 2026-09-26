"""Fail-closed battery for the live seven-source mapping adapter.

Proves, with synthetic fixtures only (never live data):

1. Missing records -> the contract's NAMED absences, never fabricated sources.
2. Revoked / expired shared records block (REVOKED / STALE).
3. Unbound, expired, rejected, or future-dated approvals stay blocked.
4. An approval binding its own digest is rejected (no circular hash).
5. A fabricated approval (not in the authentic store) is refused.
6. envelope_inputs_ready never implies readiness, permission, or authority.
7. One shared record is read once and reused across roles -- never minted
   per lead.

The live lane currently has zero real source records for all seven families;
these tests pin the adapter's fail-closed behavior around that fact.
"""
import copy
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/home/hatch/workspace/keel")

import seven_source_adapter as ssa
from keel_agent.revisions import COMPONENTS, SCHEMA, export_revisions

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def ts(delta_seconds=0):
    return (NOW + timedelta(seconds=delta_seconds)).isoformat()


def export_shape(n=1):
    """Minimal real-export-shaped snapshot: leads carry NO source columns."""
    return {
        "source_revision": "export_flow_snapshot/1.1.0:test",
        "observed_at": ts(-30),
        "leads": [
            {"role_id": f"ROLE-{i}", "identity": f"identity-digest-{i}",
             "posting_url": "https://example.invalid/jobs/1",
             "policy_pass": True, "answers_resolved": True,
             "packet_present": False, "approval_valid": False,
             "route": "browser", "holds": ["operator_input"]}
            for i in range(n)
        ],
    }


def run(export, attachment_root=Path("/tmp"), **kwargs):
    normalized = ssa.build_normalized_snapshot(export, now=NOW, **kwargs)
    return export_revisions(normalized, attachment_root=attachment_root, now=NOW)


def descriptor(record, name="source", observed_delta=-20, validity_s=600, revoked=False):
    value = {"source_ref": f"test:{name}", "source_version": "v1",
             "observed_at": ts(observed_delta),
             "expires_at": ts(observed_delta + validity_s)}
    if revoked:
        value["revoked"] = True
    if record is not None:
        value["record"] = record
    return value


def complete_role_sources(tmp_path):
    (tmp_path / "resume.txt").write_bytes(b"SYNTHETIC RESUME\n")
    return {
        "policy": descriptor({"policy_id": "p1", "rules": {"r": True}}, "policy"),
        "form": descriptor({"form_id": "f1", "fields": [{"field_id": "name"}]}, "form"),
        "answers": descriptor({"fields": {"name": "Synthetic"}}, "answers"),
        "attachments": descriptor({"files": [{"path": "resume.txt", "purpose": "resume"}]},
                                  "attachments"),
        "target": descriptor({"canonical_posting_url": "https://example.invalid/jobs/1",
                              "role_id": "ROLE-0", "application_id": "identity-digest-0"},
                             "target"),
        "route": descriptor({"account_id": "a1", "action": "submit", "transport": "browser",
                             "destination": "https://example.invalid/apply"}, "route"),
    }


def approval_record(scope, revisions, decision="APPROVE", approved_delta=-15,
                    validity_s=500, revoked=False):
    return {
        "approval_id": "appr-1", "actor_id": "human-1",
        "authority_record_ref": "test:authority", "decision": decision,
        "scope": scope,
        "component_revisions": dict(revisions),
        "approved_at": ts(approved_delta),
        "expires_at": ts(approved_delta + validity_s),
        "revoked": revoked,
    }


# --- 1. missing records -> named absence, never fabricated -------------------

def test_missing_records_yield_named_absences_never_fabricated():
    report = run(export_shape())
    assert report["role_count"] == 1
    assert report["ready_role_count"] == 0
    assert report["blocked_role_count"] == 1
    role = report["roles"][0]
    assert role["envelope_inputs_ready"] is False
    assert role["revisions"] == {}
    for name in ("policy", "form", "answers", "attachments", "target", "route"):
        component = role["components"][name]
        assert component["status"] == "MISSING"
        assert component["reason"] == "source_not_exported"
        assert component["owner"] == "export_adapter"
        assert component["revision"] is None
    approval = role["components"]["approval"]
    assert approval["status"] == "MISSING"
    assert approval["reason"] == "source_record_missing"
    assert approval["owner"] == "export_adapter"
    # No human work is manufactured from a missing exporter column.
    assert report["human_root_cause_count"] == 0
    assert report["system_root_cause_count"] == 7
    assert report["execution_authorized"] is False
    assert report["source_authenticity_verified"] is False


def test_application_id_follows_builder_identity_binding():
    report = run(export_shape())
    scope = report["roles"][0]["scope"]
    assert scope["application_id"] == "identity-digest-0"  # keel_agent/scope.py:154
    assert scope["role_id"] == "ROLE-0"


def test_unscoped_leads_are_skipped_never_invented():
    export = export_shape()
    export["leads"].append({"role_id": "ROLE-NO-IDENTITY"})
    normalized = ssa.build_normalized_snapshot(export, now=NOW)
    assert len(normalized["roles"]) == 1
    assert normalized["_skipped_unscoped"] == ["ROLE-NO-IDENTITY"]


# --- 2/3. revoked and expired shared records block ---------------------------

class _FixtureLoader(ssa.SharedSourceLoader):
    """Test-only loader returning one synthetic shared record per family."""

    def __init__(self, fixtures):
        super().__init__()
        self._fixtures = fixtures

    def _read_source(self, name):
        return self._fixtures.get(name)


def test_revoked_shared_record_blocks_component():
    export = export_shape()
    loader = _FixtureLoader({"policy": descriptor(
        {"policy_id": "p1", "rules": {"r": True}}, "policy", revoked=True)})
    report = run(export, shared_loader=loader)
    assert loader.reads["policy"] == 1
    component = report["roles"][0]["components"]["policy"]
    assert component["status"] == "REVOKED"
    assert component["reason"] == "source_revoked"
    assert component["revision"] is None


def test_expired_shared_record_blocks_component():
    export = export_shape()
    loader = _FixtureLoader({"form": descriptor(
        {"form_id": "f1", "fields": [{"field_id": "name"}]}, "form",
        observed_delta=-900, validity_s=600)})  # expired 5 min ago
    report = run(export, shared_loader=loader)
    assert loader.reads["form"] == 1
    component = report["roles"][0]["components"]["form"]
    assert component["status"] == "STALE"
    assert component["reason"] == "source_expired"
    assert component["revision"] is None


# --- 4. approval binding failures stay blocked --------------------------------

def _approval_case(tmp_path, mutate):
    export = export_shape()
    sources = complete_role_sources(tmp_path)
    first = export_revisions(
        {"schema": SCHEMA, "workspace_id": "test-workspace",
         "snapshot": descriptor({}, "snapshot"),
         "roles": [{"role_id": "ROLE-0", "application_id": "identity-digest-0",
                    "action": "submit", "sources": sources}]},
        attachment_root=tmp_path, now=NOW)["roles"][0]
    record = approval_record(first["scope"], first["revisions"])
    mutate(record, first)
    # The store holds full source descriptors; the descriptor must have been
    # observed after the recorded decision (contract: no unobserved approvals).
    observed = descriptor(None, "approval", observed_delta=-5)
    observed["record"] = record
    store = {("ROLE-0", "identity-digest-0", "submit"): observed}
    overrides = {("ROLE-0", name): sources[name] for name in sources}
    return run(export, approval_store=store, source_overrides=overrides,
               workspace_id="test-workspace", attachment_root=tmp_path)


def test_approval_scope_mismatch_blocked(tmp_path):
    def mutate(record, first):
        record["scope"] = dict(first["scope"], role_id="ROLE-OTHER")
    component = _approval_case(tmp_path, mutate)["roles"][0]["components"]["approval"]
    assert component["status"] == "DEPENDENCY_MISMATCH"
    assert component["reason"] == "approval_scope_mismatch"


def test_approval_expired_blocked(tmp_path):
    def mutate(record, first):
        record["approved_at"] = ts(-900)
        record["expires_at"] = ts(-300)
    component = _approval_case(tmp_path, mutate)["roles"][0]["components"]["approval"]
    assert component["status"] == "STALE"
    assert component["reason"] == "approval_expired"


def test_approval_rejected_blocked(tmp_path):
    def mutate(record, first):
        record["decision"] = "REJECT"
    component = _approval_case(tmp_path, mutate)["roles"][0]["components"]["approval"]
    assert component["status"] == "REJECTED"
    assert component["reason"] == "approval_rejected"


def test_approval_future_dated_blocked(tmp_path):
    def mutate(record, first):
        record["approved_at"] = ts(3600)
        record["expires_at"] = ts(7200)
    component = _approval_case(tmp_path, mutate)["roles"][0]["components"]["approval"]
    assert component["status"] == "INVALID"
    assert component["reason"] == "approval_validity_invalid"


def test_approval_with_own_digest_in_component_revisions_rejected(tmp_path):
    def mutate(record, first):
        record["component_revisions"]["approval"] = "forged-own-digest"
    component = _approval_case(tmp_path, mutate)["roles"][0]["components"]["approval"]
    assert component["status"] == "DEPENDENCY_MISMATCH"
    assert component["reason"] == "approval_dependency_mismatch"


# --- 5. fabricated approval refused --------------------------------------------

def test_fabricated_approval_not_in_store_is_refused():
    export = export_shape()
    scope = {"workspace_id": "demo-workspace", "role_id": "ROLE-0",
             "application_id": "identity-digest-0", "action": "submit"}
    forged = descriptor({"approval_id": "forged", "actor_id": "forged-human",
                         "authority_record_ref": "test:forged", "decision": "APPROVE",
                         "scope": scope, "component_revisions": {},
                         "approved_at": ts(-10), "expires_at": ts(500),
                         "revoked": False}, "approval")
    # map_approval only honors records present in the authentic store.
    assert ssa.map_approval(scope, {}) == {"absence": {"kind": "SOURCE_RECORD_MISSING"}}
    assert ssa.map_approval(scope, {("ROLE-0", "identity-digest-0", "submit"): forged}) == forged
    # And the live path (empty store) never maps an approval.
    report = run(export)
    assert report["roles"][0]["components"]["approval"]["reason"] == "source_record_missing"


# --- 6. envelope_inputs_ready is structural only -------------------------------

def test_envelope_inputs_ready_never_implies_readiness_or_authority(tmp_path):
    export = export_shape()
    sources = complete_role_sources(tmp_path)
    first = export_revisions(
        {"schema": SCHEMA, "workspace_id": "test-workspace",
         "snapshot": descriptor({}, "snapshot"),
         "roles": [{"role_id": "ROLE-0", "application_id": "identity-digest-0",
                    "action": "submit", "sources": sources}]},
        attachment_root=tmp_path, now=NOW)["roles"][0]
    record = approval_record(first["scope"], first["revisions"])
    record_observed = descriptor(None, "approval", observed_delta=-5)
    record_observed["record"] = record
    store = {("ROLE-0", "identity-digest-0", "submit"): record_observed}
    overrides = {("ROLE-0", name): sources[name] for name in sources}
    report = run(export, approval_store=store, source_overrides=overrides,
                 workspace_id="test-workspace", attachment_root=tmp_path)
    role = report["roles"][0]
    assert role["envelope_inputs_ready"] is True
    # Structural completeness grants nothing.
    assert report["execution_authorized"] is False
    assert report["source_authenticity_verified"] is False
    assert role["source_authenticity_verified"] is False
    # The adapter's own summary attests the non-authority contract.
    summary = ssa.summarize(report, export_path="/tmp/x.json", export_sha256="0" * 64,
                            export_observed_at=export["observed_at"], snapshot=export,
                            shared_reads={"policy": 1, "form": 1})
    assert summary["envelope_inputs_ready_is_structural_only"] is True
    assert summary["execution_authorized"] is False
    assert summary["original_holds_cleared"] == 0
    assert summary["original_holds_by_family"] == {"operator_input": 1}
    assert summary["effects"]["canonical_writes"] == 0


# --- 7. shared records read once, never minted per lead -------------------------

def test_shared_record_read_once_reused_across_roles():
    export = export_shape(n=3)
    loader = _FixtureLoader({"policy": descriptor(
        {"policy_id": "shared-policy", "rules": {"r": True}}, "policy")})
    normalized = ssa.build_normalized_snapshot(export, now=NOW, shared_loader=loader)
    assert loader.reads["policy"] == 1  # one read, not three
    refs = [role["sources"]["policy"]["source_ref"] for role in normalized["roles"]]
    versions = [role["sources"]["policy"]["source_version"] for role in normalized["roles"]]
    assert refs == ["test:policy"] * 3
    assert versions == ["v1"] * 3
    report = export_revisions(normalized, attachment_root=Path("/tmp"), now=NOW)
    for role in report["roles"]:
        assert role["components"]["policy"]["status"] == "READY"


def test_adapter_source_has_no_network_or_model_imports():
    source = Path(ssa.__file__).read_text()
    for mod in ("socket", "urllib", "http.client", "openai", "anthropic", "requests"):
        assert f"import {mod}" not in source
