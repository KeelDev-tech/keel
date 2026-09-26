"""Explicitly synthetic fixture. Never imports or invents real applicant data."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from keel_trust.common import canonical, digest


def make_fixture(root):
    root = Path(root)
    evidence_root, packet_root = root / "evidence", root / "packet"
    evidence_root.mkdir(parents=True, exist_ok=False)
    packet_root.mkdir(parents=True, exist_ok=False)
    now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
    expires = (now + timedelta(hours=1)).isoformat()
    context = {"candidate_id": "synthetic-candidate", "employer_id": "synthetic-employer",
        "posting_id": "synthetic-posting", "kind": "fact", "question_hash": "synthetic-question",
        "field_type": "text", "claim_predicate": "synthetic-qualification"}
    scope = digest({k: context[k] for k in ("candidate_id", "employer_id", "posting_id")})
    value = "Synthetic fixture qualification; not a real applicant fact."
    record = {"schema_version": 1, "candidate_id": context["candidate_id"], "kind": "fact",
        "question_hash": context["question_hash"], "field_type": "text", "quarantined": False,
        "source_ref": "fixture://profile", "verification_ref": "fixture://bank-review",
        "issued_at": now.isoformat(), "expires_at": expires,
        "scope": {"type": "posting", "employer_id": context["employer_id"], "posting_id": context["posting_id"]},
        "value": value}
    raw = canonical({"value": value, "answer": record})
    (evidence_root / "profile.json").write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    source = {"source_id": "source-fixture", "workspace_id": "synthetic-workspace",
        "publisher_id": context["candidate_id"], "source_ref": record["source_ref"], "revision": "v1",
        "content_hash": sha, "origin": "APPLICANT_RECORD", "observed_at": now.isoformat(),
        "expires_at": expires, "status": "ACTIVE", "verification_ref": record["verification_ref"]}
    claim = {"claim_id": "claim-fixture", "workspace_id": source["workspace_id"],
        "subject_id": context["candidate_id"], "predicate": "synthetic-qualification", "value_hash": digest(value),
        "revision": "v1", "kind": "FACT", "basis": "SELF_ATTESTED", "review_state": "APPROVED",
        "approval_ref": "fixture://synthetic-approval", "allowed_scopes": [scope], "allowed_wording": [value],
        "evidence": [{k: source[k] for k in ("source_id", "revision", "content_hash")}],
        "expires_at": expires, "conflicts_with": []}
    artifact = {"artifact_id": "artifact-fixture", "workspace_id": source["workspace_id"], "revision": "v1",
        "scope": scope, "statements": [{"claim_id": claim["claim_id"], "claim_revision": "v1", "wording": value}],
        "depends_on": []}
    document = {"schema_version": 1, "workspace_id": source["workspace_id"],
        "source_revision": "SYNTHETIC-NOT-LIVE", "observed_at": now.isoformat(), "complete": True,
        "sources": [source], "claims": [claim], "artifacts": [artifact]}
    bindings = {"schema": "keel.grounding.bindings.v1", "trust_snapshot_sha256": digest(document),
        "sources": [{"source_id": source["source_id"], "revision": "v1", "path": "profile.json",
            "media_type": "application/json", "selectors": [
                {"selector_id": "value", "kind": "json_pointer", "pointer": "/value"},
                {"selector_id": "record", "kind": "json_pointer", "pointer": "/answer"}]}],
        "claims": [{"claim_id": claim["claim_id"], "revision": "v1", "value": value,
                    "selections": [{"source_id": source["source_id"], "selector_id": "value"}]}]}
    material = (value + "\n").encode()
    (packet_root / "answer.txt").write_bytes(material)
    packet = {"schema": "keel.grounding.packet.v1", "trust_snapshot_sha256": digest(document),
        "workspace_id": source["workspace_id"], "subject_id": context["candidate_id"], "scope": scope,
        "artifacts": [{"artifact_id": artifact["artifact_id"], "revision": "v1", "path": "answer.txt",
            "sha256": hashlib.sha256(material).hexdigest(), "format": "text_lines", "fields": []}],
        "attachments": []}
    answer_bindings = [{"record_hash": digest(record), "source_id": source["source_id"],
        "selector_id": "record", "claim_id": claim["claim_id"], "claim_revision": "v1"}]
    return {"document": document, "bindings": bindings, "packet": packet, "records": [record],
            "context": context, "answer_bindings": answer_bindings, "now": now}
