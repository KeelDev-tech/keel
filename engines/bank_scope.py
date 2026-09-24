#!/usr/bin/env python3
"""Governed-memory scope helpers for answer_bank.json (ADOPTION 1 of 5).

Every banked answer carries a machine-readable scope::

    "global"          - the applicant's standing answer; safe to reuse on every lead
    "employer:<Name>" - scoped to one employer; never used globally
    "ambiguous"       - scope not established; the entry belongs in
                        ``_quarantined``, never in ``answers``
    "legacy-unknown"  - pre-scope entries (plain strings and the old
                        {"answer": ..., "question_patterns": ...} dicts).
                        Read-only classification: the 93 existing entries are
                        NOT rewritten; new writes are normalized on write.

Reader contract (backward compatible):

    bank_value(entry) -> str   # the factual answer, whatever the shape
    bank_scope(entry) -> str   # "global" | "employer:<Name>" | "legacy-unknown"

Writers (tray_answer --live --bank-new/--bank-key) must route every new
entry through decide_bank_scope(); an AMBIGUOUS result is written to
``_quarantined`` with the reason, never to ``answers``.

Fail-safe: helpers never raise on unexpected shapes -- they return the
empty string / "legacy-unknown".
"""
from __future__ import annotations

import json
import os
import re

SCOPE_GLOBAL = "global"
SCOPE_AMBIGUOUS = "ambiguous"
SCOPE_LEGACY_UNKNOWN = "legacy-unknown"

#: Scopes a human may explicitly assign with --scope.
WRITABLE_SCOPES = frozenset({SCOPE_GLOBAL, SCOPE_AMBIGUOUS})


def bank_value(entry) -> str:
    """The factual answer string for a bank entry, regardless of storage shape.

    - plain string (legacy)        -> as-is
    - {"value": ..., "scope": ...} (scoped write) -> value
    - {"answer": ..., ...}         (old dict form) -> answer
    - anything else                -> "" (fail-safe)

    SCOPE BLINDNESS WARNING (F34, 2026-09-17): this returns the raw value
    WITHOUT scope, provenance, or expiry checks. It is a write-path and
    legacy-compatibility helper only — all application consumers
    (briefs, payloads, guards) must read through answer_resolver, never
    this function. Direct reads discard the metadata F34 exists to
    enforce.
    """
    try:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            v = entry.get("value", entry.get("answer", ""))
            return v if isinstance(v, str) else ("" if v is None else str(v))
        if entry is None:
            return ""
        return str(entry)
    except Exception:
        return ""


def bank_scope(entry) -> str:
    """The machine-readable scope of a bank entry.

    Plain strings and old dicts without a "scope" field read as
    "legacy-unknown" (they were written before scoped memory existed).
    """
    try:
        if isinstance(entry, dict):
            scope = entry.get("scope")
            if isinstance(scope, str) and scope:
                if scope == SCOPE_GLOBAL or scope.startswith("employer:"):
                    return scope
                if scope == SCOPE_AMBIGUOUS:
                    return scope
            return SCOPE_LEGACY_UNKNOWN
        return SCOPE_LEGACY_UNKNOWN
    except Exception:
        return SCOPE_LEGACY_UNKNOWN


def parse_scope_arg(text: str) -> str:
    """Parse an explicit --scope CLI value.

    Returns "global", "employer:<Name>", or "ambiguous".
    Raises ValueError on anything else.
    """
    s = (text or "").strip()
    if s == SCOPE_GLOBAL:
        return SCOPE_GLOBAL
    if s == SCOPE_AMBIGUOUS:
        return SCOPE_AMBIGUOUS
    if s.startswith("employer:") and len(s) > len("employer:"):
        name = s[len("employer:"):].strip()
        if name:
            return f"employer:{name}"
    raise ValueError(
        f"invalid --scope {text!r}: use 'global', 'employer:<Name>', or 'ambiguous'")


# --------------------------------------------------------------------------
# Sensitivity detection (write-time scope inference)
# --------------------------------------------------------------------------

#: tray_triage classes that are consent/attestation judgments -- a banked
#: answer here is never a standing global fact without the applicant's explicit
#: --scope.
_SENSITIVE_CLASSES = frozenset({"consent", "attestation", "no_ai",
                                "recording_consent"})

#: Family-name tokens (from FAMILY[...] headers) that mark the same class.
_SENSITIVE_FAMILY_TOKENS = frozenset({
    "sms", "whatsapp", "text_message", "texting", "consent", "attest",
    "arbitration", "background_check", "background", "recording",
    "no_ai", "unaided", "unaided_work",
})


def _family_is_sensitive(family: str | None) -> bool:
    fam = (family or "").lower()
    head = fam.split("/", 1)[0]  # e.g. "interview_recording_consent"
    return any(tok in head for tok in _SENSITIVE_FAMILY_TOKENS)


def is_sensitive_card(family: str | None, question: str | None) -> bool:
    """True when the tray card is a consent/attestation judgment.

    Uses the triage classifier (question text + family) plus a family-name
    token fallback. Fail-safe: any error returns True (quarantine direction).
    """
    try:
        from tray_triage import classify
        cls = classify(question or "", family)
        if cls in _SENSITIVE_CLASSES:
            return True
    except Exception:
        return True
    try:
        return _family_is_sensitive(family)
    except Exception:
        return True


# --------------------------------------------------------------------------
# Employer mention detection
# --------------------------------------------------------------------------

_MULTIWORD_CAP_RE = re.compile(
    # Proper-name phrases only: second char must be lowercase, so ALL-CAPS
    # decision headers ("TRAVEL DECISION REQUIRED") never match.
    r"\b([A-Z][a-z][A-Za-z&']{0,}(?:\s+[A-Z][a-z][A-Za-z&']{0,}){1,3})\b")


def employer_mentioned(question: str, employers) -> str:
    """Return the employer name if the question names a specific employer.

    Checks (1) a known-employer list (queue entries' employer fields,
    matched case-insensitively on word boundaries) and (2) capitalized
    multiword tokens in the question text. Returns "" on no match.
    Fail-safe: returns "" on any error (a scope-check failure elsewhere
    already fails to quarantine).
    """
    try:
        q = question or ""
        for emp in employers or ():
            emp = str(emp or "").strip()
            if not emp:
                continue
            if re.search(r"(?i)(?<![\w&])" + re.escape(emp) + r"(?![\w&])", q):
                return emp
        for tok in _MULTIWORD_CAP_RE.findall(q):
            _op = os.environ.get("KEEL_OPERATOR_NAME", "").strip().lower()
            if _op and tok.lower() == _op:
                continue  # the operator themself is not an employer
            return tok
        return ""
    except Exception:
        return ""


def known_employers(queue_paths=None) -> set:
    """Employer names from the canonical queues (fail-safe: never raises)."""
    try:
        if queue_paths is None:
            base = os.path.join(os.path.expanduser("~"), "workspace",
                                "job-pipeline", "queue")
            queue_paths = [os.path.join(base, n) for n in (
                "standard-queue.json", "needs_input-queue.json",
                "strategic-queue.json")]
        names = set()
        for path in queue_paths:
            try:
                with open(path, encoding="utf-8") as f:
                    q = json.load(f)
            except (OSError, ValueError):
                continue
            items = q.get("items") if isinstance(q, dict) else q
            if not isinstance(items, list):
                continue
            for e in items:
                if not isinstance(e, dict):
                    continue
                for field in ("employer", "company"):
                    name = str(e.get(field) or "").strip()
                    if name:
                        names.add(name)
        return names
    except Exception:
        return set()


# --------------------------------------------------------------------------
# Scope decision (the governed write path)
# --------------------------------------------------------------------------

def decide_bank_scope(scope_arg: str | None, card: dict,
                      employers=None) -> tuple:
    """Decide the scope for a new banked answer.

    Returns (scope, quarantine_reason). "ambiguous" means: write to
    ``_quarantined`` with the reason, never to ``answers``.

    Rules (first match wins):
      1. Explicit --scope always wins (the human operator's deliberate call).
      2. Consent/attestation family or question -> ambiguous.
      3. Question names a specific employer -> ambiguous.
      4. Otherwise -> global (today's standing behavior for plain facts).
    """
    if scope_arg:
        return parse_scope_arg(scope_arg), ""
    card = card or {}
    family = card.get("family")
    question = card.get("question") or ""
    if is_sensitive_card(family, question):
        return SCOPE_AMBIGUOUS, (
            f"consent/attestation-family card (family={family!r}); a "
            "per-employer consent must never land in global answers without "
            "the applicant's explicit --scope")
    if employers is None:
        employers = known_employers()
    emp = employer_mentioned(question, employers)
    if emp:
        return SCOPE_AMBIGUOUS, (
            f"question names employer {emp!r}; per-employer answers are held "
            "out of global answers without an explicit --scope")
    return SCOPE_GLOBAL, ""
