"""Employer form-pattern memory for the Western Application Pipeline.

Per-employer form patterns learned from verified live encounters (browser tasks,
ATS API form-intel). Priors only — the live rendered form always rules; patterns
never authorize answering a question the live form does not show, and never
authorize inventing an answer.

Backed by employer_form_patterns.json in this directory. Set the
EMPLOYER_PATTERNS_FILE env var to point at a different file (used by tests).

Conventions (per ~/AGENTS.md lessons):
- Lookup is case-insensitive on employer; ATS must also match when supplied,
  because one employer's form differs across ATS platforms.
- record_pattern() merges: union of blockers/questions, evidence appended,
  encounters bumped. It NEVER silently replaces a high-confidence entry's
  blockers — new data is unioned in, confidence never downgraded.
- New entries start at confidence "medium". Promotion to "high" stays
  human-approved (edit the JSON).
"""

import copy
import json
import os
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
PATTERNS_FILE = os.environ.get(
    "EMPLOYER_PATTERNS_FILE",
    os.path.join(BASE, "employer_form_patterns.json"),
)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "is",
    "are", "you", "your", "our", "we", "with", "by", "at", "from", "as",
    "be", "it", "this", "that", "will", "now", "either", "per", "week",
}


def _norm(s):
    return (s or "").strip().lower()


def _load():
    with open(PATTERNS_FILE) as f:
        return json.load(f)


def _save(data):
    tmp = PATTERNS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, PATTERNS_FILE)


def _patterns(data):
    return data.get("patterns", [])


def get_patterns(employer, ats=None):
    """Return the stored pattern dict for an employer, or None.

    Employer match is case-insensitive. When ats is supplied (non-empty), the
    entry's ATS must also match (case-insensitive) — a pattern learned on one
    ATS never applies to the same employer's form on another ATS. Returns a
    deep copy so callers cannot mutate the store by accident.
    """
    emp = _norm(employer)
    if not emp:
        return None
    want_ats = _norm(ats)
    best = None
    for p in _patterns(_load()):
        if _norm(p.get("employer")) != emp:
            continue
        if want_ats and _norm(p.get("ats")) != want_ats:
            continue
        ev = p.get("evidence", {})
        if best is None or ev.get("encounters", 0) > best.get("evidence", {}).get("encounters", 0):
            best = p
    return copy.deepcopy(best)


def record_pattern(employer, ats, blockers, questions, evidence_source):
    """Add a new pattern or merge into the existing one. Returns the entry.

    Merge behavior (never a silent overwrite):
    - blockers/questions are unioned (order-preserving, de-duplicated);
      a high-confidence entry's existing blockers are always kept.
    - evidence: encounters bumped by 1, new source appended to the source log.
    - confidence: never downgraded; new entries start at "medium".
    """
    emp = (employer or "").strip()
    ats_n = (ats or "").strip().lower()
    if not emp:
        raise ValueError("employer is required")
    new_blockers = [b for b in (blockers or []) if b]
    new_questions = [q for q in (questions or []) if q]

    data = _load()
    entry = None
    for p in _patterns(data):
        if _norm(p.get("employer")) == _norm(emp) and _norm(p.get("ats")) == ats_n:
            entry = p
            break

    today = date.today().isoformat()
    if entry is None:
        entry = {
            "employer": emp,
            "ats": ats_n,
            "blockers": [],
            "questions": [],
            "evidence": {"date": today, "source": "", "encounters": 0},
            "confidence": "medium",
        }
        data.setdefault("patterns", []).append(entry)

    # Union — existing high-confidence blockers/questions are never dropped.
    for b in new_blockers:
        if b not in entry["blockers"]:
            entry["blockers"].append(b)
    for q in new_questions:
        if q not in entry["questions"]:
            entry["questions"].append(q)

    ev = entry.setdefault("evidence", {})
    ev["encounters"] = ev.get("encounters", 0) + 1
    if not ev.get("date"):
        ev["date"] = today
    prev_source = ev.get("source") or ""
    if evidence_source and evidence_source not in prev_source:
        ev["source"] = (prev_source + "; " + evidence_source).strip("; ")
    if entry.get("confidence") not in ("high", "medium", "low"):
        entry["confidence"] = "medium"
    # Confidence is never downgraded by a merge; promotion stays human-approved.

    _save(data)
    return copy.deepcopy(entry)


def _significant_tokens(text):
    # Split on anything non-alphanumeric so "3 days/week" yields days + week
    # rather than the phantom token "daysweek".
    toks = []
    for raw in __import__("re").split(r"[^a-z0-9]+", _norm(text)):
        if len(raw) >= 4 and raw not in _STOPWORDS:
            toks.append(raw)
    # de-dupe, keep order
    return list(dict.fromkeys(toks))


def match_blockers(brief_text, employer, ats=None):
    """Return the subset of known blockers for (employer, ats) that appear
    relevant to the given brief text.

    A blocker matches when at least 60% (minimum 2) of its significant tokens
    appear in the brief text. Used by the pre-launch screen: if a brief's form
    intel already contains the known office/essay/attestation question, the
    pattern confirms the block; if the brief has no intel (e.g. Ashby), no
    match is returned and the pattern is surfaced as a prior instead.
    """
    pats = get_patterns(employer, ats)
    if not pats:
        return []
    hay = _norm(brief_text or "")
    matched = []
    for blocker in pats.get("blockers", []):
        sig = _significant_tokens(blocker)
        if not sig:
            continue
        hits = sum(1 for t in sig if t in hay)
        need = max(2, -(-len(sig) * 6 // 10))  # ceil(60%), min 2
        if hits >= need:
            matched.append(blocker)
    return matched
