#!/usr/bin/env python3
"""field_question_protocol.py — Field-Requirement Protocol (FRP).

The one-time-cost loop: when a required application question cannot be
answered from the answer bank, classify it once, bank the answer when it is
verifiable, and never burn a browser attempt on a known-unknowable question
again. It trades one parked lead for permanent immunity to the same
question across every future lead.

Public edition: the classification taxonomy, the append-only question
backlog, and the verified-fact derivation rules ship here. The user's own
verified standing values live in their answer_bank.json (see
engines/answer_bank.example.json) — never in this file. Backlog stores,
hit counts, and bank backups live under configurable directories (env
overrides below) so no personal paths are hardcoded.

Mechanism (documented; implementation lives in sibling engines):
  The protocol assumes the application lane can (a) extract form
  requirements from launch packets, (b) consult a prescreen step that
  routes unmapped questions here, and (c) read employer application
  pages over HTTP. Board-detection for ATS-hosted postings is handled by
  the public ATS layer (engines/ats.py); ATS-specific board URL patterns
  are not redefined here.

Standing rules (the applicant's verified standing values; configured in
answer_bank.json — shown here as masked examples):
  1. When an application asks for a phone number, answer +1-555-0100.
     No other phone value is valid.
  2. When an application asks for a ZIP code, answer 00000.
     No other ZIP is valid.
  3. When an application asks about a security clearance, answer No
     unless a verified fact states otherwise.
  4. When an application asks about the applicant's own domain expertise
     or role history, answer with the applicant's own verified
     domain-tenure facts (e.g. "X years leading program operations for
     [Domain]"), never a generic title.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE)
from keel_paths import HOME, DATA  # noqa: E402

# Configurable so no personal paths are hardcoded. The user's real values
# live under these dirs; they are created on first use.
BACKLOG_DIR = os.environ.get("FIELD_PROTOCOL_DIR",
                             os.path.join(DATA, "employer_form_patterns"))
BACKLOG_PATH = os.path.join(BACKLOG_DIR, "field_question_backlog.jsonl")
HITCOUNTS_PATH = os.path.join(BACKLOG_DIR, "field_question_hitcounts.json")
QUEUE_DIR = os.environ.get("FIELD_PROTOCOL_QUEUE_DIR",
                           os.path.join(DATA, "queues"))
BANK_PATH = os.environ.get("FIELD_PROTOCOL_BANK_PATH",
                           os.path.join(DATA, "answer_bank.json"))
PDT = ZoneInfo("America/Los_Angeles")

OPEN_STATUSES = ("open", "open-derivable", "open-applicant-only")


def normalize_question(q, employer_synonyms=()):
    """Lowercase, strip employer names, collapse whitespace/punctuation.

    employer_synonyms: company-name variants to erase so the same question
    from different employers hashes to one norm.
    """
    s = (q or "").lower()
    for name in employer_synonyms or ():
        s = s.replace(name.lower(), "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[?.!,:;()\"'\[\]{}*/\\|~`<>=_+-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Question taxonomy
# ---------------------------------------------------------------------------
#
# Seven question classes the lane must know about. Examples reference the
# applicant's own verified facts (masked in this public edition).
#
# 1. PHONE: "+1-555-0100 (the applicant's verified number)". A phone question
#    is a standing-rule hit; answer (1), never invent another number.
# 2. ZIP: "00000 (the applicant's verified ZIP)". Standing-rule hit;
#    answer (2).
# 3. CLEARANCE: "Do you have an active security clearance?" Standing-rule
#    hit; answer No unless a verified fact states otherwise (3).
# 4. TENURE: "How many years leading program operations for [Domain]?"
#    Domain-tenure question — answer with the applicant's own verified
#    domain-tenure facts (4), banded to the form's options, never a
#    generic title.
# 5. APPLICANT-ONLY (open-ended / commitments / personal data): "Why do
#    you want to work here?", "Describe a time...", travel/relocation
#    commitments, street address, personal profile URLs. The applicant's
#    own words only — park, never invent.
# 6. POLICY (attestations / D1-style commitments): arbitration, no-AI
#    pledges, personally-completed attestations, travel/office/relocation
#    beyond the applicant's own policy caps. Always park.
# 7. DERIVABLE (verified fact): citizenship/work authorization,
#    sponsorship, degree status, the applicant's own verified tenure
#    figures. Answerable from verified facts without the applicant.

# Non-questions: operational notes a form-intel scan may surface that are
# not questions at all (audit-only, never instant-parks).
_NON_QUESTION_RES = [
    re.compile(r"\bverify[\s_-]?retry\b", re.I),
    re.compile(r"\bcaptcha\b", re.I),
]

# APPLICANT-ONLY patterns: the applicant's own words / commitments /
# personal data. When in doubt -> APPLICANT-ONLY.
_APPLICANT_ONLY_RES = [
    (re.compile(r"why (are you interested|do you want|would you like)|"
                r"what excites you most about working at|"
                r"what motivates you|"
                r"tell us something you want us to know|what are you proud of|"
                r"personal story|cover letter|"
                r"self-characterization|"
                r"dropdown.{0,60}self-characterization|"
                r"select one that feels|a/b self", re.I), "open-ended"),
    (re.compile(r"arbitrat|personally completed|privacy policy|"
                r"terms of service|truthfulness|under penalty|"
                r"waive.{0,20}(rights|claims)|"
                r"legal.{0,20}(attestation|commitment|agreement)|"
                r"no[-\s]?ai[\s-]?attestation|unaided|own original|"
                r"ai policy for interviewers|give.?directly", re.I),
     "attestation"),
    (re.compile(r"years.{0,25}(engineering|energy|clinical|legal|"
                r"member[-\s]?experience).{0,25}(experience|\?|$)|"
                r"exposure to|familiarity with|proficiency in|"
                r"fluency|self[- ]rating|"
                r"crm tools|salesforce|edi experience|saas or.{0,30}"
                r"experience|tech proficiency", re.I), "subjective"),
    (re.compile(r"street address|suite.{0,10}apt|address line|"
                r"github profile|personal.{0,15}website(?!.*linkedin)|"
                r"emergency contact|date of birth|\bssn\b|social security",
                re.I), "unverifiable"),
    (re.compile(r"willing to travel|comfortable.{0,25}travel|"
                r"travel.{0,40}(required|commitment)|"
                r"office.{0,30}days?.{0,10}per.?week|"
                r"days?.{0,10}per.?week.{0,25}office|"
                r"on[- ]site.{0,30}willingness|relocation willingness|"
                r"(4|four|5|five)\s*days?.{0,30}(in.?office|on.?site)|"
                r"full.?time.{0,30}(in.?office|on.?site)", re.I), "policy"),
    (re.compile(r"how did you hear|heard about|\bhow heard\b", re.I),
     "subjective"),
    (re.compile(r"base salary|salary expectation( figure)?", re.I),
     "subjective"),
]

# DERIVABLE: (compiled pattern, bank_key, answer, provenance).
# Answer is ONLY used when the bank does not already carry the key -- the
# bank is the source of truth; provenance is cited either way. The answers
# and facts below are MASKED EXAMPLES; the user's real values live in
# their own answer_bank.json.
_DERIVABLE_RES = [
    (re.compile(r"bachelor'?s|b\.?s\.? degree|4[-\s]?year degree|"
                r"undergraduate degree|college degree.{0,20}\?",
                re.I),
     "bachelors_degree",
     "No",
     "user-configured example: high-school diploma + verified technical "
     "completions -- never represented as a degree."),
    (re.compile(r"years.{0,25}(operations|consulting|program[-\s]?ops|"
                r"founder|ai.{0,15}automation).{0,25}(experience|\?|$)",
                re.I),
     "domain_tenure",
     "banded: select the band containing the applicant's verified domain "
     "tenure",
     "user-configured: verified domain tenure (e.g. ~7 years running the "
     "applicant's own consultancy, 2019-present), truthfully claimable for "
     "operations / program-ops / consulting / AI-automation domains only."),
    (re.compile(r"years.{0,25}ai.{0,25}(industry|experience)|"
                r"ai.{0,15}industry.{0,15}years", re.I),
     "ai_industry_tenure",
     "banded: select the band containing the applicant's verified AI "
     "industry tenure",
     "user-configured: verified numeric AI-industry tenure figure."),
    (re.compile(r"\bzip\b|postal code", re.I),
     "zip_code", "00000",
     "answer_bank.json standing fact: zip_code (the applicant's verified "
     "ZIP; masked in this public edition)."),
    (re.compile(r"work authorization.{0,20}(status|confirm)|"
                r"authorized to work", re.I),
     "us_work_auth", "Yes",
     "user-configured: verified work authorization (e.g. US citizen)."),
    (re.compile(r"sponsorship.{0,20}(needs|require|confirm)", re.I),
     "needs_sponsorship", "No",
     "user-configured: verified work authorization -- no sponsorship "
     "needed."),
    (re.compile(r"phone( number)?(?!.*verification code)", re.I),
     "phone", "+1-555-0100",
     "answer_bank.json standing fact (verified contact number; masked in "
     "this public edition)."),
    (re.compile(r"remote[- ]work intent|work intent", re.I),
     "work_intent", "Remote",
     "user-configured: remote-first preference."),
]


def _tenure_over_cap(question):
    """A tenure Yes/No or band question asking for MORE years than the
    applicant can truthfully claim: the honest answer is No. Cap is 12 for
    total/professional/career tenure, 7 for domain tenure (the
    user-configured tenure rule). Returns (bank_key, answer, provenance)
    or None."""
    m = re.search(r"(\d+)\s*\+?\s*(years?|yrs?)", question or "", re.I)
    if not m:
        return None
    asked = int(m.group(1))
    if asked < 8:
        return None  # within claimable range: band rules handle it
    q = question or ""
    if re.search(r"total|professional|career", q, re.I):
        cap, key = 12, "total_professional_years"
    else:
        cap, key = 7, "domain_tenure"
    if asked > cap:
        return (key, "No",
                "user-configured tenure rule: max truthful claim is "
                "%d years (%s); question asks %d." % (cap, key, asked))
    return None


def classify_question(question):
    """Classify one required question.

    Returns {"classification": "DERIVABLE"|"APPLICANT-ONLY", "category": str,
             "bank_key": str|None, "answer": str|None, "provenance": str|None}.

    DERIVABLE is returned ONLY when a verified fact answers the question.
    When in doubt -> APPLICANT-ONLY.
    """
    q = question or ""
    for rx in _NON_QUESTION_RES:
        if rx.search(q):
            return {"classification": "N/A", "category": "non-question",
                    "bank_key": None, "answer": None, "provenance": None,
                    "matched": rx.pattern[:80]}
    for rx, category in _APPLICANT_ONLY_RES:
        if rx.search(q):
            return {"classification": "APPLICANT-ONLY", "category": category,
                    "bank_key": None, "answer": None, "provenance": None,
                    "matched": rx.pattern[:80]}
    for rx, key, answer, prov in _DERIVABLE_RES:
        if rx.search(q):
            return {"classification": "DERIVABLE", "category": "verified-fact",
                    "bank_key": key, "answer": answer, "provenance": prov,
                    "matched": rx.pattern[:80]}
    over = _tenure_over_cap(q)
    if over:
        key, answer, prov = over
        return {"classification": "DERIVABLE", "category": "verified-fact",
                "bank_key": key, "answer": answer, "provenance": prov,
                "matched": "tenure-over-cap"}
    # Unknown shape: conservative default per the absolute-truthfulness
    # constraint -- when in doubt, APPLICANT-ONLY.
    return {"classification": "APPLICANT-ONLY", "category": "unclassified",
            "bank_key": None, "answer": None, "provenance": None,
            "matched": None}


# Stopwords for token-subset matching of banked question patterns.
_STOP = frozenset(
    "a an the of in on at to for with and or do does did is are was were "
    "you your have has had this that these those it its be been being "
    "what which who whom whose how when where why whether either neither "
    "any some such no not yes if then than so as by from into per about "
    "over under within without within i we they he she him her them me my "
    "our their his its our us our your yours".split())


def _tokens(s):
    return {t for t in normalize_question(s).split() if t and t not in _STOP}


def bank_pattern_lookup(question, bank):
    """Check answer_bank entries that carry question_patterns (the
    applicant's volunteered answers, e.g. a domain-experience detail).

    Token-subset match: every non-stopword token of a banked pattern must
    appear in the question's tokens. Returns (key, entry) or (None, None).
    """
    qtok = _tokens(question)
    answers = (bank or {}).get("answers", {})
    for key, val in answers.items():
        if not isinstance(val, dict):
            continue
        for pat in val.get("question_patterns") or []:
            ptok = _tokens(pat)
            if ptok and ptok <= qtok:
                return key, val
    return None, None

# ---------------------------------------------------------------------------
# Backlog store (append-only JSONL)
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(PDT).isoformat(timespec="seconds")


def load_backlog(path=None):
    path = path or BACKLOG_PATH
    entries = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    return entries


def append_backlog(entry, path=None):
    """Append one entry. entry must carry at least: norm, question,
    employer, role_id, classification, category, date, outcome."""
    path = path or BACKLOG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def log_question(question, employer, role_id, ats="", path=None):
    """Classify + append one unmapped question. Returns (entry, is_new)."""
    norm = normalize_question(question, [employer])
    existing = [e for e in load_backlog(path)
                if e.get("norm") == norm and e.get("status", "open") in OPEN_STATUSES]
    if existing:
        return existing[0], False
    cls = classify_question(question)
    if cls["classification"] == "N/A":
        status = "stale"  # non-question note: audit-only, never instant-parks
    elif cls["classification"] == "APPLICANT-ONLY":
        status = "open-applicant-only"
    else:
        status = "open-derivable"
    entry = {
        "norm": norm,
        "question": (question or "")[:300],
        "employer": employer or "",
        "ats": ats or "",
        "role_id": role_id or "",
        "classification": cls["classification"],
        "category": cls["category"],
        "bank_key": cls["bank_key"],
        "date": _now_iso()[:10],
        "outcome": "parked",
        "status": status,
    }
    append_backlog(entry, path)
    return entry, True


def known_applicant_only(norm, path=None):
    """True when norm is an OPEN APPLICANT-ONLY backlog entry -- parks instantly."""
    for e in load_backlog(path):
        if (e.get("norm") == norm
                and e.get("classification") == "APPLICANT-ONLY"
                and e.get("status", "open") in OPEN_STATUSES):
            return e
    return None


def _rewrite_backlog_entry(updated, path=None):
    """Rewrite one backlog entry in place (status transitions only)."""
    path = path or BACKLOG_PATH
    entries = load_backlog(path)
    for i, e in enumerate(entries):
        if e.get("norm") == updated.get("norm"):
            entries[i] = updated
            break
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def load_hitcounts(path=None):
    path = path or HITCOUNTS_PATH
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return {}
    return {}


def record_hit(norm, role_id, path=None):
    """Increment the instant-park hit counter for a known question."""
    path = path or HITCOUNTS_PATH
    counts = load_hitcounts(path)
    rec = counts.get(norm, {"hits": 0, "role_ids": []})
    rec["hits"] = rec.get("hits", 0) + 1
    if role_id and role_id not in rec.get("role_ids", []):
        rec["role_ids"].append(role_id)
    counts[norm] = rec
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(counts, f, indent=1)
    return rec


# ---------------------------------------------------------------------------
# Answer bank write-through (DERIVABLE graduation)
# ---------------------------------------------------------------------------

_bank_backed_up_today = set()


def _backup_bank_once():
    """Backup answer_bank.json to queue/_backup-<date>-frp/ once per day."""
    day = datetime.now(PDT).strftime("%Y%m%d")
    if day in _bank_backed_up_today:
        return
    bdir = os.path.join(QUEUE_DIR, f"_backup-{day}-frp")
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, "answer_bank.json")
    if not os.path.exists(dst) and os.path.exists(BANK_PATH):
        shutil.copy(BANK_PATH, dst)
    _bank_backed_up_today.add(day)


def ensure_bank_key(bank_key, answer, provenance, bank=None, bank_path=None,
                    dry_run=False):
    """Graduate a DERIVABLE answer into answer_bank.json with provenance.
    Returns (changed: bool, note: str). Additive only -- never overwrites
    an existing key's value."""
    bank_path = bank_path or BANK_PATH
    own = False
    if bank is None:
        own = True
        bank = json.load(open(bank_path)) if os.path.exists(bank_path) else {}
    answers = bank.setdefault("answers", {})
    prov = bank.setdefault("_provenance", {})
    if bank_key in answers:
        return False, f"key {bank_key} already in bank"
    if dry_run:
        return True, f"would add {bank_key} (dry-run)"
    _backup_bank_once()
    answers[bank_key] = answer
    prov[bank_key] = {"source": provenance, "added": _now_iso()[:10],
                      "via": "field-requirement-protocol"}
    meta = bank.setdefault("_meta", {})
    meta["updated"] = _now_iso()[:10]
    with open(bank_path, "w") as f:
        json.dump(bank, f, indent=1)
    return True, f"added {bank_key}"


# ---------------------------------------------------------------------------
# Prescreen integration point
# ---------------------------------------------------------------------------

def handle_unmapped_question(question, employer, role_id, ats="", bank=None,
                             bank_path=None, dry_run=False):
    """The single integration point for the prescreen's PARK path.

    Returns {"action": "instant-park"|"park"|"answered", "reason": str|None,
             "entry": dict, "derived": dict|None}.

    - instant-park: norm already in backlog as APPLICANT-ONLY -> park now,
      no browser attempt, hit counter incremented.
    - park: new APPLICANT-ONLY question -> backlog append, park with
      conventional wording.
    - answered: DERIVABLE or bank-pattern hit -> bank write-through
      (unless dry_run), do NOT park; derived answer returned for the packet.
    """
    # 0. Banked applicant answers (question_patterns) answer instantly --
    #    an applicant-volunteered answer beats classification every time.
    bkey, bval = bank_pattern_lookup(question, bank)
    if bkey:
        norm = normalize_question(question, [employer])
        known = [e for e in load_backlog()
                 if e.get("norm") == norm
                 and e.get("status", "open") in OPEN_STATUSES]
        entry = known[0] if known else None
        if entry and not dry_run:
            entry["status"] = "banked"
            entry["outcome"] = "answered"
            entry["closed_date"] = _now_iso()[:10]
            entry["closed_answer"] = str(bval.get("answer", ""))[:300]
            entry["bank_key"] = bkey
            _rewrite_backlog_entry(entry)
        return {
            "action": "answered",
            "reason": None,
            "entry": entry or {"norm": norm, "question": question[:300],
                               "classification": "BANKED",
                               "category": "applicant-volunteered",
                               "bank_key": bkey},
            "derived": {"bank_key": bkey,
                        "answer": bval.get("answer"),
                        "provenance": bval.get("provenance"),
                        "bank_changed": False,
                        "bank_note": "answered from banked applicant answer"},
        }
    norm = normalize_question(question, [employer])
    known = known_applicant_only(norm)
    if known:
        if not dry_run:
            record_hit(norm, role_id)
        return {
            "action": "instant-park",
            "reason": (
                f"Known APPLICANT-ONLY question (backlog {known.get('date')}, "
                f"first snagged on {known.get('role_id')}) -- instant park "
                f"at prescreen, no browser attempt burned "
                f"({known.get('category')}): \"{(question or '')[:120]}\""
            ),
            "entry": known,
            "derived": None,
        }
    entry, _is_new = log_question(question, employer, role_id, ats)
    if entry["classification"] == "N/A":
        # Non-question operational note, not a form question: nothing to
        # park on and nothing to answer.
        return {"action": "skip", "reason": None, "entry": entry,
                "derived": None}
    if entry["classification"] == "DERIVABLE":
        changed, note = ensure_bank_key(
            entry["bank_key"], classify_question(question)["answer"],
            classify_question(question)["provenance"],
            bank=bank, bank_path=bank_path, dry_run=dry_run)
        return {
            "action": "answered",
            "reason": None,
            "entry": entry,
            "derived": {"bank_key": entry["bank_key"],
                        "answer": classify_question(question)["answer"],
                        "provenance": classify_question(question)["provenance"],
                        "bank_changed": changed, "bank_note": note},
        }
    return {
        "action": "park",
        "reason": (
            f"Required question needs the applicant's own input "
            f"({entry['category']}) -- logged to field-question backlog, "
            f"must not invent: \"{(question or '')[:120]}\""
        ),
        "entry": entry,
        "derived": None,
    }


def close_question(question, answer, bank_key=None, provenance="",
                   bank_path=None, backlog_path=None, dry_run=False):
    """Answer-capture loop: the applicant volunteered an answer (any chat).

    - Writes/updates answer_bank.json (backup first) with provenance.
    - Marks every matching OPEN backlog entry answered/banked.
    - Returns parked role_ids whose backlog blockers are ALL now closed
      (revival candidates for canonical verify_retry; the caller routes
      them -- this function writes no queue state).
    """
    norm = normalize_question(question)
    bank_path = bank_path or BANK_PATH
    backlog_path = backlog_path or BACKLOG_PATH
    if not dry_run:
        _backup_bank_once()
    bank = json.load(open(bank_path)) if os.path.exists(bank_path) else {}
    answers = bank.setdefault("answers", {})
    prov = bank.setdefault("_provenance", {})
    if bank_key:
        if not dry_run:
            existing = answers.get(bank_key)
            if isinstance(existing, dict):
                # Dict-shaped applicant-answer entry (question_patterns).
                pats = set(existing.get("question_patterns") or [])
                pats.add(normalize_question(question))
                existing["question_patterns"] = sorted(pats)
                existing["answer"] = answer
                if provenance:
                    existing["provenance"] = provenance
            else:
                answers[bank_key] = {
                    "question_patterns": [normalize_question(question)],
                    "answer": answer,
                    "provenance": provenance or "applicant (volunteered)",
                    "added": _now_iso(),
                }
            prov[bank_key] = {"source": provenance or "applicant (volunteered)",
                              "added": _now_iso()[:10],
                              "via": "field-requirement-protocol close"}
            bank.setdefault("_meta", {})["updated"] = _now_iso()[:10]
            with open(bank_path, "w") as f:
                json.dump(bank, f, indent=1)
    entries = load_backlog(backlog_path)
    matched = [e for e in entries if e.get("norm") == norm
               and e.get("status", "open") in OPEN_STATUSES]
    for e in matched:
        e["status"] = "banked" if bank_key else "answered"
        e["outcome"] = "answered"
        e["closed_date"] = _now_iso()[:10]
        e["closed_answer"] = answer[:300]
    if matched and not dry_run:
        with open(backlog_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    # Revival candidates: parked leads whose ONLY unresolved backlog
    # questions are now closed. (Queue scan is read-only.)
    candidates = _revival_candidates(matched_norms={norm})
    return {"matched_entries": len(matched), "bank_key": bank_key,
            "revival_candidates": candidates, "dry_run": dry_run}


def _revival_candidates(matched_norms):
    """Read-only scan: needs_input leads whose every backlog-tracked
    blocker norm is now closed."""
    out = []
    ni_path = os.path.join(QUEUE_DIR, "needs_input-queue.json")
    if not os.path.exists(ni_path):
        return out
    try:
        leads = json.load(open(ni_path))
    except Exception:
        return out
    if isinstance(leads, dict):
        leads = leads.get("leads", [])
    open_norms = {e.get("norm") for e in load_backlog()
                  if e.get("status", "open") in OPEN_STATUSES}
    for lead in leads:
        blockers = []
        for r in (lead.get("unresolved") or []):
            m = re.search(r'"([^"]{5,200})"', str(r))
            if m:
                blockers.append(normalize_question(m.group(1)))
        if (blockers
                and any(b in matched_norms for b in blockers)
                and all(b not in open_norms for b in blockers)):
            out.append(lead.get("role_id"))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_classify(args):
    cls = classify_question(args.question)
    print(json.dumps({"question": args.question,
                      "norm": normalize_question(args.question),
                      **cls}, indent=1))


def cmd_close(args):
    res = close_question(args.question, args.answer, bank_key=args.bank_key,
                         provenance=args.provenance, dry_run=args.dry_run)
    print(json.dumps(res, indent=1))


def cmd_report(_args):
    entries = load_backlog()
    counts = {}
    for e in entries:
        k = (e.get("classification"), e.get("category"), e.get("status"))
        counts[k] = counts.get(k, 0) + 1
    hits = load_hitcounts()
    total_hits = sum(h.get("hits", 0) for h in hits.values())
    print(json.dumps({
        "backlog_entries": len(entries),
        "by_class_cat_status": {str(k): v for k, v in counts.items()},
        "instant_park_hits": total_hits,
        "questions_with_hits": len(hits),
    }, indent=1))


def main():
    ap = argparse.ArgumentParser(description="Field-Requirement Protocol CLI.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("classify", help="Classify one question.")
    p.add_argument("question")
    p.set_defaults(fn=cmd_classify)
    p = sub.add_parser("close",
                       help="Capture the applicant's volunteered answer.")
    p.add_argument("--question", required=True)
    p.add_argument("--answer", required=True)
    p.add_argument("--bank-key", default=None)
    p.add_argument("--provenance", default="")
    p.add_argument("--dry-run", action="store_true", default=False)
    p.set_defaults(fn=cmd_close)
    p = sub.add_parser("report", help="Backlog stats.")
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
