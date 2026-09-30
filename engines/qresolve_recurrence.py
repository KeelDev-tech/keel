"""Pure question and delivery identities; never an answer authorization.

Reviewed fact aliases can share a notification history. Active queue obligations
remain visible: a resolved fingerprint cannot silence a new employer, role,
repark, qualifier, or source revision.
"""
from __future__ import annotations

import hashlib
import json

try:
    from .qresolve_semantics import canonical_fingerprint, fact_key
except ImportError:
    from qresolve_semantics import canonical_fingerprint, fact_key


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def question_key(family: str | None, normalized_question: str) -> str:
    """Merge reviewed aliases within one family; preserve other legacy keys."""
    identity = (canonical_fingerprint(normalized_question)
                if fact_key(normalized_question) is not None
                else normalized_question.lower())
    return _hash((family or "") + "|" + identity)


def obligation_key(entry: dict, family: str | None, normalized_question: str,
                   blocker_keys: list[str]) -> str:
    """Bind a delivery to its current lead, question meaning and queue revision.

    Only reviewed unresolved aliases are normalized. All other queue fields are
    revision evidence, including employer, park timestamp, notes, and source
    pointers. This may notify again after an unrelated queue update; it cannot
    make an unresolved obligation appear answered.
    """
    context = dict(entry)
    context["unresolved"] = sorted(set(blocker_keys))
    payload = {"version": "qresolve-obligation-v1", "context": context,
               "question_key": question_key(family, normalized_question)}
    return _hash(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False))
