"""Original connector fixtures; never live applicant evidence or authority.

Only ``make_workspace`` writes: it creates tiny, explicitly synthetic attachment
files in the caller's fixture directory. The adapter does not call these helpers.
The frozen fixture clock and authored approvals are for offline demonstrations.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path

from keel_agent.revisions import export_revisions
from keel_live.review import Principal
from keel_local.readiness import dependency_hash
from keel_trust.common import digest
from keel_workbench.model import MAX_ROLES, adapt_body
from tools.make_assurance_demo import make_envelope
from tools.make_flow_demo import NOW, lead
from tools.make_source_producer_demo import fixture_sources
from tools.make_trust_demo import make_document


ATTACHMENT = b"SYNTHETIC CONNECTOR FIXTURE ONLY. NOT APPLICANT EVIDENCE.\n"


def _scopes(document):
    return [{"workspace_id": document["workspace_id"], "role_id": row["role_id"],
             "application_id": row["identity"], "action": "PREPARE"}
            for row in document["flow"]["leads"]]


def principal_for(document):
    """Return a fictional host reader with exact scopes and no write grants."""
    if document.get("synthetic") is not True:
        raise ValueError("synthetic fixture required")
    return Principal("synthetic-connector-reader", "synthetic:connector-authority",
                     document["workspace_id"], frozenset({"review:read"}),
                     tuple(_scopes(document)))


def rebind(document):
    """Reauthor fixture trust/assurance after an intentional synthetic edit.

    This mutates the supplied fixture and uses the frozen fixture clock for
    assurance. It does not change canonical gates or reauthor source revisions
    and approval dependencies. Never use it while serving a report or on
    operational data.
    """
    if document.get("synthetic") is not True:
        raise ValueError("synthetic fixture required")
    trust = document["trust"]
    if trust is not None:
        trust["flow_export"] = deepcopy(document["flow"])
        packet = next(row for row in trust["evidence_export"]["artifacts"]
                      if row["artifact_id"] == "packet")
        trust["artifact_bindings"] = [
            {"role_id": row["role_id"], "artifact_id": packet["artifact_id"],
             "artifact_revision": packet["revision"], "artifact_sha256": digest(packet),
             "packet_dependency_hash": row["packet_dependency_hash"]}
            for row in document["flow"]["leads"]]
    document["assurance"] = make_envelope(document["flow"])
    return document


def make_workspace(directory, count=1):
    """Create independent, fully bound synthetic PREPARE roles at ``NOW``.

    The shared fictional trust packet is intentionally reused; each role has an
    independent canonical identity, attachment, source revision and approval
    scope. Files are created exclusively and existing files are never replaced.
    Fixture construction writes are separate from subsequent report evaluation.
    """
    if type(count) is not int or not 1 <= count <= MAX_ROLES:
        raise ValueError("fixture count must be an integer from 1 through 2000")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    trust = make_document()
    flow = deepcopy(trust["flow_export"])
    flow.update({
        "source_revision": "SYNTHETIC-CONNECTOR-v1",
        "leads": [lead("role-" + str(index)) for index in range(1, count + 1)],
        "holds": [], "hold_decisions": [], "releases": [],
        "tray": {"schema_version": 1, "questions": []},
        "question_dependencies": [], "attempt_events": [], "applications": [],
        "discovery": {"budget_minutes": 0, "sources": []},
        "pool": {"schema_version": 1, "ready": count, "actionable": count,
                 "observed_at": NOW.isoformat()},
    })
    trust["question_costs"] = []
    trust["research_checks"] = []
    document = adapt_body({"flow": flow, "assurance": None, "trust": trust},
                          trust["workspace_id"], synthetic=True)
    sources_snapshot = {
        "schema": "keel.revision_sources.v1", "workspace_id": document["workspace_id"],
        "snapshot": {"source_ref": "synthetic:connector-snapshot", "source_version": "fixture-v1",
                     "observed_at": NOW.isoformat(),
                     "expires_at": (NOW + timedelta(seconds=90)).isoformat()},
        "roles": [],
    }
    attachment_hash = hashlib.sha256(ATTACHMENT).hexdigest()
    for row, scope in zip(document["flow"]["leads"], _scopes(document)):
        filename = "synthetic-" + row["role_id"] + ".txt"
        with (directory / filename).open("xb") as stream:
            stream.write(ATTACHMENT)
        sources = fixture_sources(scope, now=NOW)
        sources["attachments"]["record"]["files"] = [
            {"path": filename, "purpose": "resume", "sha256": attachment_hash,
             "size_bytes": len(ATTACHMENT)}]
        row["posting_url"] = sources["target"]["record"]["canonical_posting_url"]
        sources_snapshot["roles"].append(
            {key: value for key, value in scope.items() if key != "workspace_id"}
            | {"sources": sources})

    # First derive the six captured inputs, then author explicitly fictional
    # approvals bound to those exact revisions. Both passes use actual files.
    initial = export_revisions(sources_snapshot, attachment_root=directory, now=NOW)
    for source_row, checked in zip(sources_snapshot["roles"], initial["roles"]):
        source_row["sources"]["approval"] = {
            "source_ref": "synthetic:connector-approval:" + source_row["role_id"],
            "source_version": "fixture-v1", "observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=2)).isoformat(),
            "record": {"approval_id": "synthetic-approval-" + source_row["role_id"],
                       "actor_id": "synthetic-fixture-operator",
                       "authority_record_ref": "synthetic:fixture-authority",
                       "decision": "APPROVE", "scope": deepcopy(checked["scope"]),
                       "component_revisions": deepcopy(checked["revisions"]),
                       "approved_at": NOW.isoformat(),
                       "expires_at": (NOW + timedelta(minutes=2)).isoformat(),
                       "revoked": False}}
    complete = export_revisions(sources_snapshot, attachment_root=directory, now=NOW)
    for row, checked in zip(document["flow"]["leads"], complete["roles"]):
        if not checked["envelope_inputs_ready"]:
            raise ValueError("synthetic source fixture did not bind")
        row["dependencies"] = deepcopy(checked["revisions"])
        row["packet_dependency_hash"] = dependency_hash(row["dependencies"])
    document["revision_sources"] = sources_snapshot
    return rebind(document)
