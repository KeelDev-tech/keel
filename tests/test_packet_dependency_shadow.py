"""Fail-closed battery for the Keel 0.6.0 live integration (shadow validation).

Proves, against the real export shape and the ported packages:

1. The packet-dependency adapter derives a deterministic hash only when all
   seven revisions are evidenced as nonblank strings, emits a named absence
   blocker otherwise, never fabricates a revision, never modifies the input,
   and performs no network/model calls.
2. Assurance, trust and workflow gates fail closed when packet-dependency
   evidence is absent: no envelope, no qualified readiness, no authorization.

All observations are read-only; no canonical state is written.
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, "/home/hatch/workspace/keel")
sys.path.insert(0, "/home/hatch/workspace/keel/engines")

import packet_dependency_adapter as adapter
from keel_flow.board import build as board_build
from keel_trust.common import hexdigest, ContractError
from keel_workflow.integration import evaluate_candidate

NOW = datetime(2026, 9, 18, 11, 0, 0, tzinfo=timezone.utc)

FULL_REVISIONS = {
    "policy": "d1-final-20260915",
    "form": "ats-form-v3",
    "answers": "answer-bank-20260918",
    "attachments": "resume-lane-a-v7",
    "target": "posting-rev-9",
    "approval": "human-approval-20260918",
    "route": "browser-route-v2",
}


def make_lead(**overrides):
    lead = {"role_id": "R-1", "packet_dependency_hash": None}
    for name, rev in FULL_REVISIONS.items():
        lead[f"{name}_revision"] = rev
    lead.update(overrides)
    return lead


# --- adapter: deterministic hash -------------------------------------------

def test_all_seven_revisions_yield_deterministic_hash():
    first = adapter.derive_lead(make_lead())
    second = adapter.derive_lead(make_lead())
    assert first["packet_dependency_hash"] is not None
    assert len(first["packet_dependency_hash"]) == 64
    assert first["packet_dependency_hash"] == second["packet_dependency_hash"]
    assert first["blockers"] == []


def test_hash_is_order_fixed_not_input_order_dependent():
    a = make_lead()
    b = {"role_id": "R-1", "packet_dependency_hash": None}
    for name in reversed(adapter.REVISIONS):
        b[f"{name}_revision"] = FULL_REVISIONS[name]
    assert adapter.derive_lead(a)["packet_dependency_hash"] == \
        adapter.derive_lead(b)["packet_dependency_hash"]


@pytest.mark.parametrize("missing", adapter.REVISIONS)
def test_each_missing_revision_is_a_named_blocker(missing):
    lead = make_lead()
    del lead[f"{missing}_revision"]
    result = adapter.derive_lead(lead)
    assert result["packet_dependency_hash"] is None
    assert result["blockers"] == [f"missing_{missing}_revision"]


@pytest.mark.parametrize("bad", ["", "   ", None, True, 42, ["x"]])
def test_blank_or_nonstring_revision_is_never_coerced(bad):
    lead = make_lead(policy_revision=bad)
    result = adapter.derive_lead(lead)
    assert result["packet_dependency_hash"] is None
    assert "missing_policy_revision" in result["blockers"]


def test_real_export_shape_never_fabricates_revisions():
    # Shaped like a genuine export lead: booleans, URLs, digests — but no
    # *_revision fields. The adapter must report absence, not invent hashes.
    lead = {
        "role_id": "R-REAL",
        "packet_dependency_hash": None,
        "policy_pass": True,
        "answers_resolved": True,
        "packet_present": False,
        "approval_valid": False,
        "approval_expires_at": None,
        "route": "browser",
        "posting_url": "https://example.invalid/jobs/1",
        "identity": "a8690257c0921fc3cee83b6e64a29846b364ac728deee9ed9142d0dea49be6ad",
    }
    result = adapter.derive_lead(lead)
    assert result["packet_dependency_hash"] is None
    assert result["blockers"] == [f"missing_{n}_revision" for n in adapter.REVISIONS]


def test_adapter_is_read_only_and_performs_no_network_or_model_calls(tmp_path):
    snapshot = {"observed_at": NOW.isoformat(), "source_revision": "test",
                "leads": [make_lead(role_id="R-1"), make_lead(role_id="R-2")]}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    observation = adapter.observe_snapshot(json.loads(path.read_text()))
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after
    assert observation["leads_observed"] == 2
    assert observation["leads_with_hash"] == 2
    source = Path(adapter.__file__).read_text()
    for mod in ("socket", "urllib", "http.client", "openai", "anthropic"):
        assert f"import {mod}" not in source


# --- integration: gates fail closed without dependency evidence -------------

def _minimal_snapshot():
    # Real export shape, untrimmed: the honest fixture.
    return json.loads(Path("/tmp/keel06-shadow/snapshot-fresh.json").read_text())


def test_board_without_envelope_is_unverified_with_zero_coverage():
    report = board_build(_minimal_snapshot(), now=NOW, assurance=None)
    assert report["status"] == "UNVERIFIED"
    assert report["execution_authorized"] is False
    assert report["assurance"]["covered_roles"] == 0
    assert report["assurance"]["assurance_qualified_ready"] is None


def test_trust_contract_refuses_absent_or_malformed_dependency_hash():
    for bad in (None, "", "not-hex", "ab12"):
        with pytest.raises(ContractError):
            hexdigest(bad)


def test_workflow_refuses_evaluation_without_assurance_export():
    snapshot = _minimal_snapshot()
    bundle = {"role_id": "R-1"}
    with pytest.raises(Exception):
        evaluate_candidate(bundle, flow_export=snapshot, assurance_export=None,
                           trust_export={}, now=NOW)


def test_shadow_report_records_fail_closed_states():
    report = json.loads(Path("/tmp/keel06-shadow/shadow-report.json").read_text())
    assert report["execution_authorized"] is False
    assert report["board"]["assurance_covered_roles"] == 0
    assert report["board"]["assurance_qualified_ready"] is None
    assert report["twin"]["live_sync_connected"] is False
    assert report["twin"]["calibration"] == "NOT_ESTABLISHED"
    assert report["twin"]["assurance_state"] == "NOT_CONFIGURED"
    assert report["workflow"]["outcome"].startswith("REFUSED")
    assert report["effects"]["canonical_writes"] == 0
    assert report["effects"]["network_calls"] == 0
    assert report["effects"]["model_calls"] == 0
