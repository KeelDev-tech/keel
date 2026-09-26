"""Synthetic host-connector tests; no live records, identity or authority."""
from copy import deepcopy
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS
from keel_flow.common import digest
from keel_live.connector import EVENT_SCHEMA, HostSourceConnector, export_workbench
from keel_sources.decisions import prepare_request, decide_request
from keel_sources.store import SourceStore
from keel_workbench.model import project
from tools.make_flow_demo import make_snapshot
from tools.make_source_producer_demo import ATTACHMENT, SyntheticClock, fixture_scope, fixture_sources


@pytest.fixture(autouse=True)
def isolate_legacy_audit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def host(tmp_path):
    clock = SyntheticClock()
    scope = fixture_scope()
    store = SourceStore(tmp_path / "sources", scope["workspace_id"], clock=clock)
    source_root = tmp_path / "originals"
    source_root.mkdir()
    (source_root / "synthetic-resume.txt").write_bytes(ATTACHMENT)
    sources = fixture_sources(scope)
    flow = make_snapshot()
    flow["leads"][0]["posting_url"] = sources["target"]["record"]["canonical_posting_url"]
    connector = HostSourceConnector(store, producer_components={"fixture-producer": list(PREAPPROVAL_COMPONENTS)},
        action="PREPARE", attachment_source_root=source_root)
    connector.register_flow(flow)
    return {"clock": clock, "store": store, "scope": scope, "sources": sources,
            "flow": flow, "connector": connector, "source_root": source_root}


def event(host, component="policy", generation=0, descriptor=None):
    return {"schema": EVENT_SCHEMA, "producer_id": "fixture-producer", "scope": deepcopy(host["scope"]),
            "component": component, "descriptor": deepcopy(descriptor or host["sources"][component]),
            "expected_generation": generation, "flow_export_sha256": digest(host["flow"])}


def capture(host):
    for component in PREAPPROVAL_COMPONENTS:
        host["connector"].capture_event(event(host, component), flow=host["flow"])


def export(host, **kwargs):
    return host["connector"].export_workbench(flow=host["flow"], assurance=None, trust=None,
                                            synthetic=True, **kwargs)


def approve_fixture(host):
    request = prepare_request(host["store"], host["scope"],
        expires_at=(host["clock"]() + timedelta(minutes=3)).isoformat())
    return decide_request(host["store"], request["request_id"], decision="APPROVE",
        actor_id="synthetic-operator", authority_record_ref="synthetic:fixture-authority",
        reviewed_sha256=request["review_sha256"],
        expires_at=(host["clock"]() + timedelta(minutes=2)).isoformat())


def test_register_and_export_never_create_sources_or_requests(host):
    result = export(host)
    assert result["source_export"]["source_record_coverage"] == dict.fromkeys(COMPONENTS, 0)
    assert result["source_export"]["revision_report"]["human_root_cause_count"] == 0
    assert result["workbench_snapshot"]["assurance"] is None
    assert result["workbench_snapshot"]["trust"] is None
    assert result["execution_authorized"] is False


def test_six_actual_normalized_sources_stage_bytes_without_inventing_seventh(host):
    before = deepcopy(host["flow"])
    capture(host)
    result = export(host)
    source = next(r for r in result["source_export"]["rows"] if r["scope"] == host["scope"])
    assert source["semantic_validation"]["ready_for_approval"]
    assert result["source_export"]["source_record_coverage"]["approval"] == 0
    assert result["source_export"]["producer_inputs_complete_count"] == 0
    manifest = next(r for r in result["source_export"]["snapshot"]["roles"]
        if r["role_id"] == host["scope"]["role_id"])["sources"]["attachments"]["record"]["files"][0]
    assert (host["store"].attachment_root / manifest["path"]).read_bytes() == ATTACHMENT
    assert result["workbench_snapshot"]["flow"] == before == host["flow"]
    assert result["source_authenticity_verified"] is False
    assert result["factual_support_verified"] is False


def test_event_attribution_is_audited_without_claiming_identity_authentication(host):
    result = host["connector"].capture_event(event(host), flow=host["flow"])
    assert result["producer_identity_authenticated"] is False
    assert result["source_authenticity_verified"] is False
    assert result["approval_requests_created"] == 0
    audit = host["store"].events(limit=1000)
    assert audit[-1]["kind"] == "HOST_SOURCE_EVENT_CAPTURED"
    assert audit[-1]["details"]["actor_id"] == "fixture-producer"
    assert audit[-1]["details"]["revision"] == result["revision"]


@pytest.mark.parametrize("field,value,code", [
    ("producer_id", "unconfigured-producer", "producer_component_not_allowed"),
    ("component", "approval", "event_component_not_capturable"),
    ("expected_generation", True, "event_generation_invalid"),
    ("expected_generation", -1, "event_generation_invalid"),
    ("flow_export_sha256", "0" * 64, "event_flow_digest_mismatch"),
    ("schema", "unknown", "event_schema_invalid"),
])
def test_invalid_event_does_not_change_database(host, field, value, code):
    incoming = event(host)
    incoming[field] = value
    before = host["store"].db_path.read_bytes()
    with pytest.raises(ValueError, match=code):
        host["connector"].capture_event(incoming, flow=host["flow"])
    assert host["store"].db_path.read_bytes() == before


@pytest.mark.parametrize("field,value,code", [
    ("workspace_id", "different-workspace", "event_workspace_mismatch"),
    ("role_id", "missing-role", "event_current_identity_mismatch"),
    ("application_id", "0" * 64, "event_current_identity_mismatch"),
    ("action", "SUBMIT", "event_action_mismatch"),
])
def test_full_scope_must_match_host_and_current_flow(host, field, value, code):
    incoming = event(host)
    incoming["scope"][field] = value
    before = host["store"].db_path.read_bytes()
    with pytest.raises(ValueError, match=code):
        host["connector"].capture_event(incoming, flow=host["flow"])
    assert host["store"].db_path.read_bytes() == before


def test_producer_family_permissions_are_independent_of_mutable_host_config(host):
    components = ["policy"]
    connector = HostSourceConnector(host["store"], producer_components={"fixture-producer": components}, action="PREPARE")
    components.append("answers")
    with pytest.raises(ValueError, match="producer_component_not_allowed"):
        connector.capture_event(event(host, "answers"), flow=host["flow"])
    with pytest.raises(ValueError, match="connector_action_must_be_prepare"):
        HostSourceConnector(host["store"], producer_components={}, action="SUBMIT")


def test_flow_must_be_fresh_complete_and_match_event_digest(host):
    host["clock"].advance(91)
    with pytest.raises(ValueError, match="fresh canonical flow export required"):
        host["connector"].capture_event(event(host), flow=host["flow"])
    with pytest.raises(ValueError, match="fresh canonical flow export required"):
        export(host)
    host["flow"]["complete"] = False
    with pytest.raises(ValueError, match="complete flow and attempt history required"):
        export(host, allow_stale_read=True)


def test_target_cannot_be_bound_to_a_different_canonical_posting(host):
    incoming = event(host, "target")
    incoming["descriptor"]["record"]["canonical_posting_url"] = "https://synthetic.invalid/other"
    with pytest.raises(ValueError, match="target_canonical_posting_mismatch"):
        host["connector"].capture_event(incoming, flow=host["flow"])


@pytest.mark.parametrize("lead_field,lead_value,record_field,record_value,code", [
    ("route", "browser", "transport", "api", "route_canonical_transport_mismatch"),
    ("account_id", "canonical-account", "account_id", "other-account", "route_canonical_account_mismatch"),
    ("destination", "https://synthetic.invalid/canonical", "destination", "https://synthetic.invalid/other", "route_canonical_destination_mismatch"),
    ("application_url", "https://synthetic.invalid/canonical", "destination", "https://synthetic.invalid/other", "route_canonical_destination_mismatch"),
])
def test_route_capture_intersects_exported_canonical_bindings(host, lead_field, lead_value, record_field, record_value, code):
    host["flow"]["leads"][0][lead_field] = lead_value
    incoming = event(host, "route")
    incoming["descriptor"]["record"][record_field] = record_value
    with pytest.raises(ValueError, match=code):
        host["connector"].capture_event(incoming, flow=host["flow"])


def test_export_rechecks_preexisting_store_route_against_current_canonical_flow(host):
    capture(host)
    approve_fixture(host)
    host["flow"]["leads"][0]["route"] = "api"
    exported = export(host)["source_export"]
    row = next(r for r in exported["rows"] if r["scope"] == host["scope"])
    assert exported["producer_inputs_complete_count"] == 0
    assert row["canonical_binding_verified"] is False
    assert row["semantic_validation"]["ready_for_approval"] is False
    assert {i["code"] for i in row["semantic_validation"]["issues"]} == {"route_canonical_transport_mismatch"}


def test_export_rechecks_same_identity_posting_change_after_prior_capture(host):
    capture(host)
    approve_fixture(host)
    host["flow"]["leads"][0]["posting_url"] = "https://synthetic.invalid/changed-posting"
    exported = export(host)["source_export"]
    row = next(r for r in exported["rows"] if r["scope"] == host["scope"])
    assert exported["producer_inputs_complete_count"] == 0
    assert row["canonical_binding_verified"] is False
    assert any(i["code"] == "target_canonical_posting_mismatch" for i in row["semantic_validation"]["issues"])


def test_scope_registration_is_explicit_and_invalid_capture_does_not_add_it(host, tmp_path):
    store = SourceStore(tmp_path / "unregistered", host["scope"]["workspace_id"], clock=host["clock"])
    connector = HostSourceConnector(store, producer_components={"fixture-producer": ["policy"]}, action="PREPARE")
    with pytest.raises(ValueError, match="scope_not_registered"):
        connector.capture_event(event(host), flow=host["flow"])
    assert store.export_snapshot()["roles"] == []


def test_replays_and_changed_material_under_same_version_are_rejected(host):
    incoming = event(host)
    host["connector"].capture_event(incoming, flow=host["flow"])
    before = host["store"].db_path.read_bytes()
    with pytest.raises(ValueError, match="generation_conflict"):
        host["connector"].capture_event(incoming, flow=host["flow"])
    incoming["expected_generation"] = 1
    with pytest.raises(ValueError, match="source_replay"):
        host["connector"].capture_event(incoming, flow=host["flow"])
    incoming["descriptor"]["record"]["rules"]["holds"] = ["new-hold"]
    with pytest.raises(ValueError, match="source_version_material_conflict"):
        host["connector"].capture_event(incoming, flow=host["flow"])
    assert host["store"].db_path.read_bytes() == before


def test_same_generation_concurrent_deliveries_have_one_committed_capture(host):
    def deliver():
        try:
            return host["connector"].capture_event(event(host), flow=host["flow"])["generation"]
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(), range(2)))
    assert sorted(map(str, results)) == ["1", "generation_conflict"]
    assert sum(row["kind"] == "HOST_SOURCE_EVENT_CAPTURED" for row in host["store"].events(limit=1000)) == 1


def test_attachment_capture_requires_host_root_and_rejects_symlink(host):
    connector = HostSourceConnector(host["store"], producer_components={"fixture-producer": ["attachments"]}, action="PREPARE")
    with pytest.raises(ValueError, match="host_attachment_source_root_required"):
        connector.capture_event(event(host, "attachments"), flow=host["flow"])
    path = host["source_root"] / "synthetic-resume.txt"
    path.unlink()
    path.symlink_to(host["store"].db_path)
    with pytest.raises(ValueError, match="attachment_unavailable_or_symlink"):
        host["connector"].capture_event(event(host, "attachments"), flow=host["flow"])


def test_expired_source_is_retained_but_cannot_become_ready(host):
    descriptor = deepcopy(host["sources"]["policy"])
    descriptor["expires_at"] = (host["clock"]() - timedelta(seconds=1)).isoformat()
    host["connector"].capture_event(event(host, descriptor=descriptor), flow=host["flow"])
    exported = export(host)["source_export"]
    row = next(r for r in exported["rows"] if r["scope"] == host["scope"])
    assert row["columns"]["policy_revision"] is None
    assert row["semantic_validation"]["ready_for_approval"] is False


def test_long_attachment_stage_rechecks_canonical_freshness_before_commit(host, monkeypatch):
    from keel_live import connector as module
    original = module.ingest_attachments
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        host["clock"].advance(91)
        return result
    monkeypatch.setattr(module, "ingest_attachments", slow)
    before = host["store"].db_path.read_bytes()
    with pytest.raises(ValueError, match="fresh canonical flow export required"):
        host["connector"].capture_event(event(host, "attachments"), flow=host["flow"])
    assert host["store"].db_path.read_bytes() == before


def test_export_does_not_patch_dependencies_or_bypass_assurance_trust(host):
    capture(host)
    approve_fixture(host)
    before = deepcopy(host["flow"])
    result = export(host)
    assert result["source_export"]["producer_inputs_complete_count"] == 1
    snapshot = result["workbench_snapshot"]
    assert snapshot["flow"] == before
    projection = project(snapshot, attachment_root=host["store"].attachment_root, now=host["clock"]())
    assert projection["counts"]["review_checks_passed"] == 0
    assert all(r["execution_authorized"] is False for r in projection["roles"])
    assert "source_revision_binding_mismatch" in projection["roles"][0]["reasons"]


def test_changed_answer_in_new_version_invalidates_actual_approval_binding(host):
    capture(host)
    approve_fixture(host)
    descriptor = deepcopy(host["sources"]["answers"])
    descriptor["source_version"] = "fixture-v2"
    descriptor["record"]["fields"]["name"] = "Another Synthetic Name"
    host["connector"].capture_event(event(host, "answers", generation=1, descriptor=descriptor), flow=host["flow"])
    exported = export(host)["source_export"]
    assert exported["producer_inputs_complete_count"] == 0
    row = next(r for r in exported["revision_report"]["roles"] if r["scope"] == host["scope"])
    assert row["components"]["approval"]["reason"] == "approval_dependency_mismatch"


def test_proxy_profile_claims_do_not_upgrade_to_verified_factual_evidence(host):
    descriptor = deepcopy(host["sources"]["answers"])
    descriptor["record"]["provenance"]["name"].update(origin="verified_profile", evidence_refs=["asserted:unresolved-ref"])
    result = host["connector"].capture_event(event(host, "answers", descriptor=descriptor), flow=host["flow"])
    assert result["factual_support_verified"] is False
    assert result["source_authenticity_verified"] is False
    descriptor["record"]["provenance"]["name"].update(origin="generated_draft")
    with pytest.raises(ValueError, match="generated_draft_not_factual_evidence"):
        host["connector"].capture_event(event(host, "answers", generation=1, descriptor=descriptor), flow=host["flow"])


@pytest.mark.parametrize("component", ["assurance", "trust"])
def test_export_rejects_supplied_cross_flow_attestations(host, component):
    values = {"assurance": None, "trust": None}
    values[component] = ({"export_sha256": "0" * 64} if component == "assurance" else
                         {"workspace_id": host["scope"]["workspace_id"], "flow_export": {}})
    with pytest.raises(ValueError, match="binding mismatch"):
        host["connector"].export_workbench(flow=host["flow"], synthetic=True, **values)


def test_explicit_export_scope_cannot_omit_or_change_action(host):
    scope = deepcopy(host["scope"])
    scope["action"] = "SUBMIT"
    with pytest.raises(ValueError, match="event_action_mismatch"):
        export(host, scopes=[scope])
    scope.pop("action")
    with pytest.raises(ValueError, match="event_scope_invalid"):
        export(host, scopes=[scope])


def test_default_export_excludes_other_action_and_old_application_scopes(host):
    other = deepcopy(host["scope"])
    other["action"] = "SUBMIT"
    host["store"].register_scope(other)
    other["action"] = "PREPARE"
    other["application_id"] = "f" * 64
    host["store"].register_scope(other)
    result = export(host)
    assert result["excluded_noncurrent_scope_count"] == 2
    assert all(r["scope"]["action"] == "PREPARE" for r in result["source_export"]["rows"])


def test_readonly_export_preserves_database_bytes_and_current_holds(host):
    capture(host)
    before = host["store"].db_path.read_bytes()
    body = {"flow": host["flow"], "assurance": None, "trust": None}
    result = export_workbench(host["store"], body, synthetic=True)
    assert host["store"].db_path.read_bytes() == before
    assert result["workbench_snapshot"]["flow"]["holds"] == host["flow"]["holds"]
    assert result["canonical_writes"] == 0


def test_stale_diagnostic_view_keeps_observations_and_zeros_all_joined_readiness(host):
    capture(host)
    approve_fixture(host)
    host["clock"].advance(91)
    before = host["store"].db_path.read_bytes()
    result = export(host, allow_stale_read=True)
    assert result["current_flow_verified"] is False
    assert result["source_export"]["producer_inputs_complete_count"] == 0
    assert all(all(v is None for v in r["columns"].values()) for r in result["source_export"]["rows"])
    assert result["workbench_snapshot"]["flow"] == host["flow"]
    assert host["store"].db_path.read_bytes() == before
    projection = project(result["workbench_snapshot"], attachment_root=host["store"].attachment_root, now=host["clock"]())
    assert projection["current"] is False
    assert projection["counts"]["review_checks_passed"] == 0


def test_oversized_and_cyclic_events_fail_before_mutation(host):
    incoming = event(host)
    incoming["descriptor"]["record"]["metadata"]["oversize"] = "x" * (2 * 1024 * 1024)
    before = host["store"].db_path.read_bytes()
    with pytest.raises(ValueError, match="record_too_large"):
        host["connector"].capture_event(incoming, flow=host["flow"])
    incoming = event(host)
    incoming["cycle"] = incoming
    with pytest.raises(ValueError, match="record_too_complex"):
        host["connector"].capture_event(incoming, flow=host["flow"])
    assert host["store"].db_path.read_bytes() == before
