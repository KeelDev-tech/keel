"""Scope-preserving answer resolution. No model calls and no self-approval."""
from .contracts import ContractError, timestamp, text, utcnow, versioned, digest, strict_json, canonical


def question_fingerprint(question, field_type, options=()):
    return digest({"question": text(question), "field_type": text(field_type),
                   "options": list(options)})


def resolve_answer(records, context, *, now=None):
    """context is derived from live form intel and current policy.

    The live trusted bank must attest provenance. This resolver is not that
    issuer, and record strings are not authentication of a human decision.
    No-AI/unaided declarations always require the user; drafting cannot clear them.
    """
    now = now or utcnow()
    kind = context.get("kind")
    if kind in {"no_ai", "unaided", "original_unassisted"}:
        return {"status": "NEEDS_USER", "reason": "unaided_work_boundary"}
    if kind not in {"fact", "essay", "consent", "attestation", "experience"}:
        return {"status": "NEEDS_USER", "reason": "unclassified_question"}
    required = ("candidate_id", "employer_id", "question_hash", "field_type")
    if any(type(context.get(k)) is not str or not context[k] for k in required):
        return {"status": "NEEDS_USER", "reason": "missing_question_context"}
    candidates = []
    for record in records:
        try:
            versioned(record)
            if record.get("kind") != kind or record.get("quarantined") is not False:
                continue
            if record.get("candidate_id") != context["candidate_id"]:
                continue
            if record.get("question_hash") != context["question_hash"]:
                continue
            if record.get("field_type") != context["field_type"]:
                continue
            text(record.get("source_ref")); text(record.get("verification_ref"))
            issued, expires = timestamp(record["issued_at"]), timestamp(record["expires_at"])
            if not issued <= now < expires:
                continue
            scope = record["scope"]
            if type(scope) is not dict:
                continue
            scope_kind = scope.get("type")
            if scope_kind == "employer":
                if scope.get("employer_id") != context["employer_id"]:
                    continue
            elif scope_kind == "posting":
                if (scope.get("employer_id") != context["employer_id"] or
                        not context.get("posting_id") or scope.get("posting_id") != context["posting_id"]):
                    continue
            elif scope_kind == "global_fact" and kind in {"fact", "experience"}:
                if scope.get("explicit_global_authorization_ref") is None:
                    continue
                text(scope["explicit_global_authorization_ref"])
            else:
                continue
            if kind == "attestation":
                # Do not infer that every legal acknowledgment is preauthorized.
                if record.get("attestation_key") not in context.get("authorized_attestation_keys", []):
                    continue
                text(record.get("authorization_ref"))
            if kind in {"consent", "essay"}:
                text(record.get("authorization_ref"))
            if "value" not in record or record["value"] is None:
                continue
            candidates.append(record)
        except (ContractError, KeyError, TypeError, ValueError):
            continue
    if not candidates:
        return {"status": "NEEDS_USER", "reason": "no_applicable_verified_answer"}
    if len({digest(r["value"]) for r in candidates}) != 1:
        return {"status": "NEEDS_USER", "reason": "conflicting_answers"}
    winner = max(candidates, key=lambda r: timestamp(r["issued_at"]))
    return strict_json(canonical({"status": "RESOLVED", "value": winner["value"],
            "scope": winner["scope"], "source_ref": winner["source_ref"],
            "verification_ref": winner["verification_ref"], "expires_at": winner["expires_at"],
            "record_hash": digest(winner), "question_hash": context["question_hash"]}))


def brief_answer(resolution):
    """One serializer for all brief consumers: never flatten scope away."""
    if resolution.get("status") != "RESOLVED":
        return {"action": "ASK_USER", "reason": resolution.get("reason", "unresolved")}
    return {"action": "USE_SCOPED_ANSWER", **resolution}
