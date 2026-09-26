"""Actual source file imports; synthetic principals are never actual consent."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import os

import pytest

from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS
from keel_live.review import Principal, ReviewService
from keel_loki.common import canonical, clone, digest
from keel_muse.sources import FileSourceProducer, PrivateFileRoot, demo, head_pin, make_demo_inputs, source_head


@pytest.fixture(autouse=True)
def isolate_legacy_attachment_audit(tmp_path, monkeypatch):
    # Match the unchanged predecessor connector tests: its descriptor-relative
    # mkdir audit event is otherwise interpreted against protected code cwd.
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def host(tmp_path):
    return make_demo_inputs(tmp_path / "producer")


def capture(host, bindings=None, **overrides):
    bindings = host["bindings"] if bindings is None else bindings
    args = {"expected_bindings_sha256": digest(bindings), "flow": host["flow"],
            "expected_flow_sha256": digest(host["flow"]), "expected_head_sha256": host["expected_head_sha256"]}
    args.update(overrides)
    return host["producer"].capture(bindings, **args)


def replace_json(host, filename, value):
    raw = canonical(value)
    path = host["root"] / filename
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return hashlib.sha256(raw).hexdigest()


def principal(host, actions=None, **changes):
    values = {"actor_id": "synthetic-reviewer", "authority_record_ref": "synthetic-host-session",
              "workspace_id": host["scope"]["workspace_id"],
              "allowed_actions": frozenset(actions or {"review:read", "review:request", "review:decide"}),
              "allowed_scopes": (host["scope"],)}
    values.update(changes)
    return Principal(**values)


def decision_fixture(host):
    capture(host)
    who = principal(host)
    request = ReviewService(host["store"]).request(who, host["scope"],
                  expires_at=(host["clock"]() + timedelta(seconds=80)).isoformat())
    decision = {"schema": "keel.muse.human_decision.v1", "request_id": request["request_id"], "decision": "APPROVE",
                "reviewed_sha256": request["review_sha256"],
                "expires_at": (host["clock"]() + timedelta(seconds=70)).isoformat()}
    binding = {"producer_id": "synthetic-review-producer", "path": "decision.json",
               "sha256": replace_json(host, "decision.json", decision), "expected_generation": 1}
    return binding, decision, who


def observe(host, binding, who, **overrides):
    args = {"expected_binding_sha256": digest(binding), "principal": who, "flow": host["flow"],
            "expected_flow_sha256": digest(host["flow"]), "expected_head_sha256": head_pin(host["store"], host["scope"])}
    args.update(overrides)
    return host["producer"].observe_approval(binding, **args)


def test_six_actual_file_sources_have_real_revisions_and_missing_seventh(host):
    before = clone(host["flow"])
    result = capture(host)
    assert len(result["captured"]) == 6
    assert result["missing_families"] == ["approval"]
    assert result["source_record_coverage"] == {family: int(family != "approval") for family in COMPONENTS}
    assert all(row["generation"] == 1 and len(row["revision"]) == 64 for row in result["captured"])
    assert result["after_head_sha256"] == head_pin(host["store"], host["scope"])
    assert result["approval_requests_created"] == result["approval_decisions_recorded"] == 0
    assert host["flow"] == before
    assert not result["producer_identity_authenticated"]
    assert not result["execution_authorized"]


def test_source_observation_times_and_exact_attachment_bytes_are_preserved(host):
    capture(host)
    snapshot = host["store"].export_snapshot(scopes=[host["scope"]])
    sources = snapshot["roles"][0]["sources"]
    for family in PREAPPROVAL_COMPONENTS:
        assert sources[family]["observed_at"] == host["descriptors"][family]["observed_at"]
    member = sources["attachments"]["record"]["files"][0]
    assert (host["store"].attachment_root / member["path"]).read_bytes() == (host["root"] / "synthetic-resume.txt").read_bytes()


def test_fixed_head_is_stable_across_read_clock_but_changes_on_source_write(host):
    before = head_pin(host["store"], host["scope"])
    host["clock"].advance(1)
    assert before == head_pin(host["store"], host["scope"])
    capture(host)
    assert before != head_pin(host["store"], host["scope"])


def test_file_change_and_wrong_external_pins_fail_before_writes(host):
    before = host["store"].db_path.read_bytes()
    for kwargs in ({"expected_bindings_sha256": "a" * 64}, {"expected_flow_sha256": "a" * 64},
                   {"expected_head_sha256": "a" * 64}):
        with pytest.raises(ValueError):
            capture(host, **kwargs)
    (host["root"] / "policy.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="source_file_pin_changed"):
        capture(host)
    assert host["store"].db_path.read_bytes() == before


def test_missing_configured_file_is_real_blocker_and_no_partial_capture(host):
    before = host["store"].db_path.read_bytes()
    (host["root"] / "route.json").unlink()
    with pytest.raises(ValueError, match="source_file_unavailable_or_unsafe"):
        capture(host)
    assert host["store"].db_path.read_bytes() == before


def test_explicitly_unselected_families_remain_missing_without_invented_records(host):
    result = capture(host, {"policy": host["bindings"]["policy"]})
    assert result["source_record_coverage"]["policy"] == 1
    assert set(result["missing_families"]) == set(COMPONENTS) - {"policy"}


def test_host_selected_export_pointers_capture_all_six_without_payload_producer_selection(host):
    sha = replace_json(host, "upstream-export.json", {"sources": host["descriptors"], "producer_id": "untrusted-export-claim"})
    bindings = {family: {**host["bindings"][family], "path": "upstream-export.json", "sha256": sha,
                         "pointer": "/sources/" + family} for family in PREAPPROVAL_COMPONENTS}
    result = capture(host, bindings)
    assert len(result["captured"]) == 6
    assert {row["file_sha256"] for row in result["captured"]} == {sha}
    assert result["missing_families"] == ["approval"]


def test_missing_or_ambiguous_export_selector_blocks_all_writes(host):
    bindings = clone(host["bindings"])
    bindings["policy"]["pointer"] = "/missing"
    with pytest.raises(ValueError):
        capture(host, bindings)
    assert head_pin(host["store"], host["scope"]) == host["expected_head_sha256"]


def test_payload_cannot_select_producer_or_approval_authority(host):
    bindings = clone(host["bindings"])
    bindings["policy"]["producer_id"] = "payload-superuser"
    with pytest.raises(ValueError, match="producer_component_not_allowed"):
        capture(host, bindings)
    binding = host["bindings"]["policy"]
    with pytest.raises(ValueError, match="preapproval_families_only"):
        capture(host, {"approval": binding})
    descriptor = clone(host["descriptors"]["policy"])
    descriptor["producer_id"] = "synthetic-file-producer"
    bindings = clone(host["bindings"])
    bindings["policy"]["sha256"] = replace_json(host, "policy.json", descriptor)
    with pytest.raises(ValueError, match="source_descriptor_fields_invalid"):
        capture(host, bindings)


def test_late_invalid_family_rolls_back_all_source_rows(host):
    bindings = clone(host["bindings"])
    route = clone(host["descriptors"]["route"])
    route["record"]["destination"] = "https://synthetic.invalid/not-policy-destination"
    # A structural failure in the final family occurs after five tentative writes.
    route["record"]["extra_payload"] = True
    bindings["route"]["sha256"] = replace_json(host, "route.json", route)
    before = head_pin(host["store"], host["scope"])
    with pytest.raises(ValueError):
        capture(host, bindings)
    assert head_pin(host["store"], host["scope"]) == before
    assert all(generation == 0 for generation in source_head(host["store"], host["scope"])["generations"].values())


def test_source_replay_and_generation_conflict_do_not_refresh_evidence(host):
    capture(host)
    after = head_pin(host["store"], host["scope"])
    with pytest.raises(ValueError, match="source_head_pin_changed"):
        capture(host)
    with pytest.raises(ValueError, match="generation_conflict"):
        capture(host, expected_head_sha256=after)
    bindings = clone(host["bindings"])
    for value in bindings.values():
        value["expected_generation"] = 1
    with pytest.raises(ValueError, match="source_replay"):
        capture(host, bindings, expected_head_sha256=after)
    assert head_pin(host["store"], host["scope"]) == after


def test_concurrent_batches_with_same_head_have_one_winner(host):
    def run(_):
        try:
            return capture(host)["status"]
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(results) == ["CAPTURED", "source_head_pin_changed"]


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "permissions", "traversal", "absolute"])
def test_file_roots_reject_unsafe_reads(host, tmp_path, attack):
    binding = clone(host["bindings"]["policy"])
    path = host["root"] / "policy.json"
    if attack == "symlink":
        path.unlink()
        path.symlink_to(host["root"] / "form.json")
    elif attack == "hardlink":
        os.link(path, host["root"] / "second-link")
    elif attack == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    elif attack == "permissions":
        os.chmod(path, 0o644)
    elif attack == "traversal":
        binding["path"] = "../form.json"
    else:
        binding["path"] = str(path)
    with pytest.raises(ValueError):
        capture(host, {"policy": binding})


def test_held_root_replacement_is_detected_and_no_data_escapes(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    with PrivateFileRoot(root) as files:
        root.rename(tmp_path / "original")
        root.mkdir(mode=0o700)
        with pytest.raises(ValueError, match="source_root_changed"):
            files.check_current()


def test_attachment_hash_must_be_supplied_and_actual_bytes_must_match(host):
    (host["root"] / "synthetic-resume.txt").write_bytes(b"replacement")
    with pytest.raises(ValueError, match="source_file_pin_changed"):
        capture(host)
    assert all(generation == 0 for generation in source_head(host["store"], host["scope"])["generations"].values())


def test_existing_explicit_synthetic_review_plus_host_principal_produces_seventh(host):
    binding, _, who = decision_fixture(host)
    result = observe(host, binding, who)
    assert result["recorded_state"] == "APPROVED"
    assert result["approval_currently_valid"]
    assert result["approval_decisions_recorded"] == 1
    assert result["approval_requests_created"] == 0
    assert all(result["source_head"]["revisions"][family] for family in COMPONENTS)
    assert not result["execution_authorized"]
    assert result["identity_authentication"] == "host_responsibility"


def test_decision_payload_cannot_supply_actor_or_construct_principal(host):
    binding, decision, who = decision_fixture(host)
    with pytest.raises(ValueError, match="review_principal_required"):
        observe(host, binding, {"actor_id": "synthetic-reviewer"})
    decision["actor_id"] = "forged"
    binding["sha256"] = replace_json(host, "decision.json", decision)
    with pytest.raises(ValueError, match="exact object keys required"):
        observe(host, binding, who)


def test_absent_request_wrong_digest_and_replay_cannot_manufacture_approval(host):
    binding, decision, who = decision_fixture(host)
    actual = clone(decision)
    decision["request_id"] = "does-not-exist"
    binding["sha256"] = replace_json(host, "decision.json", decision)
    with pytest.raises(ValueError, match="approval_request_not_found"):
        observe(host, binding, who)
    decision = {**actual, "reviewed_sha256": "a" * 64}
    binding["sha256"] = replace_json(host, "decision.json", decision)
    with pytest.raises(ValueError, match="approval_review_digest_mismatch"):
        observe(host, binding, who)
    binding["sha256"] = replace_json(host, "decision.json", actual)
    observe(host, binding, who)
    binding["expected_generation"] = 2
    with pytest.raises(ValueError, match="approval_decision_immutable"):
        observe(host, binding, who)


def test_source_change_after_human_review_blocks_decision_even_with_new_head(host):
    binding, _, who = decision_fixture(host)
    policy = clone(host["descriptors"]["policy"])
    policy["source_version"] = "v2"
    policy["record"]["rules"]["holds"] = ["new-hold"]
    host["store"].put_source(host["scope"], "policy", policy, expected_generation=1)
    with pytest.raises(ValueError):
        observe(host, binding, who)
    assert source_head(host["store"], host["scope"])["generations"]["approval"] == 1


def test_review_scope_and_allowed_operation_remain_host_gates(host):
    binding, _, _ = decision_fixture(host)
    with pytest.raises(ValueError, match="review_access_denied"):
        observe(host, binding, principal(host, actions={"review:read"}))
    different = {**host["scope"], "role_id": "different-role"}
    with pytest.raises(ValueError, match="review_access_denied"):
        observe(host, binding, principal(host, allowed_scopes=(different,)))


def test_flow_freshness_canonical_target_and_approval_producer_cannot_be_bypassed(host):
    host["clock"].advance(91)
    with pytest.raises(ValueError, match="fresh canonical flow export required"):
        capture(host)


def test_demo_never_creates_synthetic_approval_as_real_observation(tmp_path):
    report = demo(tmp_path / "demo")
    assert report["approval_genuinely_absent"]
    assert report["actual_human_decisions"] == 0
    assert report["capture"]["approval_requests_created"] == 0
