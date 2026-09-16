#!/usr/bin/env python3
"""input_broker.py — broker applicant-blocked input gaps to an external model for drafting.

Standing rule: "An external model may draft gaps; Keel verifies."
Standing skip directive: never prompt the applicant; drafts surface only
via their tap-list, on their terms. Nothing here unparks a lead, clears a
blocker, or submits anything — drafts await the applicant's verify.

Privacy: this module operates on the user's OWN data (queue entries, answer
bank) with the user's OWN external model key. Drafts go to the user's own
external model and back; nothing is sent anywhere else.

Pipeline:
  --classify        dry-run: classify every needs_input blocker, print counts
  --pack            generate the external-model question pack (no queue writes)
  --auto-fill       store AUTO_FILLABLE drafts from answer_bank (queue write, backup)
  --ingest FILE     parse + verify external-model answers, store drafts (queue write, backup)
  --tapline         update the tap-list "input broker" line
  --run             classify + pack + auto-fill + tapline

Categories per (entry, blocker):
  GPT_DRAFTABLE  research/factual drafts the external model can write from verified materials
  APPLICANT_ONLY the applicant's own words/attestations/commitments — parked, untouched, silent
  LANE_RETRY     verification codes/captchas — the lane's retry logic, not input
  AUTO_FILLABLE  answerable now from answer_bank.json verified values

Mechanism: "external drafts; local verifies". The pack carries verified
facts + DO-NOT-INVENT refusal grounds to the external model; every draft
that comes back passes verify_draft() locally before it is stored
(travel/commitment language, degree claims, over-cap tenure claims,
unverified URLs, compensation figures are all refused; the attestation
check reuses this module's own ATTEST_PAT). A draft with honest [VERIFY]
flags is not a refusal — the flags route to the applicant. Nothing is
invented, ever.

Public/private boundary: the production version of this module shares its
hard gates with the private submission layer (api_submit.BLOCKER_PATS —
"import, never duplicate"). The private layer is not published (see
SPLIT.md), so this edition screens draft-side attestation language with
the module's own ATTEST_PAT and documents the shared-gates mechanism as
a private-layer concern.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE)
from keel_paths import HOME, DATA  # noqa: E402

NEEDS_INPUT_Q = os.environ.get(
    "INPUT_BROKER_QUEUE_PATH",
    os.path.join(DATA, "queues", "needs_input-queue.json"))
ANSWER_BANK_P = os.environ.get(
    "INPUT_BROKER_BANK_PATH",
    os.path.join(DATA, "answer_bank.json"))
BROKER_DIR = os.path.join(HOME, "hidden_files", "input-broker")
TAPLIST_DIR = os.environ.get(
    "INPUT_BROKER_TAPLIST_DIR", os.path.join(DATA, "tap-list"))
PDT = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------- patterns

# LANE_RETRY — the lane's retry logic owns these, not the input broker.
LANE_RETRY_PAT = re.compile(
    r"captcha|verification code|email.{0,12}code|one.?time (pass )?code|"
    r"\botp\b|\b2fa\b|human verification|prove you.?re human|"
    r"security code (for|to).{0,20}(application|verif)", re.I)

# APPLICANT_ONLY hard stops (checked before anything else draftable).
NO_AI_PAT = re.compile(
    r"no[-\s]?ai[\s-]?attestation|no-ai-attestation|unassisted writing|"
    r"unaided|own original|ai policy for interviewers|give.?directly", re.I)
ATTEST_PAT = re.compile(
    r"arbitrat|personally completed|privacy policy|terms of service|"
    r"truthfulness|under penalty|waive.{0,20}(rights|claims)|"
    r"legal.{0,20}(attestation|commitment|agreement)", re.I)
TRAVEL_PAT = re.compile(r"\btravel\b|relocat", re.I)
RECORDING_PAT = re.compile(r"recording|brighthire|consent to ai notetaker", re.I)
PERSONAL_DATA_PAT = re.compile(
    r"street address|suite/apt|github profile|github url", re.I)
APPLY_EMAIL_PAT = re.compile(r"apply.?by.?email", re.I)
MANUAL_TAKEOVER_PAT = re.compile(r"manual takeover|tool-blocked", re.I)
EXERCISE_PAT = re.compile(
    r"exercise.{0,20}(url|submission)|create a .{0,20}thread|"
    r"paste the .{0,20}url", re.I)
PERSONAL_VOICE_PAT = re.compile(
    r"tell us something you want us to know|what motivates you|"
    r"proud of|personality|self-characterization|your pick|"
    r"applicant'?s (own )?choice|opt-in|opt in|a/b self|select one that feels",
    re.I)
SELFCHAR_DROPDOWN_PAT = re.compile(
    r"dropdown.{0,60}(self-characterization|applicant'?s pick)|"
    r"\d+\s*(?:-|–|to)?\s*\d*\s*yrs?.{0,40}experience\?|"
    r"self-assessment dropdowns", re.I)

# AUTO_FILLABLE — tight, high-confidence mappings to verified answer_bank keys.
AUTOFILL_RULES = [
    (re.compile(r"how did you hear", re.I), "heard_about"),
    (re.compile(r"current (or previous )?(employer|company)\b", re.I),
     "current_employer"),
    (re.compile(r"current (or previous )?job title", re.I), "current_job_title"),
    (re.compile(r"where are you (currently )?based|from where do you intend "
                r"to work|current location|where do you live", re.I), "location"),
    (re.compile(r"when can you start|start.?date|start timeframe", re.I),
     "start_timeframe"),
    (re.compile(r"authori[sz]ed to work|work authori", re.I), "us_work_auth"),
    (re.compile(r"sponsorship", re.I), "needs_sponsorship"),
    (re.compile(r"willing to relocate", re.I), "relocation_willingness"),
    (re.compile(r"\bzip\b|\bpostal\b", re.I), "zip_code"),
]

WHY_PAT = re.compile(
    r"why (are you interested|do you want|would you like)|"
    r"what excites you most about working at", re.I)


def extract_question(blocker):
    """Pull the form's exact question text out of a blocker string.

    Returns (question, truncated). truncated=True when the queue note itself
    cut the prompt off — the pack then flags it for form-intel recovery.
    """
    t = (blocker or "").strip()
    m = re.search(r'"([^"]{12,500})"', t)
    if m:
        return m.group(1).strip(), False
    # single-quoted: closing quote must not be an apostrophe inside a word
    m = re.search(r"'(.{12,500}?)'(?![A-Za-z])", t)
    if m:
        return m.group(1).strip(), False
    # Q(x) 'truncated prompt...  (queue note cut it off)
    m = re.match(r"^Q\([a-c]\)\s*'(.+?)\s*$", t)
    if m:
        return m.group(1).rstrip(" '\""), True
    # "Required essay: ...", "ESSAY: ...", "Required free-text: ..." prefixes
    m2 = re.match(r"^(?:(?:required\s+)?(?:essay|free-?text|personal story|text))"
                  r"\s*:\s*(.+?)\s*(?:—.*)?$", t, re.I)
    if m2:
        return m2.group(1).strip().strip("'\""), False
    # Unquoted question leading a parenthetical: "Why Linear? (required ...)"
    m3 = re.match(r"^(.+?\?)\s*\(", t)
    if m3:
        return m3.group(1).strip(), False
    # Opening quote with no close — the note was truncated mid-prompt
    if t.count('"') % 2 == 1:
        return t[t.find('"') + 1:].strip(), True
    return "", False


def display_identity(entry):
    """company/title, falling back to parsing role_key 'Co | Title | ...'."""
    co, ti = entry.get("company"), entry.get("title")
    if co and ti:
        return co, ti
    rk = (entry.get("role_key") or "").split("|")
    co = co or (rk[0].strip() if len(rk) > 0 and rk[0].strip() else "?")
    ti = ti or (rk[1].strip() if len(rk) > 1 and rk[1].strip() else "?")
    return co, ti


# Employers under a no-AI / unaided-work attestation regime: their essays are
# NEVER draftable, even when the blocker text doesn't say so explicitly.
NO_AI_EMPLOYERS_PAT = re.compile(r"givedirectly|anthropic|perplexity", re.I)
WRITING_HINT_PAT = re.compile(r"essay|free-?text|writing|words|story", re.I)


def classify_blocker(blocker):
    """Return (category, reason) for one unresolved blocker string."""
    t = blocker or ""
    if LANE_RETRY_PAT.search(t):
        return ("LANE_RETRY", "verification/captcha — lane retry logic owns it")
    if NO_AI_PAT.search(t):
        return ("APPLICANT_ONLY",
                "no-AI/unaided-work attestation regime — the applicant's own words only")
    if ATTEST_PAT.search(t):
        return ("APPLICANT_ONLY", "attestation/legal commitment — the applicant's word only")
    if TRAVEL_PAT.search(t):
        return ("APPLICANT_ONLY", "travel/relocation commitment — the applicant's word only")
    if RECORDING_PAT.search(t):
        return ("APPLICANT_ONLY", "recording/interview consent — the applicant's choice only")
    if PERSONAL_DATA_PAT.search(t):
        return ("APPLICANT_ONLY",
                "personal data not in verified facts — cannot invent")
    if APPLY_EMAIL_PAT.search(t):
        return ("APPLICANT_ONLY", "apply-by-email — the applicant sends or authorizes")
    if MANUAL_TAKEOVER_PAT.search(t):
        return ("APPLICANT_ONLY", "needs the applicant's manual browser takeover")
    if EXERCISE_PAT.search(t):
        return ("APPLICANT_ONLY", "exercise artifact the applicant must produce themselves")
    if PERSONAL_VOICE_PAT.search(t):
        return ("APPLICANT_ONLY",
                "personal voice/story/choice — cannot be drafted without inventing")
    if SELFCHAR_DROPDOWN_PAT.search(t):
        return ("APPLICANT_ONLY",
                "self-characterization dropdown — the applicant's pick only")
    for pat, key in AUTOFILL_RULES:
        if pat.search(t):
            return ("AUTO_FILLABLE", f"verified answer_bank value: {key}")
    q, _trunc = extract_question(t)
    if q:
        # A real question we can hand to the drafting model with grounding.
        return ("GPT_DRAFTABLE",
                "draftable from verified materials + public research; "
                "[VERIFY] flags where facts are missing")
    if (re.search(r"free-?text|essay|screener", t, re.I) and "(" in t
            and len(t) > 40):
        # Topic hints on record (e.g. Fleetio's four screener topics) but the
        # exact prompts aren't — the model drafts from verified facts and flags gaps.
        return ("GPT_DRAFTABLE",
                "topics on record; exact prompts need form intel — draft from "
                "verified facts with [VERIFY] flags")
    return ("APPLICANT_ONLY", "no extractable question — fail closed")


def classify_entry(entry):
    """Classify every unresolved blocker on an entry."""
    no_ai_regime = bool(NO_AI_EMPLOYERS_PAT.search(
        f"{entry.get('company','')} {entry.get('role_id','')}"))
    out = []
    for i, b in enumerate(entry.get("unresolved") or []):
        cat, reason = classify_blocker(b)
        # No-AI regime override: essays under an unaided-work attestation
        # stay applicant-only even when the blocker text doesn't say so.
        if (no_ai_regime and cat == "GPT_DRAFTABLE"
                and WRITING_HINT_PAT.search(b or "")):
            cat = "APPLICANT_ONLY"
            reason = ("no-AI attestation regime (employer) — the applicant's "
                      "own words only")
        qtext, qtrunc = extract_question(b)
        needs_exact = (cat == "GPT_DRAFTABLE" and not qtext
                       and bool(re.search(r"free-?text|essay|screener", b or "",
                                          re.I)))
        if qtrunc:
            reason = (reason + " [queue note truncated the prompt — exact "
                      "wording needs form intel]")
        out.append({"qid": f"Q{i+1}", "blocker": b,
                    "question": qtext or (b if needs_exact else ""),
                    "category": cat, "reason": reason,
                    "needs_exact_prompts": needs_exact or qtrunc})
    return out


# ---------------------------------------------------------------- queue io

def load_queue(path):
    with open(path) as f:
        q = json.load(f)
    return (q if isinstance(q, list) else q.get("entries", q.get("queue", []))), q


def backup_queue(path):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(HOME, "queue", f"_backup-{ts[:8]}-input-broker")
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(path, os.path.join(dest, os.path.basename(path)))
    return dest


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def pdt_now():
    return datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")


def load_bank():
    if not os.path.exists(ANSWER_BANK_P):
        raise FileNotFoundError(
            f"answer bank not found at {ANSWER_BANK_P} — copy "
            "engines/answer_bank.example.json to that path and fill in "
            "your own verified values first (your bank is personal data; "
            "it is never committed).")
    with open(ANSWER_BANK_P) as f:
        return json.load(f)

# ---------------------------------------------------------------- pack

VERIFIED_FACTS_MD = """\
## Verified facts about the applicant (EXAMPLE TEMPLATE — replace with your own)

Ground truth — never contradict, never embellish. Keel verifies everything
before it touches an application.

- Name: Applicant Name. Founder of Example Consulting, 2019–present (~7 years running it).
- 5 years in the AI industry; 12 years total paid professional experience.
- Work authorization: authorized to work in the US (example standing fact). No security clearance unless verified.
- Based in: City, ST (example). Remote-first; open to hybrid/on-site per the applicant's own location/travel policy; relocation per policy.
- Education: high-school diploma + 400+ verified technical completions (example). NEVER represent these as a degree unless verified as one.
- How they operate: written proposals, named metrics, decision logs, parallel workstreams coordinated through briefs and a shared state log.
- Voice: concise, emotionally precise, direct, reflective, grounded. No generic motivation ("I'm passionate about..."), no invented quotes, no engagement bait, no corporate theater.
"""

DO_NOT_INVENT_MD = """\
## DO NOT INVENT (refusal grounds — a draft violating any of these is rejected)

- No employers, job titles, budgets, metrics, team sizes, or dates beyond the verified facts above.
- No degrees, credentials, or certifications beyond the verified facts above.
- No tenure claims beyond the verified facts above (e.g. N years industry, M years total professional experience).
- No URLs except the applicant's own verified URLs (replace these placeholders): linkedin.com/in/your-profile, github.com/your-org/your-repo.
- No salary/compensation figures. No legal attestations, no travel/relocation commitments, no consent statements.
- If a draft needs a fact not provided here, write `[VERIFY: describe exactly what is missing]` and keep going — never fill the gap with invention.
"""

PACK_HEADER_MD = """\
# Input broker pack — {ts}

> You are drafting job-application answers for the applicant. Keel verifies everything before it touches an application.
>
> **Non-negotiable rules:**
> 1. Never invent (see DO NOT INVENT below). Missing fact → `[VERIFY: ...]` flag, keep going.
> 2. Respect word caps exactly as stated per question.
> 3. Draft ONLY from the verified facts + your public research on the company/role. For "why this company" questions, research the company's real public materials (mission, product, recent work) and connect to the applicant's verified background — no flattery, no generic motivation.
> 4. Output format per item — copy this exactly:
>
> ### <role_id> — <Qn>
> **A:** <draft text>
> **Words:** <n>
> **Flags:** none  |  [VERIFY: ...]
>
> 5. Off limits (do not draft, do not ask about): anything under a no-AI / unaided-work attestation, legal attestations, travel commitments, street address, personal profile URLs, verification codes.
"""


def build_pack(entries_with_class, bank):
    """entries_with_class: list of (entry, [classified blockers])."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parts = [PACK_HEADER_MD.format(ts=ts), VERIFIED_FACTS_MD, DO_NOT_INVENT_MD,
             "## Gaps — draft each one\n"]
    n = 0
    for entry, classified in entries_with_class:
        draftable = [c for c in classified if c["category"] == "GPT_DRAFTABLE"]
        if not draftable:
            continue
        n += 1
        co, ti = display_identity(entry)
        parts.append(
            f"### Gap {n} — {co} — {ti} "
            f"(fit {entry.get('fit_score','?')}, `{entry.get('role_id')}`)\n")
        # word-cap hint
        for c in draftable:
            q = c["question"] or c["blocker"]
            cap = ""
            m = re.search(r"less than (\d+) words|under (\d+) words|(\d+)\s*words?\s*max",
                          c["blocker"], re.I)
            if m:
                cap = f" — HARD CAP: {next(g for g in m.groups() if g)} words"
            why = " [why-company research draft]" if WHY_PAT.search(q) else ""
            parts.append(f"**{c['qid']}**{why}{cap}: \"{q}\"\n")
            ends_clean = q.rstrip().endswith((".", "?", "!"))
            if c.get("needs_exact_prompts") or not ends_clean:
                parts.append("- Note: exact form prompt not on record (or truncated in our "
                             "notes) — topics above are from the queue. Draft from verified "
                             "facts only; use `[VERIFY]` wherever the exact prompt wording "
                             "is needed.\n")
        parts.append("")
    parts.append("---\n*Drafts come back through verification. `[VERIFY]` flags are "
                 "expected and honest — they become the applicant's tap-list items.*\n")
    return ts, "\n".join(parts), n


def write_pack(entries_with_class, bank):
    os.makedirs(BROKER_DIR, exist_ok=True)
    ts, text, n = build_pack(entries_with_class, bank)
    path = os.path.join(BROKER_DIR, f"pack-{ts}.md")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path, n


# ---------------------------------------------------------------- ingest

# Verified URLs the applicant may claim (example placeholders — the real
# list is personal data). Anything else in a draft is refused.
VERIFIED_URLS = (
    "linkedin.com/in/your-profile",
    "github.com/your-org/your-repo",
)
DEGREE_PAT = re.compile(
    r"\bmba\b|"
    r"\b(bachelor'?s?|master'?s?|ph\.?d\.?|b\.?s\.?|m\.?s\.?)\b.{0,25}"
    r"(degree|graduated|university|college|school|program)", re.I)
# Draft-side travel/commitment language — stricter than the form-label gate:
# a draft must never itself volunteer a travel commitment.
DRAFT_TRAVEL_PAT = re.compile(r"\btravel\b|relocat", re.I)
TENURE_PAT = re.compile(r"(\d+)\s*(?:\+?\s*)years?", re.I)
DOLLAR_PAT = re.compile(r"\$\s?\d", re.I)
WORDCAP_PAT = re.compile(
    r"less than (\d+) words|under (\d+) words|(\d+)\s*words?\s*max", re.I)


def verify_draft(question, draft, flags):
    """Return (ok: bool, reasons: [str]). Refuse invented/unverifiable content.

    Local verifier (the "local verifies" half of "external drafts; local
    verifies"). The draft-side attestation check reuses this module's own
    ATTEST_PAT — the production build shares hard gates with the private
    submission layer (api_submit.BLOCKER_PATS), which is not published
    (see SPLIT.md); this edition keeps the check local and documents that
    boundary instead.
    """
    reasons = []
    t = draft or ""
    if not t.strip():
        return False, ["empty draft"]
    if ATTEST_PAT.search(t):
        reasons.append("draft contains attestation/commitment language")
    if DRAFT_TRAVEL_PAT.search(t):
        reasons.append("draft contains travel/relocation commitment language")
    if DEGREE_PAT.search(t):
        reasons.append("draft claims a degree — not in verified facts")
    for m in TENURE_PAT.finditer(t):
        if int(m.group(1)) > 12:
            reasons.append(f"draft claims {m.group(1)} years — exceeds verified "
                           f"12-year total")
            break
    for full, bare in re.findall(
            r"https?://([^\s)]+)|((?:[\w-]+\.)+(?:com|io|dev|ai|org)(?:/[^\s)]*)?)",
            t):
        cand = (full or bare or "").lower()
        cand = re.sub(r"^www\.", "", cand)
        if cand and not any(cand.startswith(v) for v in VERIFIED_URLS):
            reasons.append(f"draft cites unverified URL: {cand[:60]}")
            break
    if DOLLAR_PAT.search(t):
        reasons.append("draft contains a compensation figure — not permitted")
    if flags:
        # [VERIFY] flags are honest, not refusal grounds — they route to
        # the applicant.
        pass
    return (len(reasons) == 0), reasons


def parse_pack_answers(text):
    """Parse the external model's answers back into (role_id, qid, draft).

    Expected format (enforced in the pack):
        ### <role_id> — <Qn>
        **A:** <draft text>
        **Words:** <n>
        **Flags:** none | [VERIFY: ...]
    """
    out = []
    cur = None
    hdr = re.compile(r"^###\s+([A-Z0-9]+(?:-[A-Z0-9]+)*)\s*[—-]\s*(Q\d+)\s*$",
                     re.I)
    for line in text.splitlines():
        m = hdr.match(line.strip())
        if m:
            if cur:
                out.append(cur)
            cur = {"role_id": m.group(1).upper(), "qid": m.group(2).upper(),
                   "a_lines": [], "flags": []}
            continue
        if cur is None:
            continue
        a = re.match(r"^\*\*A:\*\*\s*(.*)$", line.strip())
        if a:
            cur["a_lines"].append(a.group(1))
            continue
        f = re.match(r"^\*\*Flags:\*\*\s*(.*)$", line.strip(), re.I)
        if f:
            v = f.group(1).strip()
            cur["flags"] = [] if v.lower() == "none" else [v]
            continue
        if re.match(r"^\*\*Words:\*\*", line.strip(), re.I):
            continue
        if cur["a_lines"] and line.strip() and not line.strip().startswith(
                "**"):
            cur["a_lines"].append(line.strip())
    if cur:
        out.append(cur)
    return out


def ingest_answers(path, classified_by_role):
    """classified_by_role: {role_id: [classified blockers]}. Returns stats."""
    with open(path) as f:
        answers = parse_pack_answers(f.read())
    entries, _raw = load_queue(NEEDS_INPUT_Q)
    by_role = {}
    for e in entries:
        rk = e.get("role_id")
        if rk:
            by_role[str(rk).upper()] = e
    stats = {"stored": 0, "refused": [], "unknown_role": [], "no_match": []}
    for a in answers:
        role = by_role.get(a["role_id"])
        if not role:
            stats["unknown_role"].append(a["role_id"])
            continue
        clist = classified_by_role.get(role.get("role_id"), [])
        target = next((c for c in clist
                       if c["qid"] == a["qid"] and
                       c["category"] == "GPT_DRAFTABLE"), None)
        if not target:
            stats["no_match"].append(f"{a['role_id']} {a['qid']}")
            continue
        draft = " ".join(a["a_lines"]).strip()
        ok, reasons = verify_draft(target["question"] or target["blocker"],
                                   draft, a["flags"])
        entry_drafts = role.setdefault("input_broker_drafts", {})
        qentry = entry_drafts.setdefault(a["qid"], {})
        qentry.update({
            "blocker": target["blocker"],
            "question": target["question"],
            "draft": draft if ok else "",
            "flags": a["flags"],
            "verdict": "keel-verified" if ok else "refused",
            "refusal_reasons": reasons,
            "provenance": ("input-broker ingest — external draft, "
                           "keel-verified") if ok else ("input-broker ingest — "
                           "refused at verify"),
            "updated": pdt_now(),
        })
        if ok:
            stats["stored"] += 1
            # Mark the blocker as awaiting the applicant's verify, so the
            # queue shows a draft exists without unparking the lead.
            if target["blocker"] in (role.get("unresolved") or []):
                role.setdefault("input_broker_drafts", {})[a["qid"]][
                    "status"] = "awaiting applicant verify"
        else:
            stats["refused"].append(f"{a['role_id']} {a['qid']}: "
                                    + "; ".join(reasons))
    atomic_write_json(NEEDS_INPUT_Q, entries)
    return stats


def auto_fill(entries, bank):
    """Store AUTO_FILLABLE drafts from verified answer_bank values (no model)."""
    bank_values = {k: v for k, v in (bank.get("answers", {}) if isinstance(
        bank, dict) else {}).items()} if isinstance(bank, dict) else {}
    stats = {"filled": 0}
    for entry, classified in entries:
        entry_drafts = entry.setdefault("input_broker_drafts", {})
        for c in classified:
            if c["category"] != "AUTO_FILLABLE":
                continue
            key = c["reason"].rsplit("answer_bank value: ", 1)[-1].strip()
            val = bank_values.get(key)
            if val is None:
                continue
            draft = str(val).strip()
            ok, reasons = verify_draft(c["question"] or c["blocker"], draft,
                                       [])
            if not ok:
                continue
            qentry = entry_drafts.setdefault(c["qid"], {})
            qentry.update({
                "blocker": c["blocker"],
                "question": c["question"],
                "draft": draft,
                "flags": [],
                "verdict": "keel-verified",
                "refusal_reasons": [],
                "provenance": "input-broker auto-fill (answer_bank verified)",
                "status": "awaiting applicant verify",
                "updated": pdt_now(),
            })
            stats["filled"] += 1
    atomic_write_json(NEEDS_INPUT_Q, entries)
    return stats


def update_tapline(entries_with_class):
    """Update the tap-list 'input broker' line. Creates dir if missing."""
    os.makedirs(TAPLIST_DIR, exist_ok=True)
    path = os.path.join(TAPLIST_DIR, "input-broker.md")
    counts = {"GPT_DRAFTABLE": 0, "APPLICANT_ONLY": 0, "AUTO_FILLABLE": 0,
              "LANE_RETRY": 0}
    leads = []
    for entry, classified in entries_with_class:
        leads.append(entry)
        for c in classified:
            counts[c["category"]] = counts.get(c["category"], 0) + 1
    by_role = {}
    for entry, classified in entries_with_class:
        rid = entry.get("role_id")
        if rid:
            by_role[str(rid).upper()] = classified
    lines = [
        f"# Input broker — {pdt_now()}",
        "",
        f"**{counts['GPT_DRAFTABLE']}** gaps draftable (external model + local verify), "
        f"**{counts['AUTO_FILLABLE']}** auto-filled from your verified answer bank, "
        f"**{counts['APPLICANT_ONLY']}** awaiting only your words, "
        f"**{counts['LANE_RETRY']}** in the lane's retry logic.",
        "",
        "Reply with the item to approve or correct — nothing is applied until you say so.",
        "",
    ]
    for entry in leads:
        co, ti = display_identity(entry)
        rid = entry.get("role_id", "?")
        clist = by_role.get(str(rid).upper(), [])
        draftable = [c for c in clist if c["category"] == "GPT_DRAFTABLE"]
        if not draftable:
            continue
        lines.append(f"## {co} — {ti} `{rid}`")
        for c in draftable:
            qentry = (entry.get("input_broker_drafts") or {}).get(c["qid"])
            if qentry and qentry.get("draft"):
                verdict = qentry.get("verdict", "")
                status = qentry.get("status", "")
                lines.append(f"- **{c['qid']}** [{verdict}; {status}]")
                lines.append(f"  Q: {c['question'] or c['blocker']}")
                lines.append(f"  A: {qentry['draft']}")
                if qentry.get("flags"):
                    lines.append(f"  Flags: {' | '.join(qentry['flags'])}")
            else:
                lines.append(f"- **{c['qid']}** [awaiting draft]")
                lines.append(f"  Q: {c['question'] or c['blocker']}")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


# ---------------------------------------------------------------- commands

def cmd_classify(args):
    entries, _raw = load_queue(NEEDS_INPUT_Q)
    classified = [(e, classify_entry(e)) for e in entries]
    from collections import Counter
    counts = Counter()
    cat_by_entry = []
    for entry, clist in classified:
        for c in clist:
            counts[c["category"]] += 1
        cat_by_entry.append((entry, [c["category"] for c in clist]))
    print(f"needs_input entries: {len(entries)}")
    for cat in ("GPT_DRAFTABLE", "AUTO_FILLABLE", "APPLICANT_ONLY",
                "LANE_RETRY"):
        print(f"  {cat}: {counts.get(cat, 0)}")
    if args.verbose:
        for entry, cats in cat_by_entry:
            co, ti = display_identity(entry)
            print(f"\n{co} — {ti} `{entry.get('role_id')}`: {cats}")
            entry2 = next(e for e, _c in classified
                          if e.get("role_id") == entry.get("role_id"))
            for c in classify_entry(entry2):
                print(f"    {c['qid']} {c['category']}: {c['reason']}")
                print(f"      Q: {(c['question'] or c['blocker'])[:120]}")
    return classified


def main():
    ap = argparse.ArgumentParser(
        description="Broker applicant-blocked input gaps to an external "
                    "model for drafting; Keel verifies every draft.")
    ap.add_argument("--classify", action="store_true",
                    help="dry-run: classify every blocker, print counts")
    ap.add_argument("--pack", action="store_true",
                    help="generate the external-model question pack "
                         "(no queue writes)")
    ap.add_argument("--auto-fill", action="store_true",
                    help="store AUTO_FILLABLE drafts from answer_bank "
                         "(queue write, backup)")
    ap.add_argument("--ingest", metavar="FILE",
                    help="parse + verify external-model answers, store drafts "
                         "(queue write, backup)")
    ap.add_argument("--tapline", action="store_true",
                    help="update the tap-list 'input broker' line")
    ap.add_argument("--run", action="store_true",
                    help="classify + pack + auto-fill + tapline")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    bank = None
    classified = cmd_classify(args)

    do_run = args.run or not any(
        [args.classify, args.pack, args.auto_fill, args.ingest,
         args.tapline])

    if args.pack or do_run:
        bank = load_bank()
        path, n = write_pack(classified, bank)
        print(f"pack written: {path} ({n} leads with draftable gaps)")

    if args.auto_fill or do_run:
        bank = bank or load_bank()
        backup_queue(NEEDS_INPUT_Q)
        st = auto_fill(classified, bank)
        print(f"auto-filled: {st['filled']}")

    if args.ingest:
        entries, _raw = load_queue(NEEDS_INPUT_Q)
        by_role = {e.get("role_id"): classify_entry(e) for e in entries}
        backup_queue(NEEDS_INPUT_Q)
        st = ingest_answers(args.ingest, by_role)
        print(f"stored: {st['stored']}")
        for r in st["refused"]:
            print(f"  REFUSED {r}")
        for r in st["unknown_role"]:
            print(f"  unknown role: {r}")
        for r in st["no_match"]:
            print(f"  no matching draftable blocker: {r}")

    if args.tapline or do_run:
        path = update_tapline(classified)
        print(f"tap-list updated: {path}")


if __name__ == "__main__":
    main()
