#!/usr/bin/env python3
"""Synthetic source-producer rehearsal: local files/SQLite, no live authority.

Every person, source record, approval and observation in this tool is a fixture.
This demonstrates contracts and failure behavior, not truth of live source data.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keel_agent.io import write_private
from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS
from keel_sources.capture import ingest_attachments
from keel_sources.decisions import prepare_request, decide_request, revoke_request
from keel_sources.service import build_export, status_summary
from keel_sources.store import SourceStore
from tools.make_flow_demo import NOW, lead, make_snapshot


ATTACHMENT = b"SYNTHETIC SOURCE-PRODUCER FIXTURE. NO REAL APPLICANT OR QUALIFICATION.\n"


class SyntheticClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def fixture_scope(index=1):
    role_id = "role-" + str(index)
    return {"workspace_id": "synthetic-source-producer", "role_id": role_id,
            "application_id": lead(role_id)["identity"], "action": "PREPARE"}


def fixture_sources(scope, now=NOW):
    """Author explicitly fictional source records with original fixture times."""
    observed = (now - timedelta(seconds=20)).isoformat()
    expiry = (now + timedelta(minutes=10)).isoformat()
    destination = "https://synthetic.invalid/apply/" + scope["role_id"]
    records = {
        "policy": {"policy_id": "synthetic-policy", "rules": {
            "allowed_actions": [scope["action"]], "allowed_accounts": ["synthetic-account"],
            "allowed_destinations": [destination], "requires_human_approval": True, "holds": []}},
        "form": {"form_id": "synthetic-form", "fields": [
            {"field_id": "name", "label": "Synthetic applicant name", "kind": "text", "required": True,
             "assistance_allowed": False, "answer_class": "fact"},
            {"field_id": "location", "label": "Synthetic location choice", "kind": "select", "required": True,
             "options": ["Synthetic North", "Synthetic South"], "assistance_allowed": False,
             "answer_class": "statement"}], "required_attachment_purposes": ["resume"]},
        "answers": {"fields": {"name": "Synthetic Applicant", "location": "Synthetic North"},
            "provenance": {key: {"source_ref": "synthetic:human-answer:" + key,
                "source_version": "fixture-v1", "origin": "human", "observed_at": observed,
                "evidence_refs": []} for key in ("name", "location")}},
        "attachments": {"files": [{"path": "synthetic-resume.txt", "purpose": "resume",
                                   "filename": "synthetic-resume.txt", "mime_type": "text/plain"}]},
        "target": {"role_id": scope["role_id"], "application_id": scope["application_id"],
            "canonical_posting_url": "https://synthetic.invalid/jobs/" + scope["role_id"],
            "application_url": destination, "verified": True, "verification_ref": "synthetic:target-observation"},
        "route": {"role_id": scope["role_id"], "application_id": scope["application_id"],
            "action": scope["action"], "account_id": "synthetic-account", "transport": "browser",
            "destination": destination, "target_source_ref": "synthetic:target",
            "target_source_version": "fixture-v1"},
    }
    return {component: {"source_ref": "synthetic:" + component, "source_version": "fixture-v1",
            "observed_at": observed, "expires_at": expiry,
            "record": {**record, "metadata": {"synthetic": True, "not_live_evidence": True}}}
            for component, record in records.items()}


class SyntheticProducer:
    """Isolated fixture harness; never points at an existing host store."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.clock = SyntheticClock()
        self.scope = fixture_scope()
        self.store = SourceStore(self.directory / "store", self.scope["workspace_id"], clock=self.clock)
        self.store.register_scope(self.scope)
        self.source_root = self.directory / "synthetic-originals"
        self.source_root.mkdir(mode=0o700)
        (self.source_root / "synthetic-resume.txt").write_bytes(ATTACHMENT)
        self.sources = fixture_sources(self.scope)
        self.generations = dict.fromkeys(PREAPPROVAL_COMPONENTS, 0)

    def capture(self):
        self.sources["attachments"] = ingest_attachments(self.sources["attachments"],
            source_root=self.source_root, attachment_root=self.store.attachment_root,
            scope=self.scope, now=self.clock())
        for component in PREAPPROVAL_COMPONENTS:
            self.put(component, self.sources[component])
        return self.export()

    def put(self, component, descriptor):
        result = self.store.put_source(self.scope, component, descriptor,
                                       expected_generation=self.generations[component])
        self.sources[component] = deepcopy(descriptor)
        self.generations[component] = result["generation"]
        return result

    def request(self):
        return prepare_request(self.store, self.scope,
                               expires_at=(self.clock() + timedelta(minutes=5)).isoformat())

    def decide_synthetic(self, request, decision="APPROVE"):
        """Explicit fixture operator decision. This is not actual human consent."""
        return decide_request(self.store, request["request_id"], decision=decision,
            actor_id="synthetic-fixture-operator", authority_record_ref="synthetic:fixture-authority",
            reviewed_sha256=request["review_sha256"],
            expires_at=(self.clock() + timedelta(minutes=2)).isoformat())

    def approve_synthetic(self):
        return self.decide_synthetic(self.request())

    def export(self, **kwargs):
        return build_export(self.store, **kwargs)

    def changed_answer(self, value="Synthetic Changed Applicant"):
        source = deepcopy(self.sources["answers"])
        source["source_version"] = "fixture-v2"
        source["observed_at"] = self.clock().isoformat()
        source["record"]["fields"]["name"] = value
        return self.put("answers", source)


def _check(checks, name, condition):
    checks.append({"name": name, "passed": bool(condition)})
    if not condition:
        raise AssertionError("synthetic rehearsal failed: " + name)


def run_rehearsal(output):
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    checks = []
    observations = {"synthetic": True, "not_live_evidence": True}
    empty_clock = SyntheticClock()
    empty_store = SourceStore(output / "synthetic-empty-store", fixture_scope()["workspace_id"], clock=empty_clock)
    for index in range(1, 838):
        empty_store.register_scope(fixture_scope(index))
    empty = build_export(empty_store)
    summary = status_summary(empty)
    report = empty["revision_report"]
    _check(checks, "837_empty_scopes_have_5859_missing_components_and_seven_categories",
        report["blocked_role_count"] == 837 and len(report["root_causes"]) == 7
        and sum(len(row["components"]) for row in report["roles"]) == 5859
        and all(not row["revisions"] for row in report["roles"]))
    _check(checks, "missing_sources_create_zero_human_requests", report["human_root_cause_count"] == 0)
    before = empty_store.db_path.read_bytes()
    empty_clock.advance(1)
    build_export(empty_store)
    _check(checks, "readonly_export_does_not_write_database", before == empty_store.db_path.read_bytes())
    observations["empty_scope_summary"] = summary

    producer = SyntheticProducer(output / "synthetic-complete")
    six = producer.capture()
    _check(checks, "six_captured_sources_do_not_invent_approval",
        len(six["revision_report"]["roles"][0]["revisions"]) == 6
        and six["revision_report"]["human_root_cause_count"] == 0
        and six["producer_inputs_complete_count"] == 0)
    request = producer.request()
    pending = producer.export()
    _check(checks, "explicit_request_creates_one_pending_human_decision",
        request["state"] == "PENDING" and pending["revision_report"]["human_root_cause_count"] == 1)
    producer.clock.advance(5)
    refreshed = deepcopy(producer.sources["policy"])
    refreshed["observed_at"] = producer.clock().isoformat()
    producer.put("policy", refreshed)
    decision = producer.decide_synthetic(request)
    _check(checks, "new_observation_of_same_material_preserves_pending_review", decision["state"] == "APPROVED")
    complete = producer.export()
    _check(checks, "explicit_synthetic_approval_yields_seven_reviewable_sources",
        complete["producer_inputs_complete_count"] == 1
        and len(complete["revision_report"]["roles"][0]["revisions"]) == 7)
    _check(checks, "original_source_observation_times_are_retained",
        complete["snapshot"]["roles"][0]["sources"]["answers"]["observed_at"]
        == (NOW - timedelta(seconds=20)).isoformat())
    producer.store = SourceStore(producer.store.home, producer.scope["workspace_id"], clock=producer.clock)
    _check(checks, "restart_preserves_revision_bindings",
        producer.export()["revision_report"] == complete["revision_report"])
    joined = producer.export(flow=make_snapshot())
    _check(checks, "canonical_join_requires_exact_role_and_application", joined["rows"][0]["current_flow_identity_matched"])
    wrong_flow = make_snapshot()
    wrong_flow["leads"][0]["identity"] = "a" * 64
    wrong = producer.export(flow=wrong_flow)
    _check(checks, "mismatched_canonical_identity_emits_no_revision_columns",
        wrong["producer_inputs_complete_count"] == 0
        and all(value is None for value in wrong["rows"][0]["columns"].values()))
    observations["synthetic_complete_export"] = complete
    producer.changed_answer()
    changed = producer.export()
    _check(checks, "changed_material_invalidates_prior_approval", changed["producer_inputs_complete_count"] == 0)

    rejected = SyntheticProducer(output / "synthetic-rejected")
    rejected.capture()
    rejected.decide_synthetic(rejected.request(), "REJECT")
    _check(checks, "explicit_rejection_remains_blocked", rejected.export()["producer_inputs_complete_count"] == 0)
    revoked = SyntheticProducer(output / "synthetic-revoked")
    revoked.capture()
    approved = revoked.approve_synthetic()
    revoke_request(revoked.store, approved["request_id"], actor_id="synthetic-fixture-operator",
                   reason="Synthetic revocation test only")
    _check(checks, "revoked_approval_remains_blocked", revoked.export()["producer_inputs_complete_count"] == 0)
    stale = SyntheticProducer(output / "synthetic-expired")
    stale.capture()
    stale.approve_synthetic()
    stale.clock.advance(601)
    _check(checks, "expired_sources_and_approval_remain_blocked", stale.export()["producer_inputs_complete_count"] == 0)
    invalid = SyntheticProducer(output / "synthetic-invalid-answer")
    invalid.sources["answers"]["record"]["fields"]["location"] = "Not a listed option"
    invalid.capture()
    request_blocked = False
    try:
        invalid.request()
    except ValueError:
        request_blocked = True
    _check(checks, "invalid_required_answer_blocks_approval_request",
        request_blocked and not invalid.export()["rows"][0]["semantic_validation"]["ready_for_approval"])
    _check(checks, "completion_never_claims_execution_or_source_authenticity",
        complete["execution_authorized"] is False and complete["source_authenticity_verified"] is False
        and complete["canonical_writes"] == 0)
    from tools.source_producer_inventory import inventory
    evidence = {"schema": "keel.source_producer_rehearsal.v1", "synthetic": True,
        "not_live_evidence": True, "status": "PASS", "checks": checks,
        "checks_passed": sum(item["passed"] for item in checks), "checks_total": len(checks),
        "fixture_clock_start": NOW.isoformat(), "source_inventory": inventory(),
        "model_calls": 0, "http_calls": 0, "browser_actions": 0, "canonical_writes": 0,
        "execution_authorized": False, "source_authenticity_verified": False,
        "limitations": ["All sources, applicant details and decisions are synthetic fixtures.",
                        "No real model, browser, live export, or upstream source was exercised.",
                        "Approval is a local host assertion; operator authenticity is not proved."]}
    write_private(output / "source-producer-rehearsal.json", evidence)
    write_private(output / "synthetic-observations.json", observations)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new directory; existing paths refused")
    args = parser.parse_args(argv)
    evidence = run_rehearsal(args.out)
    print(json.dumps({key: evidence[key] for key in ("status", "synthetic", "checks_passed", "checks_total",
                                                   "execution_authorized")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
