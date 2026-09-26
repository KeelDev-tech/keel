"""Conservative question grouping, with no generated answers or resolution."""
from collections import Counter
import re
import unicodedata
from .contracts import ContractError, digest, text, versioned


def normalize(value):
    return " ".join(unicodedata.normalize("NFC", text(value)).split())


def category(question):
    value = question["text"].casefold()
    if question.get("quarantined") is True or ("whatsapp" in value and question["employer"].strip().casefold() == "stripe"):
        return "CONSENT_QUARANTINED"
    if re.search(r"\b(consent|opt[ -]?in|recording|whatsapp)\b", value):
        return "CONSENT_DECISION"
    if re.search(r"\b(unaided|unassisted|certify|certification|attest|pledge)\b", value):
        return "PERSONAL_ATTESTATION"
    if re.search(r"\b(travel|relocat\w*|onsite|on-site|in-office|hybrid)\b", value):
        return "COMMITMENT_DECISION"
    if re.search(r"\b(zip|postal|residence|city)\b", value):
        return "RESIDENCE_FACT"
    if question.get("field_type") == "textarea":
        return "PERSONAL_WRITING"
    return "UNCLASSIFIED_REVIEW"


def audit_tray(document):
    versioned(document)
    questions = document.get("questions")
    if type(questions) is not list or any(type(q) is not dict for q in questions):
        raise ContractError("questions array required")
    bank = document.get("answer_bank", {})
    if type(bank) is not dict or type(bank.get("answers", {})) is not dict or type(bank.get("_provenance", {})) is not dict:
        raise ContractError("invalid answer bank")
    groups, ids = {}, set()
    for question in questions:
        qid = text(question.get("id"), "question.id")
        if qid in ids:
            raise ContractError("duplicate question id; reconcile before grouping")
        ids.add(qid)
        # Missing employer, scope, options, type or required status must NOT
        # become an empty default shared with unrelated questions.
        text(question.get("employer"), "employer"); text(question.get("policy_scope"), "policy_scope")
        text(question.get("field_type"), "field_type"); text(question.get("text"), "text")
        if type(question.get("required")) is not bool or type(question.get("options")) is not list:
            raise ContractError("explicit required bool and ordered options list required")
        if type(question.get("quarantined", False)) is not bool:
            raise ContractError("quarantined must be a bool")
        for option in question["options"]:
            text(option, "option")
        kind = category(question)
        signature = {key: question[key] for key in ("employer", "policy_scope", "field_type", "required", "options")}
        signature.update(text=normalize(question["text"]), classification=kind)
        key = digest(signature)
        if key not in groups:
            groups[key] = {"group_id": key, "employer": question["employer"],
                           "policy_scope": question["policy_scope"], "question": question["text"],
                           "options": question["options"], "classification": kind,
                           "question_ids": [], "answer_bank_candidates": [],
                           "resolved": False, "operator_reply_required": True}
        group = groups[key]
        group["question_ids"].append(qid)
        bank_key = question.get("answer_bank_key")
        # Caller-provided mapping is a REVIEW candidate, never auto-reuse.
        # Quarantined values are not even surfaced as reuse candidates.
        if kind != "CONSENT_QUARANTINED" and type(bank_key) is str and bank_key in bank.get("answers", {}):
            candidate = {"key": bank_key, "value_present": bank["answers"][bank_key] is not None,
                         "provenance_present": bool(bank.get("_provenance", {}).get(bank_key)),
                         "scope_and_truth_verified": False, "reuse_authorized": False}
            if candidate not in group["answer_bank_candidates"]:
                group["answer_bank_candidates"].append(candidate)
    rows = list(groups.values())
    return {"mode": "READ_ONLY_TRIAGE", "input_questions": len(questions), "display_groups": len(rows),
            "duplicate_presentations_removed": len(questions) - len(rows),
            "classifications": dict(Counter(g["classification"] for g in rows)),
            "groups": rows, "answers_generated": 0, "items_resolved": 0, "queue_writes": 0}
