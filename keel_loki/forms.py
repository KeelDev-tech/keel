"""Strict preparation contracts. Hashes bind evidence; they do not establish truth."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import urlsplit

REVISION_FAMILIES = ("policy", "form", "answers", "attachments", "target", "approval", "route")
KINDS = {"text", "select", "radio", "checkbox", "attestation", "attachment"}


class FormError(ValueError):
    pass


def clone(value):
    try:
        return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise FormError("finite JSON required") from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                                     ensure_ascii=False).encode()).hexdigest()


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise FormError(f"invalid {label} keys")


def ident(value, label="id"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,95}", value):
        raise FormError(f"invalid {label}")
    return value


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise FormError("invalid SHA256")
    return value


def integer(value, minimum=0, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise FormError("invalid integer")
    return value


def text(value, maximum=20000, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > maximum or "\x00" in value:
        raise FormError("invalid text")
    return value


def validate_revisions(value):
    exact(value, REVISION_FAMILIES, "revision families")
    for revision in value.values():
        hash_value(revision)


def validate_origin(origin):
    text(origin, 2048)
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise FormError("invalid origin") from exc
    if (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or
            "\\" in origin or not parsed.hostname or origin != f"{parsed.scheme}://{parsed.netloc}"):
        raise FormError("exact canonical origin required")
    if parsed.scheme == "http":
        if parsed.hostname != "127.0.0.1" or not port or parsed.netloc != f"127.0.0.1:{port}":
            raise FormError("HTTP permitted only for explicit loopback fixture")
    elif parsed.scheme != "https" or port not in (None, 443):
        raise FormError("HTTPS origin or explicit loopback fixture required")
    return origin


def _value(field, value):
    kind = field["kind"]
    if kind in {"checkbox", "attestation"}:
        if type(value) is not bool:
            raise FormError("checkbox and attestation require exact booleans")
        if field["required"] and not value:
            raise FormError("required acknowledgment is not approved")
    elif kind == "attachment":
        exact(value, ("name", "mime_type", "size", "sha256"), "attachment")
        text(value["name"], 128)
        if value["name"] in {".", ".."} or any(c in value["name"] for c in "/\\\r\n"):
            raise FormError("attachment basename required")
        if value["mime_type"] not in {"text/plain", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
            raise FormError("unsupported attachment MIME")
        integer(value["size"], 1, 5 * 1024 * 1024)
        hash_value(value["sha256"])
    else:
        text(value, allow_empty=not field["required"])
        if kind in {"select", "radio"} and value not in field["options"]:
            raise FormError("choice is absent from captured options")


def validate_contract(contract):
    contract = clone(contract)
    exact(contract, ("schema", "form_id", "origin", "account_id", "revisions", "fields"), "form")
    if contract["schema"] != "keel.loki.form.v1":
        raise FormError("unsupported form schema")
    ident(contract["form_id"])
    ident(contract["account_id"])
    validate_origin(contract["origin"])
    validate_revisions(contract["revisions"])
    fields = contract["fields"]
    if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
        raise FormError("one to 100 fields required")
    earlier = {}
    for field in fields:
        exact(field, ("id", "label", "kind", "required", "options", "condition", "assistance"), "field")
        ident(field["id"])
        if field["id"] in earlier:
            raise FormError("duplicate field id")
        text(field["label"], 256)
        if not isinstance(field["kind"], str) or field["kind"] not in KINDS or type(field["required"]) is not bool:
            raise FormError("invalid field type")
        if not isinstance(field["assistance"], str) or field["assistance"] not in {"allowed", "unaided", "no_ai"}:
            raise FormError("invalid assistance policy")
        options = field["options"]
        if not isinstance(options, list) or len(options) > 100:
            raise FormError("invalid options")
        if field["kind"] in {"select", "radio"}:
            if not options or any(not isinstance(v, str) or not v or len(v) > 256 for v in options):
                raise FormError("choices require nonempty text options")
            if len(set(options)) != len(options):
                raise FormError("duplicate options")
        elif options:
            raise FormError("options only valid for choice fields")
        condition = field["condition"]
        if condition is not None:
            exact(condition, ("field_id", "equals"), "condition")
            ident(condition["field_id"])
            source = earlier.get(condition["field_id"])
            if source is None or source["kind"] == "attachment":
                raise FormError("condition must reference an earlier nonattachment field")
            _value({**source, "required": False}, condition["equals"])
        earlier[field["id"]] = field
    return contract


def build_plan(contract, values, approvals, *, now):
    """Compile explicit host-approved values. Every active field needs a scoped grant.

    `approvals` is a trusted-host input, never the model's answer. This function
    cannot authenticate that host or establish evidence truth. Inactive fields
    must be absent; even optional active fields require an explicit choice.
    """
    contract = validate_contract(contract)
    integer(now)
    if not isinstance(values, dict) or not isinstance(approvals, list) or len(approvals) > 100:
        raise FormError("values map and bounded host approvals required")
    scope = digest(contract)
    approved = {}
    for item in clone(approvals):
        exact(item, ("approval_id", "actor_id", "actor_kind", "decision", "scope_sha256", "field_id",
                     "value_sha256", "evidence_sha256", "issued_at", "expires_at", "revoked"), "approval")
        ident(item["approval_id"])
        ident(item["actor_id"])
        ident(item["field_id"])
        if item["approval_id"] in approved or item["actor_kind"] != "human" or item["decision"] != "approve_prepare":
            raise FormError("unique explicit human preparation approvals required")
        hash_value(item["scope_sha256"])
        hash_value(item["value_sha256"])
        integer(item["issued_at"])
        integer(item["expires_at"])
        if type(item["revoked"]) is not bool or not item["issued_at"] <= now < item["expires_at"] or item["revoked"]:
            raise FormError("approval stale, future or revoked")
        _evidence(item["evidence_sha256"])
        approved[item["approval_id"]] = item
    rows, prepared, active_ids = [], {}, set()
    for field in contract["fields"]:
        condition = field["condition"]
        active = condition is None or (condition["field_id"] in prepared and
                                       digest(prepared[condition["field_id"]]) == digest(condition["equals"]))
        if not active:
            rows.append({"field_id": field["id"], "state": "inactive", "kind": field["kind"],
                         "value": None, "approval_id": None, "evidence_sha256": []})
            continue
        active_ids.add(field["id"])
        if field["assistance"] != "allowed":
            raise FormError("active unaided/no-AI field requires separate human workflow")
        value = values.get(field["id"])
        exact(value, ("value", "approval_id", "evidence_sha256"), "proposed field value")
        _value(field, value["value"])
        _evidence(value["evidence_sha256"])
        ident(value["approval_id"])
        approval = approved.get(value["approval_id"])
        if (approval is None or approval["field_id"] != field["id"] or approval["scope_sha256"] != scope or
                approval["value_sha256"] != digest(value["value"]) or
                approval["evidence_sha256"] != value["evidence_sha256"]):
            raise FormError("missing or mismatched scoped human preparation approval")
        prepared[field["id"]] = clone(value["value"])
        rows.append({"field_id": field["id"], "state": "prepared", "kind": field["kind"], **clone(value)})
    if set(values) != active_ids:
        raise FormError("extra/inactive or missing field values")
    return {"schema": "keel.loki.preparation.v1", "contract_sha256": scope, "origin": contract["origin"],
            "account_id": contract["account_id"], "revisions": contract["revisions"], "fields": rows,
            "valid_until": min(approved[row["approval_id"]]["expires_at"] for row in rows if row["state"] == "prepared"),
            "synthetic": False, "execution_authorized": False, "submission_authorized": False}


def _evidence(hashes):
    if not isinstance(hashes, list) or not 1 <= len(hashes) <= 32:
        raise FormError("one to 32 distinct evidence pins required")
    for value in hashes:
        hash_value(value)
    if len(set(hashes)) != len(hashes):
        raise FormError("duplicate evidence pins")


def validate_plan(contract, plan):
    """Validate all fields and types independently of gateway provenance.

    This checks completeness and pinned structure, not the authenticity of an
    approval record. Only ActionGateway with trusted host grants grants effects.
    """
    contract = validate_contract(contract)
    plan = clone(plan)
    exact(plan, ("schema", "contract_sha256", "origin", "account_id", "revisions", "fields", "valid_until",
                 "synthetic", "execution_authorized", "submission_authorized"), "preparation")
    if (plan["schema"] != "keel.loki.preparation.v1" or plan["contract_sha256"] != digest(contract) or
            plan["origin"] != contract["origin"] or plan["account_id"] != contract["account_id"] or
            plan["revisions"] != contract["revisions"] or type(plan["synthetic"]) is not bool or
            plan["execution_authorized"] is not False or plan["submission_authorized"] is not False):
        raise FormError("plan metadata/scope mismatch")
    integer(plan["valid_until"], 1)
    if not isinstance(plan["fields"], list) or len(plan["fields"]) != len(contract["fields"]):
        raise FormError("plan must cover every captured field")
    prepared = {}
    for field, row in zip(contract["fields"], plan["fields"]):
        exact(row, ("field_id", "state", "kind", "value", "approval_id", "evidence_sha256"), "prepared field")
        if row["field_id"] != field["id"] or row["kind"] != field["kind"]:
            raise FormError("plan field order, identity or kind changed")
        condition = field["condition"]
        active = condition is None or (condition["field_id"] in prepared and
                                       digest(prepared[condition["field_id"]]) == digest(condition["equals"]))
        if active:
            if row["state"] != "prepared" or field["assistance"] != "allowed":
                raise FormError("active field is not safely prepared")
            _value(field, row["value"])
            ident(row["approval_id"])
            _evidence(row["evidence_sha256"])
            prepared[field["id"]] = row["value"]
        elif row != {"field_id": field["id"], "state": "inactive", "kind": field["kind"],
                    "value": None, "approval_id": None, "evidence_sha256": []}:
            raise FormError("inactive field has actions or values")
    return plan


def inventory_blockers(contract, values, approvals, *, now):
    """Diagnostic coverage only: every field is prepared, inactive or blocked.

    A diagnostic never grants execution. build_plan and gateway revalidate all
    input before issuing any preparation capability.
    """
    contract = validate_contract(contract)
    integer(now)
    if not isinstance(values, dict) or not isinstance(approvals, list):
        raise FormError("values map and host approval list required")
    rows, prepared, inactive = [], {}, set()
    scope = digest(contract)
    for field in contract["fields"]:
        condition = field["condition"]
        if condition is not None:
            parent = condition["field_id"]
            if parent in inactive or (parent in prepared and digest(prepared[parent]) != digest(condition["equals"])):
                inactive.add(field["id"])
                rows.append({"field_id": field["id"], "status": "BLOCKED" if field["id"] in values else "INACTIVE",
                             "reason": "inactive_value_supplied" if field["id"] in values else "condition_false"})
                continue
            if parent not in prepared:
                rows.append({"field_id": field["id"], "status": "BLOCKED", "reason": "condition_unresolved"})
                continue
        try:
            if field["assistance"] != "allowed":
                raise FormError("unaided_or_no_ai")
            item = values.get(field["id"])
            exact(item, ("value", "approval_id", "evidence_sha256"), "missing_or_invalid_value")
            _value(field, item["value"])
            _evidence(item["evidence_sha256"])
            matching = [a for a in approvals if isinstance(a, dict) and a.get("approval_id") == item["approval_id"]]
            if len(matching) != 1:
                raise FormError("missing_or_duplicate_approval")
            approval = matching[0]
            exact(approval, ("approval_id", "actor_id", "actor_kind", "decision", "scope_sha256", "field_id",
                             "value_sha256", "evidence_sha256", "issued_at", "expires_at", "revoked"), "approval")
            ident(approval["actor_id"])
            ident(approval["approval_id"])
            integer(approval["issued_at"])
            integer(approval["expires_at"])
            if (approval["actor_kind"] != "human" or approval["decision"] != "approve_prepare" or
                    approval["scope_sha256"] != scope or approval["field_id"] != field["id"] or
                    approval["value_sha256"] != digest(item["value"]) or approval["evidence_sha256"] != item["evidence_sha256"] or
                    approval["revoked"] is not False or not approval["issued_at"] <= now < approval["expires_at"]):
                raise FormError("stale_revoked_or_mismatched_approval")
            prepared[field["id"]] = item["value"]
            rows.append({"field_id": field["id"], "status": "PREPARED", "reason": "scoped_human_value"})
        except FormError as exc:
            rows.append({"field_id": field["id"], "status": "BLOCKED", "reason": str(exc)})
    known = {f["id"] for f in contract["fields"]}
    extras = sorted(key for key in values if isinstance(key, str) and key not in known)
    try:
        build_plan(contract, values, approvals, now=now)
        status, validation_error = "COMPLETE", None
    except FormError as exc:
        status, validation_error = "BLOCKED", str(exc)
    return {"status": status, "fields": rows, "unknown_field_ids": extras,
            "blocked_field_count": sum(row["status"] == "BLOCKED" for row in rows),
            "validation_error": validation_error, "execution_authorized": False}


def expected_readback(contract, plan):
    """Expected observation data only. Calling this is NOT browser validation."""
    contract = validate_contract(contract)
    plan = validate_plan(contract, plan)
    return {"schema": "keel.loki.readback.v1", "contract_sha256": digest(contract),
            "origin": contract["origin"], "account_id": contract["account_id"],
            "fields": [{"field_id": row["field_id"], "kind": row["kind"], "active": row["state"] == "prepared",
                        "value": clone(row["value"])} for row in plan["fields"]], "unexpected_controls": [], "submitted": False}


def validate_readback(contract, plan, observed):
    """Strict value/type/coverage equality; caller must identify observation provenance."""
    expected = expected_readback(contract, plan)
    try:
        matches = digest(expected) == digest(observed)
    except (TypeError, ValueError):
        matches = False
    return {"status": "MATCH" if matches else "MISMATCH", "expected_sha256": digest(expected),
            "rendered_browser_validated": False, "submission_authorized": False}


def demo_fixture():
    """Frozen difficult synthetic workload; fixture approvals are not real approvals."""
    contents = b"SYNTHETIC FIXTURE ONLY\nCandidate Example\nOperations rehearsal\n"
    attachment = {"name": "fixture_resume.txt", "mime_type": "text/plain", "size": len(contents),
                  "sha256": hashlib.sha256(contents).hexdigest()}
    fields = [
        ("full_name", "Full name", "text", True, [], None),
        ("motivation", "Why this role?", "text", True, [], None),
        ("region", "Preferred region", "select", True, ["West", "East"], None),
        ("work_mode", "Work arrangement", "radio", True, ["Remote", "Hybrid", "Onsite"], None),
        ("relocate", "Open to relocation", "checkbox", False, [], None),
        ("move_date", "Available move date", "text", True, [], {"field_id": "relocate", "equals": True}),
        ("onsite_city", "Onsite city", "text", True, [], {"field_id": "work_mode", "equals": "Onsite"}),
        ("experience", "Relevant experience", "select", True, ["0-2", "3-5", "6+"], None),
        ("portfolio", "Portfolio URL (optional)", "text", False, [], None),
        ("resume", "Resume", "attachment", True, [], None),
        ("recording", "Optional recording consent", "checkbox", False, [], None),
        ("accuracy", "I reviewed these fixture answers for accuracy", "attestation", True, [], None),
    ]
    contract = {"schema": "keel.loki.form.v1", "form_id": "fixture_application", "origin": "http://127.0.0.1:8765",
                "account_id": "synthetic_candidate", "revisions": {key: digest({"fixture": key}) for key in REVISION_FAMILIES},
                "fields": [{"id": fid, "label": label, "kind": kind, "required": required, "options": options,
                            "condition": condition, "assistance": "allowed"}
                           for fid, label, kind, required, options, condition in fields]}
    plain = {"full_name": "Candidate Example", "motivation": "Synthetic operations experience for fixture testing.",
             "region": "West", "work_mode": "Hybrid", "relocate": True, "move_date": "2027-01-01",
             "experience": "3-5", "portfolio": "", "resume": attachment, "recording": False, "accuracy": True}
    result = {"contract": contract, "plain_values": plain,
              "attachments": {"resume": base64.b64encode(contents).decode()}, "synthetic": True, "now": 1000}
    return bind_fixture(result, now=1000)


def bind_fixture(fixture, *, now):
    """Generate synthetic host approvals, only for this in-memory demo fixture."""
    fixture = clone(fixture)
    if fixture.get("synthetic") is not True:
        raise FormError("fixture authority cannot be used for authentic work")
    contract = validate_contract(fixture["contract"])
    values, approvals = {}, []
    for field_id, value in fixture["plain_values"].items():
        evidence = [digest({"synthetic_evidence_for": field_id, "value": value})]
        aid = f"fixture_{field_id}"
        values[field_id] = {"value": value, "approval_id": aid, "evidence_sha256": evidence}
        approvals.append({"approval_id": aid, "actor_id": "fixture_human", "actor_kind": "human",
                          "decision": "approve_prepare", "scope_sha256": digest(contract), "field_id": field_id,
                          "value_sha256": digest(value), "evidence_sha256": evidence,
                          "issued_at": now, "expires_at": now + 300, "revoked": False})
    fixture.update({"values": values, "approvals": approvals, "now": now,
                    "host_snapshot": {"schema": "keel.loki.host.v1", "snapshot_id": "fixture_snapshot",
                                      "origin": contract["origin"], "account_id": contract["account_id"],
                                      "contract_sha256": digest(contract), "revisions": contract["revisions"],
                                      "consent": True, "approval_current": True, "no_ai": False, "holds": [],
                                      "unknown_attempt": False, "rate_limited": False, "revoked_approval_ids": [],
                                      "issued_at": now, "expires_at": now + 300, "synthetic": True}})
    return fixture
