"""Full-field file evidence, bounded model packets, memory and cache checks."""
import hashlib
import os

import pytest

from keel_loki.common import canonical, clone, digest
from keel_loki.temporal import TemporalMemory
from keel_muse.context import compile_context, demo, make_demo_inputs, validate_cache


@pytest.fixture
def context(tmp_path):
    return make_demo_inputs(tmp_path / "context")


def compile_(fixture, support=None, **kwargs):
    support = fixture["support"] if support is None else support
    inputs = {**fixture["kwargs"], "expected_support_sha256": digest(support), **kwargs}
    return compile_context(fixture["contract"], support, **inputs)


def write(fixture, path, document):
    raw = canonical(document)
    full = fixture["kwargs"]["root"] / path
    full.write_bytes(raw)
    os.chmod(full, 0o600)
    return hashlib.sha256(raw).hexdigest()


def cache_args(fixture, report):
    return {"expected_context_sha256": digest(report), "root": fixture["kwargs"]["root"],
            "source_head_provider": fixture["kwargs"]["source_head_provider"],
            "expected_policy_sha256": fixture["kwargs"]["expected_policy_sha256"],
            "expected_contract_sha256": fixture["kwargs"]["expected_contract_sha256"],
            "expected_support_sha256": report["pins"]["support_sha256"], "now": fixture["kwargs"]["now"]}


def with_memory(fixture, tmp_path, *, valid_until=None):
    now = fixture["kwargs"]["now"]
    memory = TemporalMemory(tmp_path / "memory", clock=lambda: now)
    binding = fixture["support"]["fields"]["full_name"]
    revision = {"revision_id": "name-v1", "subject_id": fixture["support"]["subject_id"], "key": "name",
                "value": fixture["fixture"]["plain_values"]["full_name"],
                "source": {"source_id": binding["source_ref"], "sha256": binding["sha256"], "classification": "personal",
                           "allowed_scopes": [fixture["support"]["scope_id"]], "permitted_uses": ["application_fact"]},
                "scope": fixture["support"]["scope_id"], "permitted_uses": ["application_fact"],
                "valid_from": now - 10, "valid_until": valid_until, "supersedes": []}
    memory.record_claim(revision, "name-capture")
    memory.record_observation({"observation_id": "name-review", "revision_id": "name-v1", "reviewer_id": "synthetic-reviewer",
                               "verdict": "verified", "source_sha256": binding["sha256"], "observed_at": now}, "name-review")
    support = clone(fixture["support"])
    support["fields"]["full_name"].update(memory_key="name", memory_revision_id="name-v1")
    return memory, support, revision


def test_complete_twelve_field_fixture_has_actual_support_for_all_active_fields(context):
    report = compile_(context)
    assert report["status"] == "COMPILED"
    assert report["coverage_complete"]
    assert report["field_count"] == 12
    assert report["active_field_count"] == 11
    assert report["host_only"]["preparation_values"] == context["fixture"]["plain_values"]
    assert len(report["host_only"]["field_manifest"]) == 12
    assert len(report["host_only"]["file_pins"]) == 12  # 11 active fields + one statement source.
    assert not report["execution_authorized"]
    assert not report["source_claim_truth_verified"]


def test_human_attestation_and_consent_values_remain_host_only(context):
    report = compile_(context)
    model_rows = {row["field_id"]: row for row in report["model_context"]["fields"]}
    for fid in ("accuracy", "recording"):
        assert model_rows[fid] == {"field_id": fid, "state": "HUMAN_WORKFLOW_REQUIRED"}
    assert report["host_only"]["preparation_values"]["accuracy"] is True
    assert report["host_only"]["preparation_values"]["recording"] is False


def test_mandatory_policy_is_exact_untruncated_and_overflow_blocks(context):
    report = compile_(context)
    assert report["model_context"]["mandatory_policy"] == context["kwargs"]["mandatory_policy"]
    full_overflow = compile_(context, max_bytes=256)
    assert full_overflow["status"] == "BLOCKED"
    assert full_overflow["model_context"] is None
    assert not full_overflow["policy_truncated"]
    policy = "mandatory" * 1000
    mandatory_overflow = compile_(context, mandatory_policy=policy, expected_policy_sha256=digest(policy), max_bytes=256)
    assert mandatory_overflow["blockers"] == [{"field_id": None, "code": "MANDATORY_POLICY_OVERFLOW"}]
    assert mandatory_overflow["model_context"] is None
    assert mandatory_overflow["host_only"]["preparation_values"] == {}


@pytest.mark.parametrize("pin", ["expected_policy_sha256", "expected_contract_sha256", "expected_support_sha256", "expected_source_head_sha256"])
def test_external_host_pins_cannot_be_selected_by_payload(context, pin):
    with pytest.raises(ValueError):
        compile_(context, **{pin: "a" * 64})


def test_missing_field_including_optional_fields_blocks_complete_preparation(context):
    for fid in ("full_name", "portfolio", "accuracy"):
        support = clone(context["support"])
        support["fields"].pop(fid)
        report = compile_(context, support)
        assert report["status"] == "BLOCKED"
        assert any(row["field_id"] == fid and row["code"] == "FIELD_EVIDENCE_MISSING" for row in report["blockers"])


def test_unresolved_condition_cannot_be_misclassified_as_inactive(context):
    support = clone(context["support"])
    support["fields"].pop("relocate")
    report = compile_(context, support)
    assert any(row == {"field_id": "move_date", "code": "CONDITION_UNRESOLVED"} for row in report["blockers"])


def test_inactive_field_support_is_not_read_or_filled(context):
    support = clone(context["support"])
    support["fields"]["onsite_city"] = {**support["fields"]["full_name"], "path": "must-not-open.json"}
    report = compile_(context, support)
    assert any(row["code"] == "INACTIVE_FIELD_SUPPORT_PRESENT" for row in report["blockers"])
    assert "onsite_city" not in report["host_only"]["preparation_values"]


@pytest.mark.parametrize("change", [{"scope_id": "other-role"}, {"permitted_uses": ["human_review"]},
                                    {"valid_until": 1799999999}, {"valid_from": 1800000001, "valid_until": 1800000010}])
def test_privacy_use_and_freshness_filter_before_file_value_exposure(context, change):
    support = clone(context["support"])
    support["fields"]["full_name"].update(change)
    report = compile_(context, support)
    assert report["status"] == "BLOCKED"
    assert "full_name" not in report["host_only"]["preparation_values"]
    rows = [row for row in report["model_context"]["fields"] if row["field_id"] == "full_name"]
    assert "value" not in rows[0]


@pytest.mark.parametrize("assistance", ["unaided", "no_ai"])
def test_unaided_and_no_ai_fields_are_human_workflows_even_if_captured(context, assistance):
    contract = clone(context["contract"])
    contract["fields"][0]["assistance"] = assistance
    support = clone(context["support"])
    binding = support["fields"]["full_name"]
    binding.update(mode="human", permitted_uses=["human_review"])
    report = compile_context(contract, support, **{**context["kwargs"], "expected_contract_sha256": digest(contract),
                                                  "expected_support_sha256": digest(support)})
    assert report["status"] == "BLOCKED"
    assert any(row["code"] == "SEPARATE_UNAIDED_OR_NO_AI_WORKFLOW_REQUIRED" for row in report["blockers"])
    assert report["model_context"]["fields"][0] == {"field_id": "full_name", "state": "HUMAN_WORKFLOW_REQUIRED"}


def test_attestation_cannot_use_factual_memory_or_fact_binding(context):
    support = clone(context["support"])
    support["fields"]["accuracy"].update(mode="fact", permitted_uses=["application_fact"])
    report = compile_(context, support)
    assert any(row["code"] == "human_only_field_not_reusable" for row in report["blockers"])
    support["fields"]["accuracy"].update(mode="human", memory_key="consent", memory_revision_id="rev-1")
    with pytest.raises(ValueError, match="memory_reuse_for_facts_only"):
        compile_(context, support)


def test_statement_claim_partition_covers_every_character_and_every_mapping(context):
    report = compile_(context)
    assert len(report["host_only"]["evidence_sha256"]["motivation"]) == 2
    support = clone(context["support"])
    support["fields"]["motivation"]["claims"] = []
    report = compile_(context, support)
    assert any(row["code"] == "statement_claim_coverage_incomplete" for row in report["blockers"])


def test_unmapped_claim_text_and_changed_supporting_evidence_block(context):
    import json
    path = context["kwargs"]["root"] / "motivation.json"
    document = json.loads(path.read_text())
    document["value"] += " Unsupported additional achievement."
    support = clone(context["support"])
    support["fields"]["motivation"]["sha256"] = write(context, "motivation.json", document)
    report = compile_(context, support)
    assert any(row["code"] == "statement_text_not_fully_partitioned" for row in report["blockers"])
    write(context, "motivation-source.json", {"text": "different"})
    report = compile_(context, support)
    assert any(row["code"] == "source_file_pin_changed" for row in report["blockers"])


def test_actual_file_change_or_json_pointer_mismatch_never_yields_value(context):
    support = clone(context["support"])
    support["fields"]["full_name"]["pointer"] = "/absent"
    result = compile_(context, support)
    assert "full_name" not in result["host_only"]["preparation_values"]
    (context["kwargs"]["root"] / "full_name.json").write_text('{"value":"changed"}')
    result = compile_(context)
    assert "full_name" not in result["host_only"]["preparation_values"]
    assert any(row["code"] == "source_file_pin_changed" for row in result["blockers"])


def test_contract_rejects_choice_and_boolean_type_coercion(context):
    for field_id, value in (("region", "not-an-option"), ("relocate", 1)):
        support = clone(context["support"])
        support["fields"][field_id]["sha256"] = write(context, field_id + ".json", {"value": value})
        result = compile_(context, support)
        assert any(row["field_id"] == field_id and row["code"] == "FIELD_SUPPORT_INVALID" for row in result["blockers"])


def test_head_change_between_reads_quarantines_entire_compilation(context):
    calls = [0]
    def provider():
        calls[0] += 1
        return context["source_head"] if calls[0] == 1 else {**context["source_head"], "store_id": "changed"}
    with pytest.raises(ValueError, match="source_head_pin_changed"):
        compile_(context, source_head_provider=provider)


@pytest.mark.parametrize("change,code", [
    ({"record_presence": {"approval": False}}, "source_family_missing_approval"),
    ({"approval": {"decision": "REJECT"}}, "positive_approval_observation_required"),
    ({"approval": {"revoked": True}}, "positive_approval_observation_required"),
    ({"revoked_families": ["policy"]}, "source_head_revoked"),
    ({"valid_until": 1800000000}, "source_head_expired_or_future"),
])
def test_missing_rejected_revoked_or_expired_real_source_heads_block(context, change, code):
    head = clone(context["source_head"])
    for key, value in change.items():
        if type(value) is dict:
            head[key].update(value)
        else:
            head[key] = value
    support = clone(context["support"])
    support["source_head_sha256"] = digest(head)
    with pytest.raises(ValueError, match=code):
        compile_(context, support, expected_source_head_sha256=digest(head), source_head_provider=lambda: head)


def test_source_approval_binding_and_contract_revisions_are_exact(context):
    head = clone(context["source_head"])
    head["approval"]["component_revisions"]["answers"] = "a" * 64
    support = clone(context["support"])
    support["source_head_sha256"] = digest(head)
    with pytest.raises(ValueError, match="source_approval_dependencies_changed"):
        compile_(context, support, expected_source_head_sha256=digest(head), source_head_provider=lambda: head)
    contract = clone(context["contract"])
    contract["revisions"]["form"] = "b" * 64
    with pytest.raises(ValueError, match="contract_source_revisions_mismatch"):
        compile_context(contract, context["support"], **{**context["kwargs"], "expected_contract_sha256": digest(contract)})


def test_source_scope_is_bound_not_just_a_self_selected_label(context):
    support = clone(context["support"])
    support["scope_id"] = "different-scope"
    for row in support["fields"].values():
        row["scope_id"] = "different-scope"
    with pytest.raises(ValueError, match="context_source_scope_mismatch"):
        compile_(context, support)


def test_temporal_memory_must_match_actual_bytes_and_current_verified_revision(context, tmp_path):
    memory, support, _ = with_memory(context, tmp_path)
    report = compile_(context, support, memory=memory, expected_memory_head_sha256=memory.verify()["head_sha256"])
    assert report["status"] == "COMPILED"
    assert report["pins"]["memory_head_sha256"] == memory.verify()["head_sha256"]


def test_late_memory_correction_invalidates_context_and_cache(context, tmp_path):
    memory, support, revision = with_memory(context, tmp_path)
    old_head = memory.verify()["head_sha256"]
    report = compile_(context, support, memory=memory, expected_memory_head_sha256=old_head)
    new = {**revision, "revision_id": "name-v2", "value": "Corrected", "supersedes": ["name-v1"]}
    memory.record_claim(new, "name-correction")
    with pytest.raises(ValueError, match="trusted_checkpoint_mismatch"):
        compile_(context, support, memory=memory, expected_memory_head_sha256=old_head)
    current = compile_(context, support, memory=memory, expected_memory_head_sha256=memory.verify()["head_sha256"])
    assert any(row["code"] == "memory_revision_stale_or_conflicting" for row in current["blockers"])
    with pytest.raises(ValueError, match="trusted_checkpoint_mismatch"):
        validate_cache(report, **cache_args(context, report), memory=memory)


def test_memory_natural_expiry_limits_cached_context(context, tmp_path):
    now = context["kwargs"]["now"]
    memory, support, _ = with_memory(context, tmp_path, valid_until=now + 10)
    report = compile_(context, support, memory=memory, expected_memory_head_sha256=memory.verify()["head_sha256"])
    assert report["valid_until"] == now + 10
    with pytest.raises(ValueError, match="context_cache_expired"):
        validate_cache(report, **{**cache_args(context, report), "now": now + 10}, memory=memory)


def test_future_valid_correction_limits_cache_without_new_head_event(context, tmp_path):
    now = context["kwargs"]["now"]
    memory, support, original = with_memory(context, tmp_path)
    future = {**original, "revision_id": "future-name", "value": "Future Name", "valid_from": now + 10,
              "supersedes": ["name-v1"]}
    memory.record_claim(future, "future-name")
    report = compile_(context, support, memory=memory, expected_memory_head_sha256=memory.verify()["head_sha256"])
    assert report["status"] == "COMPILED"
    assert report["valid_until"] == now + 10
    with pytest.raises(ValueError, match="context_cache_expired"):
        validate_cache(report, **{**cache_args(context, report), "now": now + 10}, memory=memory)


def test_cache_final_memory_recheck_catches_correction_during_source_callback(context, tmp_path):
    memory, support, original = with_memory(context, tmp_path)
    report = compile_(context, support, memory=memory, expected_memory_head_sha256=memory.verify()["head_sha256"])
    calls = [0]
    def head_provider():
        calls[0] += 1
        if calls[0] == 2:
            memory.record_claim({**original, "revision_id": "corrected", "value": "Corrected", "supersedes": ["name-v1"]}, "correction")
        return context["source_head"]
    args = {**cache_args(context, report), "source_head_provider": head_provider}
    with pytest.raises(ValueError, match="trusted_checkpoint_mismatch"):
        validate_cache(report, **args, memory=memory)


def test_cache_rechecks_all_source_bytes_and_external_policy_pins(context):
    report = compile_(context)
    args = cache_args(context, report)
    assert validate_cache(report, **args)["status"] == "CURRENT"
    assert not validate_cache(report, **args)["observation_times_refreshed"]
    with pytest.raises(ValueError, match="context_cache_pin_changed"):
        validate_cache(report, **{**args, "expected_policy_sha256": "a" * 64})
    with pytest.raises(ValueError, match="source_head_pin_changed"):
        validate_cache(report, **{**args, "source_head_provider": lambda: {**context["source_head"], "store_id": "changed"}})
    write(context, "full_name.json", {"value": "changed"})
    with pytest.raises(ValueError, match="source_file_pin_changed"):
        validate_cache(report, **args)


def test_cache_tampering_and_blocked_packet_cannot_be_reused(context):
    report = compile_(context)
    args = cache_args(context, report)
    altered = clone(report)
    altered["host_only"]["preparation_values"]["full_name"] = "forged"
    with pytest.raises(ValueError, match="context_cache_tampered"):
        validate_cache(altered, **args)
    blocked = compile_(context, max_bytes=256)
    with pytest.raises(ValueError, match="complete_non_authorizing_cache_required"):
        validate_cache(blocked, **cache_args(context, blocked))


def test_determinism_and_input_immutability(context):
    before = clone(context["support"])
    assert compile_(context) == compile_(context)
    assert context["support"] == before


def test_demo_reports_actual_full_field_checks_without_authority(tmp_path):
    report = demo(tmp_path / "demo")
    assert report["complete_fixture_coverage"]
    assert report["missing_field_blocks"]
    assert report["overflow_blocks_without_truncation"]
    assert report["human_values_kept_host_only"]
    assert report["actual_human_decisions"] == 0
