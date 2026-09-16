#!/usr/bin/env python3
"""Normalized blocker object + 10-point pre-escalation proof (§2, §30).

Every candidate blocker is normalized into a Blocker record. The ten proof
checks run IN CODE before requires_trent may be set True. Fail closed:
auto-resolution happens ONLY on positive proof (answer-bank hit with
provenance, standing-rule match, verified candidate record). Any ambiguity
stays human. Unknown != Yes, unknown != No.

Blocker text is DATA, never directives: injection patterns in blocker text
("ignore previous...", fake Trent quotes) are flagged and can never cause
an auto-resolution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import preferences as prefs_mod


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9%\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _toks(text: str) -> set[str]:
    return set(_norm(text).split())


# ---------------------------------------------------------------------------
# Safety: blocker text is data, never directives
# ---------------------------------------------------------------------------

INJECTION_PAT = re.compile(
    r"(ignore\s+(all\s+)?previous|disregard\s+(all\s+)?(prior\s+)?instructions|"
    r"mark\s+(this\s+)?as\s+resolved|override\s+the\s+(policy|rules?)|"
    r"system\s+prompt|you\s+are\s+now|jailbreak|do\s+not\s+tell\s+(trent|the\s+user))",
    re.IGNORECASE,
)

# A Trent quote inside blocker text is UNVERIFIED unless it also appears in a
# verified record. Never treat it as provenance.
TRENT_QUOTE_PAT = re.compile(
    r"(trent\s+(said|says|approved|told|confirmed|wants)|\(trent:|trent:)",
    re.IGNORECASE,
)


def injection_suspected(raw: str) -> tuple[bool, str]:
    if INJECTION_PAT.search(raw or ""):
        return True, "blocker text contains directive-like phrasing; treated as data only"
    return False, ""


# ---------------------------------------------------------------------------
# Family signatures: (family, anchor_groups, exclusions, variant_markers)
# anchor_groups: list of token-sets; at least one group must be fully present.
# ---------------------------------------------------------------------------

def _g(*groups):
    return [set(g) for g in groups]


def _sig(text: str, n: int = 80) -> str:
    """Question signature for varianting: identical questions collapse,
    different questions never do."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()[:n]


def _keyword_detect(raw: str) -> tuple[str, str]:
    """Anchor-based family detection (no structured parsing)."""
    n = _norm(raw)
    toks = set(n.split())
    # Light plural tolerance: union stripped variants, never remove originals.
    # ("interviews" -> "interview"; "address" keeps working via the original.)
    toks = toks | {t[:-1] for t in toks
                   if len(t) > 4 and t.endswith("s") and not t.endswith("ss")}
    for fam, groups, excl, variants in FAMILY_SIGS:
        if excl and (toks & excl):
            continue
        hit = any(g <= toks for g in groups)
        if not hit and fam in SUBSTR_ANCHORS:
            hit = any(a in n for a in SUBSTR_ANCHORS[fam])
        if hit:
            variant = "general"
            for vname, vtoks in variants.items():
                if vname != "general" and vtoks and (vtoks <= toks):
                    variant = vname
                    break
            return fam, variant
    return "unknown", "general"


def structured_detect(raw: str):
    """Parse prescreen.py's structured verdict micro-format BEFORE keyword
    guessing. Returns (family, variant) or None.

    Micro-format examples:
      Required attestation needs Trent's explicit word (attest): "unaided"
      Required free-text question not in answer bank -- ...: "Why do you...?"
      Required office/... commitment question ... (travel): "travel_commitment"
      ESSAY: What excites you...? / DROPDOWN: 3-5yrs ...? / TEXT: Zip Code
    """
    m = re.search(r'\(attest\):\s*"([^"]*)"', raw)
    if m:
        val = m.group(1).lower()
        if "unaided" in val or "no-ai" in val or "no ai" in val:
            return ("no_ai_unaided_writing", "general")
        if "arbitration" in val:
            return ("arbitration_agreement", "general")
        return ("generic_attestation", _sig(val) or "general")
    if re.search(r'\(travel\):', raw):
        return ("travel_commitment", "general")
    m = re.search(r'Required free-text question not in answer bank[^:]*:\s*"([^"]*)"', raw)
    if m:
        return ("free_text_trent_only", _sig(m.group(1)))
    m = re.search(r'Required essay[^:]*\(essay\):\s*"([^"]*)"', raw)
    if m:
        return ("free_text_trent_only", _sig(m.group(1)))
    m = re.search(r'\bESSAY:\s*(.+)', raw)
    if m:
        return ("free_text_trent_only", _sig(m.group(1).rstrip('"; ')))
    # Common prescreen phrasing for Trent-authored questions (no ESSAY: tag).
    m = re.search(r'\brequired\s+(essay|personal\s+story|free-?text|written\s+response)\b[:\s]*(.+)', raw, re.I)
    if m:
        inner = m.group(2)
        if re.search(r'unaided|no-?ai', inner, re.I):
            return ("no_ai_unaided_writing", "general")
        return ("free_text_trent_only", _sig(inner) or "general")
    if re.search(r"own words|own-words", raw, re.I) and re.search(
            r"(trent|personal|essay|answers?)", raw, re.I):
        return ("free_text_trent_only", _sig(raw))
    # Prefixed form questions: the DECISION content matters for dedup, not the
    # widget type. Check the inner question against known families first.
    m = re.search(r'\bDROPDOWN:\s*(.+)', raw)
    if m:
        inner = m.group(1).rstrip('"; ')
        fam, _ = _keyword_detect(inner)
        if fam != "unknown":
            return (fam, "general")
        return ("dropdown_qualification", _sig(inner))
    m = re.search(r'\bTEXT:\s*(.+)', raw)
    if m:
        inner = m.group(1).rstrip('"; ')
        fam, _ = _keyword_detect(inner)
        if fam != "unknown":
            return (fam, "general")
        return ("text_fact", _sig(inner))
    if re.search(r'own-original-writing|own original writing', raw, re.I):
        return ("no_ai_unaided_writing", "general")
    return None

FAMILY_SIGS = [
    ("travel_commitment",
     _g({"travel"}, {"travel", "commitment"}, {"travel", "required"}, {"trip"}),
     {"interview", "traveler"},
     {}),
    ("office_frequency",
     _g({"office"}, {"on", "site"}, {"onsite"}, {"hybrid"}, {"in", "office"}, {"days", "week", "office"}),
     set(),
     {}),
    ("relocation_willingness",
     _g({"relocat"}, {"willing", "move"}),
     set(),
     {}),
    ("interview_recording_consent",
     _g({"record", "interview"}, {"recording", "interview"}, {"recorded", "interview"},
        {"metaview"}, {"bright", "hire"}, {"transcription"}, {"transcribe"},
        {"notetaker"}, {"record", "consent"}, {"recording", "consent"}),
     set(),
     {"metaview": {"metaview"}, "bright_hire": {"bright", "hire"}, "general": set()}),
    ("arbitration_agreement",
     _g({"arbitration"}, {"binding", "arbitration"}),
     set(),
     {}),
    ("ai_evaluation_consent",
     _g({"ai", "evaluat"}, {"artificial", "intelligence", "evaluat"}, {"consent", "ai", "candidacy"}),
     {"travel"},
     {}),
    ("no_ai_unaided_writing",
     _g({"no", "ai"}, {"unassisted"}, {"unaided"}, {"without", "ai"}, {"original", "writing", "unassisted"}),
     set(),
     {}),
    ("personally_completed_certification",
     _g({"personally", "completed"}, {"personally", "complete"}),
     set(),
     {}),
    ("truthfulness_certification",
     _g({"certify", "true"}, {"certify", "accurate"}, {"attest", "truth"}),
     set(),
     {}),
    # Generic legal attestation catch-all: AFTER the specific legal families
    # above (arbitration, no-AI, personally-completed, truthfulness) so their
    # precise matches win. Anchors on the attestation act itself.
    ("generic_attestation",
     _g({"attestation"}, {"applicant", "privacy"}, {"terms", "service", "agreement"}),
     set(),
     {}),
    ("recruiting_sms_consent",
     _g({"sms", "consent"}, {"text", "recruiting"}, {"sms", "recruiting"},
        {"text", "messages"}, {"texting"}),
     set(),
     {}),
    ("recruiting_whatsapp_consent",
     _g({"whatsapp"}),
     set(),
     {}),
    ("apply_by_email_authorization",
     _g({"apply", "email"}, {"email", "application", "send"}),
     set(),
     {}),
    ("how_heard_source",
     _g({"how", "hear"}, {"heard", "about"}),
     set(),
     {}),
    ("compensation_expectation",
     _g({"salary"}, {"compensation"}, {"pay", "expectation"}, {"desired", "salary"}),
     set(),
     {}),
    ("work_authorization",
     _g({"work", "authorization"}, {"authorized", "work"}, {"sponsorship", "visa"}, {"visa", "sponsorship"}),
     set(),
     {}),
    ("start_timeframe",
     _g({"start", "date"}, {"when", "start"}, {"availability", "start"}),
     set(),
     {}),
    ("education_degree",
     _g({"degree"}, {"bachelor"}, {"education", "requirement"}),
     set(),
     {}),
    ("self_assessment_skill",
     _g({"proficiency"}, {"skill", "level"}, {"rate", "yourself"}, {"self", "assessment"},
        {"self", "characterization"}, {"self", "descriptions"}, {"personality"}),
     set(),
     {}),
    ("address_fact",
     _g({"street", "address"}, {"mailing", "address"}, {"zip", "code"}, {"suite"}, {"apt"},
        {"apartment"}),
     set(),
     {}),
    ("profile_url_fact",
     _g({"github"}, {"portfolio", "url"}, {"personal", "website", "url"}),
     {"linkedin"},
     {}),
    ("manual_action_captcha",
     _g({"captcha"}, {"hcaptcha"}, {"recaptcha"}),
     set(),
     {}),
    ("manual_action_verification_code",
     _g({"verification", "code"}, {"email", "code"}, {"sms", "code"}, {"enter", "code"}),
     {"qr"},
     {}),
    ("manual_action_upload",
     _g({"upload", "resume", "manual"}, {"resume", "upload", "blocked"}, {"file", "picker"}),
     set(),
     {}),
    ("candidate_exercise",
     _g({"exercise", "submission"}, {"create", "thread"}, {"exercise", "thread"},
        {"exercise", "url"}),
     set(),
     {}),
    ("browser_tool_failure",
     _g({"page", "did", "not", "load"}, {"browser", "timed", "out"}, {"selector", "changed"}, {"ats", "inaccessible"}),
     set(),
     {}),
    ("unread_requirements",
     _g({"jd", "unread"}, {"full", "jd"}, {"requirements", "not", "read"}, {"degree", "unknown"}, {"travel", "unknown"}),
     set(),
     {}),
    ("employer_pattern_only",
     _g({"usually", "asks"}, {"pattern", "suggests"}, {"typically", "requires"}),
     set(),
     {}),
]

# Substring anchors for families whose keywords get mangled by tokenization
# (e.g. "relocat" won't match "relocation" as a token).
SUBSTR_ANCHORS = {
    "travel_commitment": ["travel"],
    "relocation_willingness": ["relocat"],
    "office_frequency": ["office", "onsite", "on-site", "hybrid"],
}


def detect_family(raw: str) -> tuple[str, str]:
    """Return (family, variant). 'unknown' family never auto-resolves and
    never collapses with other unknowns."""
    struct = structured_detect(raw or "")
    if struct:
        return struct
    return _keyword_detect(raw or "")


# ---------------------------------------------------------------------------
# Normalized blocker
# ---------------------------------------------------------------------------

@dataclass
class Blocker:
    lead_id: str
    company: str
    role: str
    raw_blocker: str
    normalized_family: str = "unknown"
    family_variant: str = "general"
    classification: str = "unprocessed"
    requires_trent: bool = False
    reason: str = ""
    agent_next_action: str = ""
    answer_bank_match: Optional[str] = None
    duplicate_of: Optional[str] = None
    verified_current_form: bool = False
    resolution: Optional[str] = None
    injection_flag: bool = False
    proof: dict = field(default_factory=dict)  # check_name -> {"survives": bool, "detail": str}


# ---------------------------------------------------------------------------
# Proof context
# ---------------------------------------------------------------------------

@dataclass
class ProofContext:
    answer_bank: dict = field(default_factory=dict)   # the live answer_bank.json
    preferences: list = field(default_factory=list)  # Preference objects
    candidate_records: dict = field(default_factory=dict)  # verified facts: key -> (value, source)
    employer: str = ""
    role_id: str = ""


# ---------------------------------------------------------------------------
# Answer-bank semantic match
# ---------------------------------------------------------------------------

def _bank_entries(bank: dict):
    answers = (bank or {}).get("answers", {})
    for key, val in answers.items():
        if isinstance(val, dict):
            patterns = val.get("question_patterns", []) or []
            answer = val.get("answer", "")
            prov = val.get("provenance", "")
            scope_emp = _key_employer(val)
        else:
            patterns = []
            answer = val
            prov = ""
            scope_emp = None
        yield key, answer, patterns, prov, scope_emp


# ---------------------------------------------------------------------------
# P-2026-09-16-g5-guardrail (Trent-approved 2026-09-16 ~12:55 PDT):
# cross-employer answer-key scoping + answer-shape compatibility.
# A bank key scoped to employer X must never resolve a blocker for
# employer Y (the Samsara-prior-employment -> Abnormal wrongful clearing),
# and an answer whose shape cannot satisfy the question's shape must not
# resolve it (boolean "No" must not answer "how many years" or
# "describe your experience"). Both refusals fail closed: the blocker
# keeps its "needs Trent" hypothesis instead of clearing on a wrong answer.
# ---------------------------------------------------------------------------

_SCOPE_ONLY_PAT = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .&'\-]*?)\s+ONLY\b")
_SCOPE_APPS_PAT = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .&'\-]*?)\s+applications?\b", re.I)


def _key_employer(val: dict):
    """Machine-readable employer scope from a bank entry's free-text
    scope fields. Returns the scoped employer name or None (unscoped)."""
    es = (val.get("employer_scope") or "").strip()
    m = _SCOPE_ONLY_PAT.match(es)
    if m:
        return m.group(1).strip()
    sc = (val.get("scope") or "").strip()
    m = _SCOPE_APPS_PAT.match(sc)
    if m:
        return m.group(1).strip()
    return None


def _norm_employer(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _employer_allows(scoped: str, employer: str) -> bool:
    """Strict provenance check: a scoped key resolves only for its own
    employer. Unknown/empty blocker employer -> refuse (fail closed)."""
    s, e = _norm_employer(scoped), _norm_employer(employer)
    if not s or not e:
        return False
    return e == s or e.startswith(s)


_BOOLEAN_Q_PAT = re.compile(
    r"^\s*(have|has|are|is|do|does|did|can|could|will|would|should|"
    r"acknowledge|confirm|attest|agree|certify)\b", re.I)
_YESNO_PAT = re.compile(r"\byes\s*/\s*no\b|\byes\s+or\s+no\b", re.I)
_NUMERIC_Q_PAT = re.compile(
    r"how many years|years of (experience|exp\b)|number of years", re.I)
_DESCRIBE_Q_PAT = re.compile(
    r"\b(describe|tell us about|explain|essay|what was your approach|"
    r"walk (us|me) through)\b", re.I)


def _question_shape(raw: str) -> str:
    t = raw or ""
    if _NUMERIC_Q_PAT.search(t):
        return "numeric_years"
    if _DESCRIBE_Q_PAT.search(t):
        return "describe"
    if _BOOLEAN_Q_PAT.search(t) or _YESNO_PAT.search(t):
        return "boolean"
    return "unknown"


def _answer_shape(answer) -> str:
    a = (answer or "").strip()
    if not a:
        return "unknown"
    if re.fullmatch(r"(?i)(yes|no|n/a|none)", a):
        return "boolean"
    if re.match(r"(?i)^(i acknowledge|i confirm|i attest|i agree|"
                r"i certify|proceed)\b", a):
        return "boolean"
    if re.search(r"\d+\s*(years|yrs)\b", a, re.I):
        return "numeric_years"
    if len(a) < 60 and re.fullmatch(r"(?i)[\w .,'\-/()]+", a):
        # short non-boolean, non-numeric answers (Indeed/Glassdoor,
        # "Next day", "Other") are choice-like, not free text
        return "choice"
    return "text"


# (question_shape, answer_shape) pairs that must never resolve.
# Anything involving "unknown" stays permissive (current behavior);
# only DETECTED incompatibility refuses.
_SHAPE_INCOMPATIBLE = {
    ("boolean", "numeric_years"),
    ("boolean", "describe"),
    ("boolean", "text"),
    ("numeric_years", "boolean"),
    ("numeric_years", "describe"),
    ("numeric_years", "text"),
    ("numeric_years", "choice"),
    ("describe", "boolean"),
    ("describe", "numeric_years"),
    ("describe", "choice"),
}


def _shapes_incompatible(qshape: str, ashape: str) -> bool:
    return (qshape, ashape) in _SHAPE_INCOMPATIBLE


def bank_match(raw: str, bank: dict, employer: str = ""):
    """Semantic match against answer_bank. Returns (key, answer, provenance)|None.

    Matches on question_patterns token overlap (>=0.6) or strong key-token hit.
    Never matches on inference: no pattern hit -> None.

    G5 guardrails (P-2026-09-16-g5-guardrail):
    - employer scoping: a key carrying employer_scope/scope for employer X
      is skipped unless `employer` matches X (fail closed on unknown employer).
    - shape compatibility: a detected question/answer shape mismatch refuses
      the match (e.g. boolean answer vs numeric-years question).
    """
    toks = _toks(raw)
    if not toks:
        return None
    best = None
    best_score = 0.0
    for key, answer, patterns, prov, scope_emp in _bank_entries(bank):
        if scope_emp and not _employer_allows(scope_emp, employer):
            continue
        for pat in patterns:
            ptoks = _toks(pat)
            if not ptoks:
                continue
            score = len(toks & ptoks) / len(ptoks)
            if score > best_score:
                best_score = score
                best = (key, answer, prov or "answer_bank.json")
        # key-derived fallback: key tokens substantially present
        ktoks = set(key.split("_")) - {"the", "a", "an", "of", "to", "in"}
        if ktoks and len(toks & ktoks) / len(ktoks) >= 0.8 and len(ktoks) >= 3:
            if 0.85 > best_score:
                best_score = 0.85
                best = (key, answer, prov or "answer_bank.json")
    if best and best_score >= 0.6:
        key, answer, _prov = best
        if _shapes_incompatible(_question_shape(raw), _answer_shape(answer)):
            return None
        return best
    return None


# ---------------------------------------------------------------------------
# The 10 proof checks. Each returns (survives: bool, detail: str).
# survives=True  -> the "needs Trent" hypothesis survives this check.
# survives=False -> positive proof the blocker does NOT need Trent (or is not
#                   a real blocker); the detail says why and where it routes.
# Ambiguity ALWAYS survives (fail closed).
# ---------------------------------------------------------------------------

OPTIONAL_PAT = re.compile(r"\b(optional|courtesy|if\s+you\s+like|not\s+required)\b", re.I)
REQUIRED_PAT = re.compile(r"\b(required|must\s+answer|mandatory|will\s+not\s+submit)\b", re.I)

def check_01_field_actually_required(blk: Blocker, ctx: ProofContext):
    raw = blk.raw_blocker or ""
    if OPTIONAL_PAT.search(raw) and not REQUIRED_PAT.search(raw):
        return False, "field marked optional in blocker text — not a blocker"
    return True, "required (or requirement ambiguous — fail closed)"


UNREAD_PAT = re.compile(
    r"\b(jd\s+unread|full\s+jd\s+not|requirements?\s+not\s+(yet\s+)?(read|checked)|"
    r"degree\s+(requirement\s+)?unknown|travel\s+(requirement\s+)?unknown|"
    r"office\s+(requirement\s+)?unknown|compensation\s+unknown|"
    r"eligibility\s+unknown|unverified\s+requirements?)\b", re.I)
PATTERN_ONLY_PAT = re.compile(
    r"\b(usually\s+asks?|typically\s+(asks?|requires?)|pattern\s+suggests?|"
    r"form\s+pattern|historical(ly)?\s+(asks?|pattern)|employer\s+usually)\b", re.I)
# Blocker text that is really an agent-side to-do, not a Trent question.
AGENT_TODO_PAT = re.compile(
    r"(needs_input:\s*\d+\s*form\s+questions|posting\s+url\s+unverifiable|"
    r"PARKED-UNVERIFIABLE|direct\s+page\s+unreachable|"
    r"live-browser\s+re-verify|no\s+tailored\s+packet\s+built)",
    re.I)
VERIFIED_FORM_PAT = re.compile(
    r"\b(form\s+intel|live\s+form|current\s+form\s+(read|inspected)|posting\s+verified\s+live|"
    r"ats\s+api|confirmed\s+live)\b", re.I)

def check_02_current_form_inspected(blk: Blocker, ctx: ProofContext):
    raw = blk.raw_blocker or ""
    # "no form intel" is the ABSENCE of verification — the presence pattern
    # must not misfire on negated contexts.
    negated = re.search(r"\bno\s+(form\s+intel|verified|live\s+form)\b", raw, re.I)
    if (blk.verified_current_form or VERIFIED_FORM_PAT.search(raw)) and not negated:
        return True, "current form/posting inspected"
    if UNREAD_PAT.search(raw):
        return False, "posting/form not inspected — agent_verification_required"
    if PATTERN_ONLY_PAT.search(raw):
        return False, "employer-pattern only, current form unconfirmed — form_verification_required"
    if AGENT_TODO_PAT.search(raw):
        if re.search(r"no\s+tailored\s+packet\s+built", raw, re.I):
            return False, "agent_action:build_packet — packet not built; agent builds on revival, not a Trent question"
        return False, "agent-side verification to-do — agent_verification_required"
    # No evidence either way: fail closed -> survives (assume uninspected? no —
    # assuming uninspected would misroute verified blockers. Ambiguity survives.)
    return True, "inspection state ambiguous — fail closed toward human"


# The blocker must actually be requesting the datum (not merely mentioning
# the word, e.g. "apply-by-email authorization" is not asking for the email).
REQUEST_DATUM_PAT = re.compile(
    r"\b(provide|enter|confirm|supply|list|what\s+is\s+your|your)\b", re.I)


def check_03_derivable_from_records(blk: Blocker, ctx: ProofContext):
    toks = _toks(blk.raw_blocker)
    raw = blk.raw_blocker or ""
    for key, (value, source) in (ctx.candidate_records or {}).items():
        ktoks = set(key.split("_"))
        if not ktoks:
            continue
        if len(toks & ktoks) / len(ktoks) < 0.75:
            continue
        # Single-token keys ("email", "phone") over-match contexts like
        # "apply-by-email authorization". Only resolve when the blocker is
        # actually requesting the datum itself.
        if len(ktoks) == 1 and not REQUEST_DATUM_PAT.search(raw):
            continue
        return False, f"derivable from verified record '{key}' (source: {source})"
    return True, "not derivable from verified candidate records"


def check_04_in_answer_bank(blk: Blocker, ctx: ProofContext):
    # P-2026-09-16-g5-guardrail: employer provenance flows into bank_match
    # so employer-scoped keys (Samsara-only, CodePath-only) never resolve
    # another employer's blocker.
    m = bank_match(blk.raw_blocker, ctx.answer_bank,
                   employer=ctx.employer or blk.company)
    if m:
        key, answer, prov = m
        blk.answer_bank_match = key
        return False, f"answer_bank hit '{key}' (provenance: {prov})"
    return True, "no answer_bank match"


def check_05_researchable(blk: Blocker, ctx: ProofContext):
    raw = blk.raw_blocker or ""
    if re.search(r"\b(see|check|per)\s+(the\s+)?posting\b", raw, re.I) and \
            re.search(r"\bdetails?\s+(in|on)\b", raw, re.I):
        return False, "answer located in posting — agent research action"
    return True, "not researchable with a concrete in-hand target"


def check_06_not_duplicated(blk: Blocker, ctx: ProofContext):
    # Dedup runs across the batch (dedup.py). This check is the per-blocker
    # hook: a blocker already marked duplicate_of fails the proof.
    if blk.duplicate_of:
        return False, f"duplicate of {blk.duplicate_of} — no new decision"
    return True, "no duplicate marking"


def check_07_standing_rule(blk: Blocker, ctx: ProofContext):
    fam = blk.normalized_family
    raw = blk.raw_blocker or ""
    if fam == "office_frequency":
        m = re.search(r"(\d+)\s*(?:days?)?\s*(?:per|a|/)?\s*week", raw, re.I) or \
            re.search(r"(\d+)\s*d/w", raw, re.I)
        if m:
            days = int(m.group(1))
            if days <= 3:
                return False, f"standing rule office.max_days_per_week=3 covers {days}d/wk — acceptable"
            return False, f"standing rule office.max_days_per_week=3 conflicts with {days}d/wk — policy_conflict, do not re-ask"
        # ambiguous frequency: D1 says undefined/unspecified = hybrid-acceptable
        if re.search(r"\b(undefined|unspecified|not\s+specified|unclear)\b", raw, re.I):
            return False, "D1: undefined/unspecified office frequency is hybrid-acceptable"
        return True, "office frequency ambiguous — survives"
    if fam == "travel_commitment":
        m = re.search(r"(\d+)\s*%", raw)
        if m and int(m.group(1)) <= 25:
            return False, f"standing rule travel <=25% covers {m.group(1)}% — acceptable"
        if m:
            return False, f"travel {m.group(1)}% exceeds 25% cap — policy_conflict, do not re-ask"
        if re.search(r"\b(unspecified|undefined|open.ended|as\s+needed)\b", raw, re.I):
            return True, "unspecified travel — consolidated Trent decision required"
        return True, "travel requirement ambiguous — survives"
    if fam == "relocation_willingness":
        pref, _ = prefs_mod.match_preference(ctx.preferences, "relocation.general",
                                             ctx.employer, ctx.role_id)
        if pref:
            return False, f"standing preference relocation.general='{pref.value}'"
        return True, "no relocation preference on file"
    if fam == "how_heard_source":
        pref, _ = prefs_mod.match_preference(ctx.preferences, "heard_about_source",
                                             ctx.employer, ctx.role_id)
        if pref:
            return False, f"standing preference heard_about='{pref.value}'"
        return True, "no source preference on file"
    if fam == "start_timeframe":
        pref, _ = prefs_mod.match_preference(ctx.preferences, "start_timeframe",
                                             ctx.employer, ctx.role_id)
        if pref:
            return False, f"standing preference start_timeframe='{pref.value}'"
        return True, "no start-timeframe preference on file"
    if fam == "apply_by_email_authorization":
        pref, _ = prefs_mod.match_preference(ctx.preferences,
                                             "email_application.agent_send_authorization",
                                             ctx.employer, ctx.role_id)
        if pref and "NOT AUTHORIZED" in pref.value:
            return False, ("standing rule: agents never send under Trent's identity — "
                           "policy_conflict: park for Trent, do not re-ask")
        return True, "no email-send authorization on file"
    if fam in prefs_mod.TRENT_ONLY_FAMILIES or fam.replace("_consent", "") in prefs_mod.TRENT_ONLY_FAMILIES:
        return True, "trent-only family — no standing auto-resolution"
    return True, "no standing rule for family"


TOOL_PAT = re.compile(
    r"\b(captcha|hcaptcha|recaptcha|verification\s+code|enter\s+(the\s+)?code|"
    r"sms\s+code|upload\s+(failed|blocked)|file\s+picker|identity\s+verification|"
    r"security\s+challenge|video\s+recording|loom)\b", re.I)
RETRY_PAT = re.compile(
    r"\b(page\s+did\s+not\s+load|timed?\s*out|selector|ats\s+inaccessible|"
    r"browser\s+(error|failed)|rate.?limit|temporarily\s+unavailable)\b", re.I)

def check_08_not_tool_problem(blk: Blocker, ctx: ProofContext):
    raw = blk.raw_blocker or ""
    if TOOL_PAT.search(raw):
        return False, "browser/tool/manual-takeover problem, not a candidate question"
    if RETRY_PAT.search(raw):
        return False, "automation retry problem, not a candidate question"
    return True, "not a tool/browser problem"


PERSONAL_PAT = re.compile(
    r"\b(i\s+certify|i\s+agree|consent|attest|authorize|my\s+own\s+words|"
    r"personally\s+completed?|unaided|unassisted|no\s+ai|signature|legal|"
    r"arbitration|waiver|background\s+check|choose|prefer|decision|motivation|"
    r"why\s+(this|us)|tell\s+us\s+about\s+yourself)\b", re.I)

def check_09_legitimately_requires_trent(blk: Blocker, ctx: ProofContext):
    fam = blk.normalized_family
    trent_fams = {
        "arbitration_agreement", "ai_evaluation_consent", "no_ai_unaided_writing",
        "personally_completed_certification", "truthfulness_certification",
        "generic_attestation", "free_text_trent_only", "dropdown_qualification",
        "candidate_exercise",
        "recruiting_sms_consent", "recruiting_whatsapp_consent",
        "interview_recording_consent", "compensation_expectation",
        "travel_commitment", "office_frequency",
    }
    if fam in trent_fams:
        return True, f"family '{fam}' is personal/legal/attestation by nature"
    if PERSONAL_PAT.search(blk.raw_blocker or ""):
        return True, "blocker language requires personal choice/attestation"
    # Deterministic factual fields do NOT require Trent once records exist;
    # without records they need a fact, which is still Trent's to supply.
    if fam in {"address_fact", "profile_url_fact", "self_assessment_skill",
               "education_degree", "work_authorization"}:
        return True, f"family '{fam}' needs Trent's factual input (no verified record)"
    if fam == "unknown":
        # Unknown family: we cannot prove agent-resolvability. Fail closed —
        # an unclassifiable blocker stays human, never auto-resolves.
        return True, "unknown family — cannot prove agent-resolvable, fail closed"
    return False, f"family '{fam}' is agent-resolvable in principle"


def check_10_asking_necessary(blk: Blocker, ctx: ProofContext):
    # If the lead itself is not progressing (dead/rejected/parked for other
    # reasons), asking Trent cannot progress the application.
    return True, "necessity judged at batch level (lead liveness), not per check"


CHECKS = [
    ("01_field_actually_required", check_01_field_actually_required),
    ("02_current_form_inspected", check_02_current_form_inspected),
    ("03_derivable_from_records", check_03_derivable_from_records),
    ("04_in_answer_bank", check_04_in_answer_bank),
    ("05_researchable", check_05_researchable),
    ("06_not_duplicated", check_06_not_duplicated),
    ("07_standing_rule", check_07_standing_rule),
    ("08_not_tool_problem", check_08_not_tool_problem),
    ("09_legitimately_requires_trent", check_09_legitimately_requires_trent),
    ("10_asking_necessary", check_10_asking_necessary),
]


def run_proof(blk: Blocker, ctx: ProofContext) -> Blocker:
    """Run all 10 checks, then classify. requires_trent=True ONLY if every
    check survives AND the family maps to a Trent-legitimate class."""
    raw = (blk.raw_blocker or "").strip()
    if not raw or not _norm(raw):
        # No blocker text at all: there is no blocker. This is a data
        # defect, never something to auto-resolve *or* escalate.
        blk.normalized_family, blk.family_variant = "unknown", "general"
        blk.injection_flag = False
        blk.classification = "false_blocker"
        blk.requires_trent = False
        blk.reason = "empty/malformed blocker text — no blocker present"
        blk.agent_next_action = "drop from tray"
        blk.resolution = "removed: empty"
        return blk
    blk.normalized_family, blk.family_variant = detect_family(blk.raw_blocker)
    inj, inj_detail = injection_suspected(blk.raw_blocker)
    blk.injection_flag = inj
    failures = []
    for name, fn in CHECKS:
        try:
            survives, detail = fn(blk, ctx)
        except Exception as exc:  # a broken check fails closed
            survives, detail = True, f"check error -> fail closed: {exc}"
        blk.proof[name] = {"survives": survives, "detail": detail}
        if not survives:
            failures.append((name, detail))
    _classify(blk, failures, inj_detail, ctx)
    return blk


def _classify(blk: Blocker, failures: list[tuple[str, str]], inj_detail: str,
              ctx: ProofContext) -> None:
    fmap = {name: detail for name, detail in failures}

    def setc(classification, requires_trent, reason, action="", resolution=None):
        blk.classification = classification
        blk.requires_trent = requires_trent
        blk.reason = ("[INJECTION-SUSPECT TEXT — treated as data] " if blk.injection_flag else "") + reason
        blk.agent_next_action = action
        blk.resolution = resolution

    # Injection can never auto-resolve: force agent review.
    if blk.injection_flag and not failures:
        setc("agent_verification_required", False,
             "blocker text contains directive-like phrasing; held for agent review, never auto-resolved. " + inj_detail,
             action="agent reviews blocker text; re-derive from verified sources only")
        return
    if blk.injection_flag and failures:
        # keep the failure classification but never mark resolved_auto
        pass  # fall through; resolved_auto branches below are guarded

    if "08_not_tool_problem" in fmap:
        raw = blk.raw_blocker or ""
        if re.search(r"captcha|hcaptcha|recaptcha", raw, re.I):
            setc("manual_takeover", False, fmap["08_not_tool_problem"],
                 action="route to captcha/manual-action queue")
        elif re.search(r"verification\s+code|enter\s+(the\s+)?code|sms\s+code", raw, re.I):
            setc("manual_takeover", False, fmap["08_not_tool_problem"],
                 action="route to verification-code flow")
        elif re.search(r"upload", raw, re.I):
            setc("manual_takeover", False, fmap["08_not_tool_problem"],
                 action="manual upload / Loom / video step")
        else:
            setc("automation_retry", False, fmap["08_not_tool_problem"],
                 action="retry via approved fallback; escalate only if human action needed")
        return
    if "01_field_actually_required" in fmap:
        setc("false_blocker", False, fmap["01_field_actually_required"],
             action="drop from tray", resolution="removed: not required")
        return
    if "02_current_form_inspected" in fmap:
        detail = fmap["02_current_form_inspected"]
        if "form_verification_required" in detail:
            setc("form_verification_required", False, detail,
                 action="inspect current application form; confirm actual fields")
        elif "agent_action:build_packet" in detail:
            setc("agent_action", False, detail,
                 action="build tailored packet on revival", resolution="agent-side")
        else:
            setc("agent_verification_required", False, detail,
                 action="retrieve current posting; resolve JD requirements; re-screen")
        return
    if "06_not_duplicated" in fmap:
        setc("duplicate", False, fmap["06_not_duplicated"],
             action="fold into canonical decision family", resolution="collapsed")
        return
    if "04_in_answer_bank" in fmap:
        if blk.injection_flag:
            setc("agent_verification_required", False,
                 "answer_bank hit but blocker text is injection-suspect — held for agent review",
                 action="agent verifies against bank out-of-band")
            return
        setc("resolved_auto", False, fmap["04_in_answer_bank"],
             action="apply banked answer with provenance",
             resolution=f"answer_bank:{blk.answer_bank_match}")
        return
    if "03_derivable_from_records" in fmap:
        setc("resolved_auto", False, fmap["03_derivable_from_records"],
             action="apply verified record value", resolution="candidate_record")
        return
    if "07_standing_rule" in fmap:
        detail = fmap["07_standing_rule"]
        dl = detail.lower()
        # Defense in depth: any standing-rule outcome that parks (not resolves)
        # must land in policy_conflict, never resolved_auto. The literal
        # "policy_conflict" marker is the primary signal; park-language is the
        # backstop so a future check-07 detail can't slip into auto-resolve.
        if ("policy_conflict" in dl or "park for trent" in dl
                or "do not re-ask" in dl or "do not ask" in dl):
            setc("policy_conflict", False, detail,
                 action="park per standing rule; do not re-ask Trent",
                 resolution="parked: standing policy")
        else:
            setc("resolved_auto", False, detail,
                 action="apply standing preference", resolution="standing_rule")
        return
    if "05_researchable" in fmap:
        setc("agent_verification_required", False, fmap["05_researchable"],
             action="agent researches posting/employer; re-run proof")
        return
    if "09_legitimately_requires_trent" in fmap:
        setc("resolved_auto", False, fmap["09_legitimately_requires_trent"],
             action="agent resolves deterministically", resolution="agent_resolvable")
        return
    if "10_asking_necessary" in fmap:
        setc("dropped_stalled", False, fmap["10_asking_necessary"],
             action="no ask; lead not progressing", resolution="dropped")
        return

    # All 10 survived -> genuine Trent decision. Map family to subtype.
    fam = blk.normalized_family
    if fam == "free_text_trent_only":
        # Normal free-text essays stay parked per standing skip-input directive
        # (essay drafting §10/§11 explicitly excluded from this build).
        setc("essay_trent_only", True,
             "all 10 proof checks survived; free-text answer needs Trent's own words (never drafted by agent)",
             action="present in compressed tap list")
        return
    if fam == "generic_attestation":
        setc("legal_attestation", True,
             "all 10 proof checks survived; attestation needs Trent's explicit word",
             action="present exact attestation text for Trent")
        return
    if fam == "dropdown_qualification":
        raw_l = (blk.raw_blocker or "").lower()
        sub = ("user_self_assessment"
               if re.search(r"\b\d+\s*-?\s*(yrs?|years?)\b", raw_l) or "proficiency" in raw_l
               else "user_fact")
        setc(sub, True,
             "all 10 proof checks survived; qualification dropdown needs Trent's factual input",
             action="present exact options + evidence")
        return
    if fam == "candidate_exercise":
        setc("human_authorship_required", True,
             "all 10 proof checks survived; employer-mandated exercise/thread needs Trent's own work",
             action="present exercise requirements; no agent authorship")
        return
    subtype = {
        "arbitration_agreement": "legal_attestation",
        "ai_evaluation_consent": "legal_attestation",
        "truthfulness_certification": "legal_attestation",
        "no_ai_unaided_writing": "human_authorship_required",
        "personally_completed_certification": "human_completion_required",
        "interview_recording_consent": "user_preference",
        "recruiting_sms_consent": "user_preference",
        "recruiting_whatsapp_consent": "user_preference",
        "travel_commitment": "user_preference",
        "office_frequency": "user_preference",
        "relocation_willingness": "user_preference",
        "compensation_expectation": "user_compensation",
        "how_heard_source": "user_fact",
        "address_fact": "user_fact",
        "profile_url_fact": "user_fact",
        "text_fact": "user_fact",        "start_timeframe": "user_fact",
        "work_authorization": "user_fact",
        "education_degree": "user_fact",
        "self_assessment_skill": "user_self_assessment",
    }.get(fam, "user_preference" if fam != "unknown" else "user_fact")
    # Normal free-text essays stay parked per standing skip-input directive
    # (essay drafting §10/§11 explicitly excluded from this build).
    if fam == "unknown" and re.search(r"\b(essay|why\s+(this|us)|tell\s+us|describe)\b",
                                      blk.raw_blocker or "", re.I):
        subtype = "essay_trent_only"
    setc(subtype, True, f"all 10 proof checks survived; family '{fam}' legitimately requires Trent",
         action="present in compressed tap list")
