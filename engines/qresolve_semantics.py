"""Conservative, offline question identity and routing for QRESOLVE.

This module neither answers questions nor authorizes writes.  A FACT result
only permits evidence lookup; provenance, scope and authorization remain the
decider's responsibility.  Confidence describes a matched rule, not a measured
probability of correctness on an applicant's private cards.

Only a finite reviewed alias table merges paraphrases. Unknown text retains
negations, quantities, punctuation and bracketed qualifiers in its identity.
There is no fuzzy matching, model call, or import-time I/O.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


FINGERPRINT_VERSION = "qresolve:v1"

# Each family has the same narrow answer meaning. Adding an alias is a semantic
# policy change and requires an explicit review and regression fixture. In
# particular, consent, commitments, discovery-channel rules and legal statements
# are not ordinary contact facts, even when an answer bank contains a value.
FACT_QUESTION_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": (
        "First name", "What is your first name?", "Please provide your first name.",
        "Your first name",
    ),
    "last_name": (
        "Last name", "What is your last name?", "Please provide your last name.",
        "Your last name",
    ),
    "email": (
        "Email address", "What is your email address?", "Please provide your email address.",
        "Your email address",
    ),
    "phone": (
        "Phone number", "What is your phone number?", "Please provide your phone number.",
        "Your phone number",
    ),
    "location": (
        "Current location", "What is your current location?", "Please provide your current location.",
        "Your current location",
    ),
    "linkedin": (
        "LinkedIn profile URL", "What is your LinkedIn profile URL?",
        "Please provide your LinkedIn profile URL.", "Your LinkedIn profile URL",
    ),
    "timezone": (
        "Time zone", "What is your time zone?", "Please provide your time zone.",
        "Your time zone",
    ),
    "ai_industry_years": (
        "Years of AI industry experience", "How many years of AI industry experience do you have?",
        "Please provide your years of AI industry experience.", "Your years of AI industry experience",
    ),
    "total_professional_years": (
        "Total years of professional experience", "How many total years of professional experience do you have?",
        "Please provide your total years of professional experience.", "Your total years of professional experience",
    ),
    "education": (
        "Highest level of education", "What is your highest level of education?",
        "Please provide your highest level of education.", "Your highest level of education",
    ),
}


def _normalize(question: str) -> str:
    if not isinstance(question, str):
        raise TypeError("question must be a string")
    # NFC preserves compatibility distinctions (e.g. a superscript quantity is
    # not silently changed to an ordinary digit). Bracketed tags can carry a
    # safety condition and must never be stripped. Keep zero-width/control
    # characters too: unfamiliar spellings cannot acquire allowlisted meaning.
    return " ".join(unicodedata.normalize("NFC", question).casefold().split())


_FACT_BY_QUESTION = {
    _normalize(alias): key
    for key, aliases in FACT_QUESTION_ALIASES.items()
    for alias in aliases
}


def fact_key(question: str) -> str | None:
    """Return a reviewed bank-key meaning, never a guessed key or bank hint."""
    return _FACT_BY_QUESTION.get(_normalize(question))


def canonical_fingerprint(question: str) -> str:
    """Versioned stable identity; merge only explicitly reviewed aliases.

    A fingerprint is question identity, not proof of an answer's applicability.
    Callers must recheck context, current evidence, scope and authorization even
    when a prior card has this fingerprint.
    """
    normalized = _normalize(question)
    key = _FACT_BY_QUESTION.get(normalized)
    payload = "fact\0" + key if key is not None else "exact\0" + normalized
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest}"


def _text_values(value: object) -> list[str]:
    # Do not stringify arbitrary objects or walk arbitrary nested structures.
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _result(kind: str, confidence: float, rationale: str, route: str) -> dict:
    return {"class": kind, "confidence": confidence, "rationale": rationale, "route": route}


_STRUCTURAL_RULES = (
    (
        r"\bapply[\s_-]*by[\s_-]*e[\s-]*mail\b|\bapplication route\b|"
        r"\b(?:send|submit|e[ -]?mail)\b.{0,60}\b(?:resume|application)\b.{0,40}\b(?:e[ -]?mail|inbox)\b|"
        r"\broute[\s_-]*(?:block|blocked|unavailable)\b",
        "structural_route", "Delivery-route blocker; a factual answer cannot authorize or repair the route.",
    ),
    (
        r"\b(?:captcha|hcaptcha|recaptcha|datadome|human verification)\b|"
        r"\b(?:prove|verify) (?:that )?you (?:are|re) human\b",
        "structural_captcha", "Human-verification blocker; route for authorized handling without answering it.",
    ),
    (
        r"\baccount[\s_-]*(?:creation|wall|required)\b|\b(?:create|register) (?:an? |your )?account\b|"
        r"\b(?:login|log in|sign in|authentication)[\s_-]*(?:wall|required|blocked)\b|"
        r"\b(?:verification|one[ -]time|security|email) (?:pass ?)?code\b|\botp\b|\b2fa\b|"
        r"\b(?:credential|password) (?:required|missing|reset)\b",
        "structural_account", "Account or verification gate; route for authorized account handling.",
    ),
    (
        r"\b(?:software[\s_-]*error|intel[\s_-]*gap|stale[\s_-]*packet|system[\s_-]*blocked)\b|"
        r"\b(?:form|field|submit button)\b.{0,35}\b(?:unreadable|broken|unresponsive|defect|missing)\b|"
        r"\b(?:exception|traceback|react[ -]select defect|answer[ -]consistency guard|"
        r"approval_unavailable|manual takeover|tool[ -]blocked)\b|"
        r"\b(?:browser session|platform)[\s\w-]{0,35}\b(?:denied|blocked)\b|"
        r"\b(?:office|onsite|on[ -]site|hybrid)[\s\w-]{0,40}\b(?:hard block|policy block|ineligible)\b|"
        r"\bhard block\b.{0,40}\b(?:office|onsite|on[ -]site|hybrid)\b",
        "structural_engineering", "Technical, intelligence, or explicit policy blocker; an answer cannot clear it.",
    ),
)

_HUMAN_ONLY_RULES = (
    (
        r"\bno[\s_-]*ai\b|\bunaided\b|\bunassisted\b|\bpersonally completed\b|"
        r"\b(?:without|did not use|have not used|not using)\b.{0,50}\b(?:ai|artificial intelligence|assistance)\b|"
        r"\b(?:own original|solely your own|entirely your own)\b",
        "An unaided-work or AI-use statement requires the applicant's explicit decision.",
    ),
    (
        r"\b(?:consent|recording|recorded|notetaker|note[ -]taker|opt[ -]?in)\b|"
        r"\b(?:attest|attestation|certify|certification|arbitration|waiver|waive|under penalty|"
        r"terms of service|privacy policy|legal agreement|acknowledg(?:e|ement|ment))\b",
        "Consent, recording, or attestation requires applicant review; a banked fact cannot grant it.",
    ),
    (
        r"\b(?:essay|personal statement|cover letter|motivation|motivates|passion|"
        r"proud of|exceptional work|tell us about yourself|why (?:us|this|our|do you|are you|would you))\b|"
        r"\b(?:describe|explain|tell us)\b.{0,80}\b(?:accomplishment|achievement|experience|challenge|interest)\b",
        "Personal voice or narrative needs the applicant's approved wording, not generated factual completion.",
    ),
    (
        r"\b(?:quarantined|do[ -]not[ -]certify|sensitive|medical|disability|religion|race|"
        r"ethnicity|gender|sexual|veteran|criminal|conviction|citizen|citizenship|immigration|"
        r"work authorization|authorized to work|sponsorship|noncompete|non[ -]compete|references?)\b",
        "Sensitive, quarantined, or legally consequential information requires applicant review.",
    ),
)

_JUDGMENT = re.compile(
    r"\b(?:availability|available|start date|start timeframe|start time|when can you start|"
    r"relocat\w*|travel|commut\w*|onsite|on[ -]site|hybrid|office|workstream|"
    r"proceed|drop|withdraw|tradeoff|trade[ -]off|compensation|salary|pay expectation|"
    r"preferred shift|work schedule|weekends|weekend|nights|overtime|shift availability)\b"
)


def classify(card: dict, contexts: list[dict] | tuple = ()) -> dict:
    """Route the card using its question and current matched lead blockers.

    Priority is structural > human-only > judgment > reviewed factual prompt.
    The caller must supply current context for every affected lead. Missing
    context is not evidence that a lead is safe; this function does not grant
    authority to resolve it. Unrecognized questions fail closed to TRENT-ONLY.
    Caller-controlled bank keys, existing drafts and numerical confidence hints
    are deliberately not classifier inputs.
    """
    if not isinstance(card, dict):
        return _result("TRENT-ONLY", 0.0, "Missing or invalid card; applicant review required.", "human_only")
    question = card.get("question")
    if not isinstance(question, str) or not question.strip():
        return _result("TRENT-ONLY", 0.0, "Missing question; applicant review required.", "human_only")

    records = [card]
    if isinstance(contexts, (list, tuple)):
        records.extend(item for item in contexts if isinstance(item, dict))
    parts = []
    unresolved = []
    for record in records:
        for field in ("question", "norm", "source", "unresolved", "queue_notes", "status_reason", "status", "blocked_reason"):
            values = _text_values(record.get(field))
            parts.extend(values)
            if field == "unresolved":
                unresolved.extend(values)
    text = "\n".join(_normalize(part) for part in parts)

    for pattern, route, reason in _STRUCTURAL_RULES:
        if re.search(pattern, text):
            return _result("STRUCTURAL", 1.0, reason, route)
    for pattern, reason in _HUMAN_ONLY_RULES:
        if re.search(pattern, text):
            return _result("TRENT-ONLY", 1.0, reason, "human_only")
    if _JUDGMENT.search(text):
        return _result("JUDGMENT", 1.0, "Work commitments or a proceed/drop tradeoff require the applicant's decision.", "human_judgment")

    key = fact_key(question)
    if key is None:
        return _result("TRENT-ONLY", 0.0, "Question has no reviewed factual meaning; applicant review required.", "human_only")
    if any(_normalize(item) and fact_key(item) != key for item in unresolved):
        return _result("TRENT-ONLY", 1.0, "Unresolved context is not the same reviewed fact; keep the mixed blockers visible.", "human_only")
    return _result("FACT", 1.0, "Exact reviewed factual prompt; current scoped provenance is still required.", "fact_evidence")
