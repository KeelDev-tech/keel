#!/usr/bin/env python3
"""Fictional, frozen-clock live-integration fixtures; no real applicant or authority.

This module is a TEST/DEMO producer. It never accepts an existing store and must
never be used to turn operational data into assurance or trust evidence. Every
source, fact, reviewer and decision here is visibly synthetic. No network,
model, rendered browser, application preparation or submission is performed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keel_agent.io import write_private
from keel_agent.revisions import PREAPPROVAL_COMPONENTS
from keel_flow.common import digest
from keel_live.connector import HostSourceConnector, EVENT_SCHEMA
from keel_live.review import Principal, ReviewService, REVIEW_ACTIONS
from keel_local.readiness import dependency_hash
from keel_sources.store import SourceStore
from tools.make_flow_demo import NOW, make_snapshot
from tools.make_source_producer_demo import SyntheticClock, fixture_sources

SYNTHETIC_WORKSPACE = "keel-demo"
SYNTHETIC_PRODUCER = "synthetic-fixture-producer"
SYNTHETIC_ATTACHMENT = b"SYNTHETIC KEEL LIVE-INTEGRATION FIXTURE. NO REAL APPLICANT OR QUALIFICATION.\n"


def fixture_flow():
    """One fictional role; retain the existing full canonical flow contract."""
    flow = make_snapshot()
    flow.update(source_revision="SYNTHETIC-LIVE-INTEGRATION-v1", leads=flow["leads"][:1],
                holds=[], hold_decisions=[], releases=[], question_dependencies=[],
                attempt_events=[], applications=[])
    flow["tray"]["questions"] = []
    flow["pool"].update(ready=1, actionable=1)
    flow["leads"][0]["posting_url"] = "https://synthetic.invalid/jobs/role-1"
    return flow


class LiveDemo:
    """A newly created, private, synthetic-only integration fixture.

    The default contains six captured source families. Approval is deliberately
    absent until the caller explicitly invokes request and decide. A Principal
    here is a host fixture claim, not proof that any real person authenticated.
    """
    synthetic = True

    def __init__(self, directory, *, capture=True):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        if self.directory.resolve(strict=True) != self.directory:
            raise ValueError("synthetic_demo_directory_symlink")
        self.clock = SyntheticClock()
        self.flow = fixture_flow()
        row = self.flow["leads"][0]
        self.scope = {"workspace_id": SYNTHETIC_WORKSPACE, "role_id": row["role_id"],
                      "application_id": row["identity"], "action": "PREPARE"}
        self.source_root = self.directory / "synthetic-originals"
        self.source_root.mkdir(mode=0o700)
        with os.fdopen(os.open(self.source_root / "synthetic-resume.txt",
                               os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
            stream.write(SYNTHETIC_ATTACHMENT)
        self.sources = fixture_sources(self.scope, now=self.clock())
        self.generations = dict.fromkeys(PREAPPROVAL_COMPONENTS, 0)
        self.principal = Principal(actor_id="synthetic-fixture-operator",
            authority_record_ref="synthetic:fixture-authority-not-human-consent",
            workspace_id=self.scope["workspace_id"], allowed_actions=REVIEW_ACTIONS,
            allowed_scopes=(self.scope,))
        self._open_store()
        self.connector.register_flow(self.flow)
        self.assurance = None
        self.trust = None
        if capture:
            self.capture()

    def _open_store(self):
        self.store = SourceStore(self.directory / "synthetic-store", SYNTHETIC_WORKSPACE,
                                 clock=self.clock)
        self.connector = HostSourceConnector(self.store,
            producer_components={SYNTHETIC_PRODUCER: PREAPPROVAL_COMPONENTS},
            action="PREPARE", attachment_source_root=self.source_root)
        self.review = ReviewService(self.store)

    @property
    def body(self):
        return {"flow": deepcopy(self.flow), "assurance": deepcopy(self.assurance),
                "trust": deepcopy(self.trust)}

    def event(self, component, descriptor=None):
        return {"schema": EVENT_SCHEMA, "producer_id": SYNTHETIC_PRODUCER,
                "scope": deepcopy(self.scope), "component": component,
                "descriptor": deepcopy(self.sources[component] if descriptor is None else descriptor),
                "expected_generation": self.generations[component],
                "flow_export_sha256": digest(self.flow)}

    def put(self, component, descriptor):
        result = self.connector.capture_event(self.event(component, descriptor), flow=self.flow)
        self.generations[component] = result["generation"]
        self.sources[component] = deepcopy(descriptor)
        return result

    def capture(self):
        for component in PREAPPROVAL_COMPONENTS:
            self.put(component, self.sources[component])
        return self.export()

    def request(self):
        return self.review.request(self.principal, self.scope,
            expires_at=(self.clock() + timedelta(minutes=5)).isoformat())

    def decide(self, pending, decision="APPROVE"):
        """An EXPLICIT synthetic fixture decision, never actual human approval."""
        return self.review.decide(self.principal, self.scope, pending["request_id"],
            decision=decision, reviewed_sha256=pending["review_sha256"],
            expires_at=(self.clock() + timedelta(minutes=2)).isoformat())

    def export(self, *, flow=None):
        return self.connector.export_workbench(flow=self.flow if flow is None else flow,
            assurance=self.assurance, trust=self.trust, synthetic=True, scopes=[self.scope],
            labels=[{"role_id": self.scope["role_id"], "company": "SYNTHETIC Example Employer",
                     "title": "SYNTHETIC Fixture Role", "lane": "General"}])

    def proof(self):
        from keel_live.proof import build_proof
        return build_proof(self.export()["workbench_snapshot"], workspace_id=SYNTHETIC_WORKSPACE,
            synthetic=True, action="PREPARE", attachment_root=self.store.attachment_root,
            now=self.clock())

    def bind_synthetic_checks(self):
        """Author a NEW synthetic flow and fixture review artifacts after approval.

        This explicit fixture-authoring operation is absent from production
        connectors. The connector itself NEVER rewrites canonical dependencies
        or invents assurance/trust reviews. No operational inputs are accepted.
        """
        from tools.make_assurance_demo import make_envelope
        from tools.make_trust_demo import make_document
        exported = self.export()["source_export"]
        if exported["producer_inputs_complete_count"] != 1:
            raise ValueError("synthetic_fixture_requires_explicit_current_approval")
        revisions = exported["revision_report"]["roles"][0]["revisions"]
        flow = deepcopy(self.flow)
        flow["source_revision"] = "SYNTHETIC-LIVE-INTEGRATION-BOUND-v1"
        flow["leads"][0]["dependencies"] = deepcopy(revisions)
        flow["leads"][0]["packet_dependency_hash"] = dependency_hash(revisions)
        trust = make_document()
        trust["source_revision"] = "SYNTHETIC-LIVE-INTEGRATION-TRUST-v1"
        trust["flow_export"] = deepcopy(flow)
        trust["artifact_bindings"] = [binding for binding in trust["artifact_bindings"]
                                       if binding["role_id"] == self.scope["role_id"]]
        trust["artifact_bindings"][0]["packet_dependency_hash"] = flow["leads"][0]["packet_dependency_hash"]
        trust["question_costs"] = []
        trust["research_checks"] = []
        self.flow, self.trust, self.assurance = flow, trust, make_envelope(flow)
        return self.export()

    def changed_answer(self):
        value = deepcopy(self.sources["answers"])
        value["source_version"] = "synthetic-fixture-v2"
        value["record"]["fields"]["name"] = "Synthetic Changed Applicant"
        self.put("answers", value)
        return value

    def restart(self):
        """Reopen only this fixture's own store, preserving its frozen host clock."""
        self._open_store()
        return self


def create_demo(new_directory, *, capture=True):
    return LiveDemo(new_directory, capture=capture)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new synthetic-only directory; existing paths refused")
    args = parser.parse_args(argv)
    demo = create_demo(args.out)
    write_private(demo.directory / "synthetic-workbench-snapshot.json", demo.export()["workbench_snapshot"])
    print(json.dumps({"synthetic": True, "not_live_evidence": True,
        "fixture_clock": NOW.isoformat(), "source_families_captured": 6,
        "approval_requests_created": 0, "execution_authorized": False,
        "model_calls": 0, "http_calls": 0, "browser_actions": 0, "canonical_writes": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
