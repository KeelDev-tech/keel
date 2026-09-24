"""Integration checks against actual Keel reducers, not fabricated readiness."""
from copy import deepcopy
from datetime import timedelta
import pytest

from keel_workbench.demo import make_demo, NOW
from keel_workbench.service import Workbench
from keel_workbench.model import validate
from keel_agent.revisions import export_revisions
from keel_local.readiness import dependency_hash
from tools.make_assurance_demo import make_envelope
from test_agent_revisions import complete_sources, source, timestamp


def app(document=None, **kwargs):
    document = document or make_demo()
    return Workbench(document, workspace_id="keel-demo", synthetic=True, host_clock=lambda: NOW, **kwargs)


def with_sources(tmp_path, *, bind_packet=True):
    doc = make_demo(); normalized = complete_sources(tmp_path)
    row = normalized["roles"][0]; lead = doc["flow"]["leads"][0]
    normalized["workspace_id"] = doc["workspace_id"]
    row.update(role_id=lead["role_id"], application_id=lead["identity"])
    target = row["sources"]["target"]["record"]
    target.update(role_id=lead["role_id"], application_id=lead["identity"])
    row["sources"].pop("approval")
    checked = export_revisions(normalized, attachment_root=tmp_path, now=NOW)["roles"][0]
    row["sources"]["approval"] = source({"approval_id":"synthetic-only", "actor_id":"fixture-human",
        "authority_record_ref":"fixture:authority", "decision":"APPROVE", "scope":checked["scope"],
        "component_revisions":checked["revisions"], "approved_at":timestamp(-15),
        "expires_at":timestamp(500), "revoked":False}, "approval")
    row["sources"]["approval"]["observed_at"] = timestamp(-10)
    checked = export_revisions(normalized, attachment_root=tmp_path, now=NOW)["roles"][0]
    if bind_packet:
        lead["dependencies"] = checked["revisions"]
        lead["packet_dependency_hash"] = dependency_hash(lead["dependencies"])
    doc["revision_sources"] = normalized
    doc["trust"]["flow_export"] = deepcopy(doc["flow"])
    doc["trust"]["artifact_bindings"][0]["packet_dependency_hash"] = lead["packet_dependency_hash"]
    doc["assurance"] = make_envelope(doc["flow"])
    return doc


def test_demo_counts_are_actual_reducer_outputs():
    v = app().overview()
    assert v["counts"] == {"roles":9, "nominal_ready":5, "base_ready":1, "review_checks_passed":0,
                           "source_gaps":63, "source_groups":7, "human_questions":2, "material_holds":0}
    assert v["forecast"]["runway_seconds"] == 300
    assert v["assurance"]["state"] == "NOT_CONFIGURED"
    assert all(g["responsibility"] == "system" for g in v["source_inventory"]["groups"])
    assert all(value == 0 for value in v["effects"].values())


def test_snapshot_copy_isolation():
    doc = make_demo(); a = app(doc); doc["labels"][0]["company"] = "changed externally"
    snap = a.snapshot(); snap["flow"]["complete"] = False
    assert a.overview()["roles"][0]["company"] == "Northstar Logistics"
    assert a.overview()["current"]


@pytest.mark.parametrize("mutate,match", [
    (lambda d:d.update(workspace_id="other"),"workspace"),
    (lambda d:d.update(synthetic=False),"separate"),
    (lambda d:d.update(schema_version=True),"schema"),
    (lambda d:d.update(extra=True),"field"),
    (lambda d:d["labels"].append(d["labels"][0]),"duplicate"),
    (lambda d:d["labels"][0].update(lane="Invented"),"career lane"),
    (lambda d:d["labels"][0].update(role_id="unknown"),"unknown"),
    (lambda d:d["trust"]["flow_export"].update(source_revision="different"),"binding"),
    (lambda d:d["trust"].update(workspace_id="other"),"workspace"),
])
def test_import_bindings_reject_mixed_or_ambiguous_data(mutate, match):
    doc = make_demo(); mutate(doc)
    with pytest.raises(ValueError, match=match): app(doc)


def test_incomplete_and_stale_are_visible_not_refreshed():
    d=make_demo(); d["flow"]["complete"]=False; d["trust"]["flow_export"]=deepcopy(d["flow"])
    assert not app(d).overview()["current"]
    stale = Workbench(make_demo(),workspace_id="keel-demo",synthetic=True,host_clock=lambda:NOW+timedelta(seconds=91))
    assert stale.overview()["status"] == "UNVERIFIED"
    assert stale.overview()["counts"]["review_checks_passed"] == 0
    assert stale.snapshot()["flow"]["observed_at"] == NOW.isoformat()


def test_whatsapp_consent_and_unknown_attempt_never_clear():
    v=app().overview(); roles={r["role_id"]:r for r in v["roles"]}
    assert not roles["role-3"]["base_checks_passed"]
    assert any("attempt" in reason for reason in roles["role-3"]["reasons"])
    assert not roles["role-5"]["review_checks_passed"]
    assert any(q["classification"]=="CONSENT_QUARANTINED" for q in v["questions"])
    assert not any(r["release_authorized"] for r in v["holds"]["rows"])


def test_complete_sources_require_matching_packet_revisions(tmp_path):
    doc=with_sources(tmp_path,bind_packet=False); v=app(doc,attachment_root=tmp_path).overview()
    first=v["source_inventory"]["rows"][0]
    assert first["inputs_complete"] and not first["packet_bound"]
    assert len(first["binding_mismatches"]) == 7
    assert not v["roles"][0]["review_checks_passed"]
    assert "source_revision_binding_mismatch" in v["roles"][0]["reasons"]


def test_all_checks_can_pass_only_for_a_fully_bound_synthetic_fixture(tmp_path):
    doc=with_sources(tmp_path); v=app(doc,attachment_root=tmp_path).overview()
    assert v["source_inventory"]["rows"][0]["packet_bound"]
    assert v["counts"]["review_checks_passed"] == 1
    assert v["roles"][0]["review_checks_passed"]
    assert not v["execution_authorized"] and not v["source_inventory"]["source_authenticity_verified"]


def test_attachment_content_change_invalidates_review_without_writing(tmp_path):
    doc=with_sources(tmp_path); a=app(doc,attachment_root=tmp_path)
    before=a.snapshot(); (tmp_path/"resume.txt").write_bytes(b"changed synthetic material")
    v=a.overview()
    assert v["counts"]["review_checks_passed"] == 0
    assert v["roles"][0]["sources"]["approval"]["reason"] == "approval_dependency_mismatch"
    assert a.snapshot() == before


def test_files_require_an_explicit_host_root(tmp_path):
    doc=with_sources(tmp_path)
    with pytest.raises(ValueError,match="attachment-root"): app(doc)


def test_real_human_task_requires_an_observed_decision_request(tmp_path):
    doc=with_sources(tmp_path); record=doc["revision_sources"]["roles"][0]["sources"]
    record["approval"]={**source({},"approval-search"),"absence":{"kind":"HUMAN_DECISION_REQUIRED","decision_request_ref":"fixture:request"}}
    v=app(doc,attachment_root=tmp_path).overview()
    human=[g for g in v["source_inventory"]["groups"] if g["responsibility"]=="human"]
    assert len(human)==1 and human[0]["role_ids"]==["role-1"]
    del record["approval"]["observed_at"]
    assert all(g["responsibility"]=="system" for g in app(doc,attachment_root=tmp_path).overview()["source_inventory"]["groups"])


@pytest.mark.parametrize("field",["workspace_id","application_id","role_id"])
def test_source_scope_cannot_cross_workspaces_or_roles(tmp_path,field):
    doc=with_sources(tmp_path)
    target=doc["revision_sources"] if field=="workspace_id" else doc["revision_sources"]["roles"][0]
    target[field]="different"
    with pytest.raises(ValueError,match="binding|workspace"):app(doc,attachment_root=tmp_path)


def test_v07_refreshed_observation_is_diagnostic_until_host_scope_check():
    doc=make_demo(); doc["assurance"]=make_envelope(doc["flow"])
    doc["flow"]["leads"][0]["observed_at"]=(NOW-timedelta(seconds=1)).isoformat()
    doc["trust"]["flow_export"]=deepcopy(doc["flow"])
    from keel_trust.common import digest
    doc["assurance"]["export_sha256"]=digest(doc["flow"])
    v=app(doc).overview()
    assert v["assurance"]["state"]=="HOST_SCOPE_CHECK_REQUIRED"
    assert v["counts"]["review_checks_passed"]==0


def test_missing_trust_is_unknown_not_zero_material_holds():
    doc=make_demo();doc["trust"]=None
    v=app(doc).overview()
    assert v["counts"]["material_holds"] is None
    assert not v["trust_configured"] and v["evidence"] is None
