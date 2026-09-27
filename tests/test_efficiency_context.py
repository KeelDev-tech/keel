"""Context size, constraints, dependency provenance and stale evidence guards."""
import json

import pytest

from keel_efficiency.context import Binding, ContextAssembler, ContextError, ContextSource, compact_handoff


def assemble(sources, **changes):
    params = dict(task="repair race condition", sources=sources,
                  required_constraints=["Human approval required before submission"],
                  max_bytes=10000, current_revisions={s.source_id: s.revision for s in sources})
    params.update(changes)
    return ContextAssembler().assemble(**params)


def test_revision_bound_context_selects_relevant_skill_with_evidence_closure():
    sources = [ContextSource("skill", "v2", "repair recipe", kind="skill",
                             dependencies=(Binding("evidence", "sha-1"),), keywords=("race",)),
               ContextSource("evidence", "sha-1", "recorded symptom", provenance_refs=("test://race/22",)),
               ContextSource("noise", "v1", "garden planting tips", kind="persona")]
    result = assemble(sources)
    assert result.status == "READY"
    assert result.included == ("evidence", "skill")
    assert result.omitted == ("noise",)
    records = {source["source_id"]: source for source in result.packet["sources"]}
    assert records["skill"]["dependencies"] == [{"source_id": "evidence", "revision": "sha-1"}]
    assert records["evidence"]["provenance_refs"] == ["test://race/22"]
    assert len(records["evidence"]["content_sha256"]) == 64
    assert json.loads(result.encoded)["approval_reusable"] is False
    assert result.byte_count == len(result.encoded) == result.estimated_token_upper
    assert "not_measured" in result.token_estimate_method


def test_budget_cannot_drop_required_constraints_or_dependencies():
    sources = [ContextSource("required", "r1", "must remain", required=True,
                             dependencies=(Binding("dependency", "r2"),)),
               ContextSource("dependency", "r2", "X" * 1000)]
    full = assemble(sources)
    held = assemble(sources, max_bytes=full.byte_count - 1)
    assert held.status == "HOLD" and held.reason == "REQUIRED_CONTEXT_EXCEEDS_BUDGET"
    assert held.packet is None and not held.encoded
    assert assemble([], max_bytes=10).status == "HOLD"


def test_constraint_kind_is_mandatory_even_when_irrelevant():
    result = assemble([ContextSource("constraint", "r1", "No credential sharing", kind="constraint")])
    assert result.included == ("constraint",)


def test_whole_optional_dependency_group_is_skipped_if_it_does_not_fit():
    sources = [ContextSource("skill", "r1", "race", dependencies=(Binding("large", "r1"),)),
               ContextSource("large", "r1", "X" * 2000)]
    result = assemble(sources, max_bytes=1000)
    assert result.status == "READY" and result.included == ()
    assert result.packet["required_constraints"]


def test_cycles_terminate_and_selection_is_stable_under_input_reordering():
    sources = [ContextSource("a", "r1", "race", dependencies=(Binding("b", "r1"),)),
               ContextSource("b", "r1", "repair", dependencies=(Binding("a", "r1"),))]
    first, second = assemble(sources), assemble(list(reversed(sources)))
    assert first.included == ("a", "b")
    assert first.encoded == second.encoded


@pytest.mark.parametrize("change,reason", [
    ({"current_revisions": {}}, "STALE_OR_UNBOUND_SOURCE"),
    ({"current_revisions": {"x": "new"}}, "STALE_OR_UNBOUND_SOURCE"),
])
def test_unbound_or_stale_content_holds(change, reason):
    assert assemble([ContextSource("x", "old", "repair")], **change).reason.startswith(reason)


def test_missing_and_conflicting_dependency_revisions_hold():
    a = ContextSource("a", "1", "race", dependencies=(Binding("b", "2"),))
    assert assemble([a]).reason == "MISSING_OR_STALE_DEPENDENCY:b"
    assert assemble([a, ContextSource("b", "3", "repair")]).reason == "MISSING_OR_STALE_DEPENDENCY:b"


def test_duplicate_ids_invalid_budget_and_bad_types_rejected():
    source = ContextSource("x", "1", "repair")
    with pytest.raises(ContextError):
        assemble([source, source])
    with pytest.raises(ContextError):
        assemble([source], max_bytes=True)
    with pytest.raises(ContextError):
        ContextSource("x", "1", "x", required="yes")
    with pytest.raises(ContextError):
        ContextSource("x", "1", "x", dependencies=("not-a-binding",))


def test_no_file_or_tool_execution_of_supplied_content(tmp_path):
    marker = tmp_path / "do-not-create"
    dangerous_text = f"__import__('pathlib').Path({str(marker)!r}).touch()"
    result = assemble([ContextSource("untrusted", "r1", dangerous_text, required=True)])
    assert result.status == "READY" and not marker.exists()
    assert not result.execution_authorized


def test_handoff_retains_bindings_constraints_and_refuses_oversize():
    kwargs = dict(goal="repair queue", status="IN_PROGRESS", findings=["Race reproduced in fixture"],
                  next_steps=["Run bounded replay"], required_constraints=["Approval is always fresh"],
                  bindings=[Binding("policy", "p7"), Binding("code", "c9")], max_bytes=10000)
    result = compact_handoff(**kwargs)
    assert result.status == "READY"
    assert result.packet["required_constraints"] == kwargs["required_constraints"]
    assert [item["source_id"] for item in result.packet["bindings"]] == ["code", "policy"]
    assert result.packet["approval_reusable"] is False
    assert compact_handoff(**{**kwargs, "max_bytes": result.byte_count - 1}).status == "HOLD"
    with pytest.raises(ContextError):
        compact_handoff(**{**kwargs, "bindings": [Binding("x", "1"), Binding("x", "2")]})


def test_exact_byte_budget_handles_unicode_and_optional_dependencies():
    sources = [ContextSource("修复", "r1", "repair 状态", dependencies=(Binding("证据", "r1"),)),
               ContextSource("证据", "r1", "已验证")] 
    full = assemble(sources)
    exact = assemble(sources, max_bytes=full.byte_count)
    assert exact.encoded == full.encoded and exact.byte_count == full.byte_count
    assert assemble(sources, max_bytes=full.byte_count - 1).included == ()


def test_supplied_source_collection_is_bounded_before_selection():
    sources = [ContextSource(str(index), "r1", "x" * 262144) for index in range(65)]
    with pytest.raises(ContextError, match="16 MiB"):
        assemble(sources)
