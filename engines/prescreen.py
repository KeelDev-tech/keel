"""Pre-launch packet screening gate for the Keel.

Scans launch packets (data/launch-packets/<role_id>.json) BEFORE a
browser task is spawned and PARKs anything that needs the applicant's own words/answers
instead of burning a browser run on it.

Lesson encoded (AGENTS.md 2026-09-14): parking MUST use only the conventional
fields the classifier reads -- `unresolved`, `status_reason` (overwritten, never
appended to stale text), `gate_note`, `queue_notes` -- and `unresolved` strings
must contain words verify_retry's GENUINE_PAT recognizes
(essay/wording/travel/attest/reference/applicant/salary/degree/location/hybrid/
onsite/relocation/captcha/account/login), otherwise a stale "posting confirmed
live ... promoted READY" status_reason makes verify_retry resurrect the lead.

Usage:
    python3 prescreen.py                 # dry-run: scan all packets, print verdicts
    python3 prescreen.py --dry-run       # same (default)
    python3 prescreen.py --live          # apply parking for PARK verdicts
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
from keel_paths import HOME as PIPELINE  # noqa: E402
QUEUE_DIR = os.path.join(PIPELINE, "data", "queues")
PACKET_DIR = os.path.join(PIPELINE, "data", "launch-packets")


def _patterns_file():
    for cand in (os.path.join(BASE, "employer_form_patterns.json"),
                 os.path.join(BASE, "employer_form_patterns.example.json")):
        if os.path.exists(cand):
            return cand
    return os.path.join(BASE, "employer_form_patterns.example.json")


PATTERNS_FILE = _patterns_file()

sys.path.insert(0, BASE)
import log_event  # noqa: E402

PDT = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------------------
# Blocker pattern sets (case-insensitive), applied to the packet's FORM INTEL
# section only -- never to the whole brief, because the brief template's GATES
# section mentions travel/attest as stop-conditions and would false-positive.
# ---------------------------------------------------------------------------

OFFICE_RELOCATION_RE = [
    re.compile(r"work (from|in) (our|the) .*office", re.I),
    re.compile(r"in person.{0,60}office", re.I),
    re.compile(r"(3|three)\s*(times|x)\s*(a|per)\s*week", re.I),
    re.compile(r"\bdays?\s*(a|per)\s*week\b.{0,40}office|office.{0,40}\bdays?\s*(a|per)\s*week\b", re.I),
    re.compile(r"relocat\w*", re.I),
    re.compile(r"hybrid.{0,50}in-?office|in-?office.{0,50}arrangement", re.I),
    re.compile(r"willing to (travel|relocate)", re.I),
    re.compile(r"travel.{0,30}(required|commitment|%\s*of)", re.I),
    # 4/5-day and full-time on-site exceed the EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK
    # policy ceiling (see the operator-set constants above). Hyphen-aware:
    # "4-days-per-week" must catch too.
    re.compile(r"(4|four|5|five)\s*[-\u2013]?\s*days?.{0,30}(in.?office|on.?site|office)", re.I),
    re.compile(r"full.?time.{0,30}(in.?office|on.?site)", re.I),
]

ESSAY_RE = [
    re.compile(r"\bessay\b", re.I),
    re.compile(r"own original,?\s*unassisted", re.I),
    re.compile(r"unassisted writing", re.I),
    re.compile(r"in your own words", re.I),
    re.compile(r"tell us something", re.I),
    re.compile(r"(thread|exercise|work sample|portfolio).{0,30}\burl\b|\burl\b.{0,30}(thread|exercise|work sample)", re.I),
]

ATTEST_RE = [
    re.compile(r"arbitration", re.I),
    re.compile(r"personally completed", re.I),
    re.compile(r"\bno-?ai\b", re.I),
    re.compile(r"unaided", re.I),
    re.compile(r"without.{0,25}ai.{0,25}(assistance|help|aid)", re.I),
    re.compile(r"\battest\w*", re.I),
]

# Hard-stop attestation classes — never pre-authorized, always park:
# no-AI / unaided-work / personally-completed attestations would be FALSE
# if an agent answered them, and travel/office/relocation commitments are
# gated by the location/travel policy below.
NO_AI_ATTEST_RE = [
    re.compile(r"personally completed", re.I),
    re.compile(r"\bno-?ai\b", re.I),
    re.compile(r"unaided", re.I),
    re.compile(r"without.{0,25}ai.{0,25}(assistance|help|aid)", re.I),
    re.compile(r"\bno[\s-]+(?:ai|artificial intelligence)\b", re.I),
    re.compile(r"\bwithout\s+(?:any\s+)?(?:automated\s+)?assistance\b", re.I),
    re.compile(r"\bindependently\b", re.I),
    re.compile(r"\b(?:did|have|has)\s+not\s+use(?:d)?\b.{0,40}\b(?:ai|artificial intelligence)\b", re.I),
]


def _no_ai_hard_stop(text):
    """No-AI/unaided attestations never inherit ordinary truthfulness consent."""
    return any(pattern.search(str(text or "")) for pattern in NO_AI_ATTEST_RE)

# Pre-authorized attestation scope. These answer-bank keys correspond to
# STANDARD application-form legal attestations (arbitration agreement,
# background-check consent, at-will acknowledgment,
# information-truthfulness attestation, data-privacy consent) that the
# operator has pre-approved as standing answers. The screen_packet
# ATTEST_RE loop skips the park ONLY when the attestation text maps to one
# of these banked keys; the keys must exist in the operator's own
# answer_bank.json — the shipped answer_bank.example.json carries none, so
# nothing is pre-authorized out of the box.
PREAUTHORIZED_ATTEST_KEYS = frozenset({
    "arbitration_agreement",
    "background_check_consent",
    "at_will_acknowledgment",
    "information_truthfulness_attestation",
    "data_privacy_consent",
})

# ---------------------------------------------------------------------------
# Configurable location/travel policy (OPERATOR-SET — sanitized from the
# private pipeline's personal policy).
#
# The private pipeline encodes one operator's personal relocation/travel
# caps here. This repo ships EXAMPLE values only: set your own before
# running, matching what you would honestly answer on a form. The screen
# uses these constants everywhere a commitment question is adjudicated —
# never invents a % cap or a day count for you.
# ---------------------------------------------------------------------------
EXAMPLE_TRAVEL_CAP_PCT = 25          # example: max travel % you accept
EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK = 3  # example: max in-office days/week

# Operator-configured state-exclusion patterns for posting_eligibility_screen.
# Example (uncomment and adapt): a posting that explicitly excludes hires in
# your state is a permanent eligibility block, not the applicant's call.
#   re.compile(r"\bcalifornia\b.{0,40}(residents?|hires?) not eligible", re.I),
STATE_EXCLUSION_RES = [
]

# ---------------------------------------------------------------------------
# Required free-text question -> answer_bank mapping.
# A question is mappable only if it clearly matches one of these rules.
# WHEN IN DOUBT, PARK -- never invent.
# ---------------------------------------------------------------------------

BANK_MAP = [
    (re.compile(r"preferred first name|first name", re.I), "first_name"),
    (re.compile(r"last name|family name|surname", re.I), "last_name"),
    (re.compile(r"\bemail\b|e-mail", re.I), "email"),
    (re.compile(r"phone|mobile|telephone", re.I), "phone"),
    (re.compile(r"\bcountry\b", re.I), "phone_country"),
    (re.compile(r"location|city|where do you (currently )?reside|current location", re.I), "location"),
    (re.compile(r"linkedin", re.I), "linkedin"),
    (re.compile(r"school|university|college|\bdegree\b|discipline|major|field of study", re.I), "education"),
    (re.compile(r"start date|available to start|when can you start|start timeframe|notice period", re.I), "start_timeframe"),
    (re.compile(r"salary|compensation|pay expectation|desired pay|expected pay|total comp", re.I), "compensation_expectation"),
    (re.compile(r"how did you hear|heard about|referral|referred by|\bsource\b", re.I), "heard_about"),
    (re.compile(r"authorized to work|work authorization|legally authorized|eligible to work", re.I), "us_work_auth"),
    (re.compile(r"sponsorship|visa", re.I), "needs_sponsorship"),
    (re.compile(r"citizen", re.I), "us_citizen_resident"),
    (re.compile(r"non-?compete|non-?solicit|confidentiality agreement", re.I), "noncompete"),
    (re.compile(r"clearance", re.I), "clearance"),
    (re.compile(r"years.{0,15}\bai\b|\bai\b.{0,15}years", re.I), "ai_industry_years"),
    (re.compile(r"years of (professional )?experience|total.*years|how many years", re.I), "total_professional_years"),
    (re.compile(r"team size|direct reports|how many (people|reports)", re.I), "team_size"),
    (re.compile(r"years.{0,20}(manag|lead)", re.I), "years_leading"),
    # Generic domain-tenure rule (operator-configured example): domain-tenure
    # questions in domains you can truthfully claim map to the bank's
    # domain-tenure key. Domain-specific tenure outside your domains does not
    # match -- stays unmapped and parks per the tenure rule. Replace the
    # example domains below with your own.
    (re.compile(r"years.{0,25}(operations|consulting|program[-\s]?ops|founder).{0,25}experience", re.I), "domain_tenure"),
    # Full-autopilot attestation keys (operator pre-authorization, see
    # PREAUTHORIZED_ATTEST_KEYS above). Placed last -- specific
    # identity/domain rules above always take precedence.
    (re.compile(r"arbitration", re.I), "arbitration_agreement"),
    (re.compile(r"background check|consumer report|investigative report", re.I), "background_check_consent"),
    (re.compile(r"\bat[\s-]?will\b", re.I), "at_will_acknowledgment"),
    (re.compile(r"(certify|attest).{0,40}information.{0,40}(true|accurate|complete)|information.{0,40}(is|are).{0,20}(true|accurate|complete)", re.I), "information_truthfulness_attestation"),
    (re.compile(r"privacy (policy|notice)|data (privacy|processing).{0,20}consent|consent.{0,20}(data|privacy).{0,20}(processing|use)", re.I), "data_privacy_consent"),
]

# Fallback employer form patterns. A JSON file `employer_form_patterns.json`
# (or the shipped employer_form_patterns.example.json) next to this script
# overrides/extends these. Record only patterns from YOUR verified encounters.
DEFAULT_EMPLOYER_PATTERNS = {
    "examplecorp": [
        "work-authorization attestation with no decline option",
    ],
}


def load_employer_patterns():
    """Employer -> [blocker strings]. Reads employer_form_patterns.json when
    present (supports both the rich {"patterns": [{employer, blockers, ...}]}
    schema and the simple {employer: [blockers]} form); falls back to the
    built-in defaults below."""
    patterns = dict(DEFAULT_EMPLOYER_PATTERNS)
    if os.path.exists(PATTERNS_FILE):
        try:
            data = json.load(open(PATTERNS_FILE))
            entries = data.get("patterns") if isinstance(data, dict) else None
            if isinstance(entries, list):
                for e in entries:
                    emp = (e.get("employer") or "").lower()
                    blockers = e.get("blockers") or []
                    if emp and blockers:
                        patterns[emp] = list(blockers)
            elif isinstance(data, dict):
                for k, v in data.items():
                    if k.startswith("_"):
                        continue
                    if isinstance(v, list):
                        patterns[k.lower()] = v
        except Exception as e:
            print(f"  prescreen: could not load {PATTERNS_FILE}: {e}", file=sys.stderr)
    return patterns


# ---------------------------------------------------------------------------
# Location/travel policy adjudication (operator-set; see the EXAMPLE_*
# constants above). Answers only when the question carries explicit,
# parseable commitments; anything ambiguous parks — never invent a cap.
# ---------------------------------------------------------------------------

def policy_travel_ok(question):
    """Travel gate: Yes only when the question explicitly states travel at or
    below EXAMPLE_TRAVEL_CAP_PCT on a defined schedule.

    Returns (True, note) or (False, park_reason). Generic, regular,
    unspecified, or open-ended travel parks — never invent a commitment."""
    percents = [int(x) for x in re.findall(r"(\d+)\s*%", question or "")]
    if percents:
        asked = max(percents)
        if asked <= EXAMPLE_TRAVEL_CAP_PCT:
            return True, "asked %d%% within policy cap %d%%" % (
                asked, EXAMPLE_TRAVEL_CAP_PCT)
        return False, "asked %d%% exceeds policy cap %d%%" % (
            asked, EXAMPLE_TRAVEL_CAP_PCT)
    return False, "no parseable travel percentage — park (never invent a cap)"


_DAY_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _office_days_in_span(text):
    """Day counts (digits or English words) in a text span; [] when none."""
    nums = [int(x) for x in re.findall(r"(\d+)", text or "")]
    for w, n in _DAY_WORDS.items():
        if re.search(r"\b%s\b" % w, text or "", re.I):
            nums.append(n)
    return nums


def policy_office_ok(question):
    """Office gate: Yes when the question explicitly states on-site/hybrid at
    or below EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK.

    Returns (True, note) or (False, park_reason). Higher frequencies,
    full-time on-site, or unparseable non-frequency content park."""
    q = question or ""
    day_nums = []
    for m in re.finditer(r"(\d+)\s*[-\u2013]?\s*days?", q, re.I):
        # Hyphen-aware: hyphenated "4-days-per-week" must parse as 4, not
        # fall through to "no parseable frequency".
        day_nums.append(int(m.group(1)))
    for w, n in _DAY_WORDS.items():
        if re.search(r"\b%s\s*days?\b" % w, q, re.I):
            day_nums.append(n)
    # Parenthesized Anchor Day lists, e.g. "(Mon/Tue/Thu)" = 3 days.
    anchor = re.search(r"\((Mon|Tue|Wed|Thu|Fri)(/(Mon|Tue|Wed|Thu|Fri)){1,4}\)", q)
    if anchor:
        day_nums.append(anchor.group(0).count("/") + 1)
    if day_nums:
        asked = max(day_nums)  # conservative: the highest stated frequency
        if asked <= EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK:
            return True, "asked %d days/week within policy ceiling %d" % (
                asked, EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK)
        return False, "asked %d days/week exceeds policy ceiling %d" % (
            asked, EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK)
    if re.search(r"full.?time.{0,25}(on.?site|in.?office)", q, re.I):
        return False, "full-time on-site/in-office exceeds policy ceiling"
    return False, "no parseable office day frequency — park"


def _posting_office_violation(text):
    """Policy office-cap on posting text.

    Returns a reason fragment or None. Conservative by design: a day count
    only counts with office-context nearby, and an explicit at-or-below-cap
    frequency short-circuits clean before the full-time pattern can fire
    (so "full-time hybrid, 3 days in office" stays clean).
    """
    t = text or ""
    day_nums = []
    for mm in re.finditer(r"(\d+)\s*days?", t, re.I):
        ctx = t[max(0, mm.start() - 50):mm.end() + 50]
        if re.search(r"\boffice\b|on.?site|in.?person|workplace", ctx, re.I):
            day_nums.append(int(mm.group(1)))
    for w, n in _DAY_WORDS.items():
        for mm in re.finditer(r"\b%s\s*days?\b" % w, t, re.I):
            ctx = t[max(0, mm.start() - 50):mm.end() + 50]
            if re.search(r"\boffice\b|on.?site|in.?person|workplace", ctx, re.I):
                day_nums.append(n)
    if day_nums:
        asked = max(day_nums)
        if asked > EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK:
            return ("%d days/week in-office/on-site requirement "
                    "(policy cap is %d)" % (asked, EXAMPLE_MAX_OFFICE_DAYS_PER_WEEK))
        return None
    m = re.search(r"full.?time.{0,30}(in.?person|in.?office|on.?site)", t, re.I)
    if m:
        return ("full-time %s requirement (policy: full-time "
                "on-site/in-person parks)" % m.group(1))
    return None


def posting_eligibility_screen(text):
    """Hard eligibility blockers in posting text.

    Returns [reason, ...]; empty = clean. Pure function, no network.
    These are permanent eligibility blocks, not the applicant's call, so
    they park as PARKED (recoverable), never needs_input."""
    reasons = []
    t = text or ""
    for pat in STATE_EXCLUSION_RES:
        m = pat.search(t)
        if m:
            reasons.append(
                "posting explicitly excludes hires/residents in your state "
                "(eligibility): \"%s\"" % m.group(0).strip()[:120])
            break
    office_hit = _posting_office_violation(t)
    if office_hit:
        reasons.append(
            "posting office requirement exceeds policy cap (eligibility): %s"
            % office_hit)
    return reasons


# ---------------------------------------------------------------------------
# Field-Requirement Protocol (FRP) packet write-back
# ---------------------------------------------------------------------------

def _frp_write_packet_back(packet):
    """Persist a packet that FRP annotated, back to its packet file.

    Atomic tmp+replace. No-op when the packet carries no role_id or the
    packet file is gone (buffer/archived packets are out of scope).
    """
    role_id = packet.get("role_id")
    if not role_id:
        return
    path = os.path.join(PACKET_DIR, f"{role_id}.json")
    if not os.path.exists(path):
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(packet, f, indent=1)
    os.replace(tmp, path)


def _frp_enabled(packet):
    """Whether the Field-Requirement Protocol hook engages for this packet.

    Opt-in: packet.get("frp_enabled") is True, or env KEEL_FRP=1. Default
    OFF — the legacy unmapped-question path (park with the applicant's-own-
    words reason) is the repo's baseline behavior, and FRP only engages
    when the operator has deliberately wired a field_question_protocol
    module in. This keeps screen_packet's contract stable regardless of
    which sibling modules happen to be importable in the engines dir.
    """
    if isinstance(packet, dict) and "frp_enabled" in packet:
        return bool(packet["frp_enabled"])
    return os.environ.get("KEEL_FRP", "").strip().lower() in ("1", "true", "yes")


def _frp_unmapped(question, packet, answer_bank):
    """Route one unmapped required question through the Field-Requirement
    Protocol. Returns "answered" (do not park), a reason string (park), or
    None (FRP disabled/absent/failed/skipped — caller uses the legacy
    reason)."""
    if not _frp_enabled(packet):
        return None
    try:
        import field_question_protocol as frp
    except Exception:
        return None
    try:
        res = frp.handle_unmapped_question(
            question, packet.get("company", ""), packet.get("role_id", ""),
            packet.get("ats", ""), bank=answer_bank)
    except Exception as e:
        print(f"  prescreen: FRP hook failed (non-fatal): {e}", file=sys.stderr)
        return None
    action = res.get("action")
    if action == "answered":
        d = res.get("derived") or {}
        try:
            packet.setdefault("frp_derived_answers", {})[question] = {
                "bank_key": d.get("bank_key"),
                "answer": d.get("answer"),
                "provenance": d.get("provenance"),
            }
            _frp_write_packet_back(packet)
        except Exception as e:
            print(f"  prescreen: FRP packet write-back failed (non-fatal): {e}",
                  file=sys.stderr)
        return "answered"
    if action in ("instant-park", "park"):
        return res.get("reason")
    return None  # "skip": non-question operational note, nothing to park on


# ---------------------------------------------------------------------------
# Pre-promotion screen (blocker-aware promotion: screen before promoting)
# ---------------------------------------------------------------------------

def render_probe_brief(intel):
    """Render a form_intel probe dict into the brief FORM INTEL text format.

    Same rendering the post-build screen sees, so extract_form_intel and the
    question extractors behave identically here. Terminated with STEP 3 to
    satisfy extract_form_intel's lookahead.
    """
    lines = ["FORM INTEL — VERIFIED PRE-LAUNCH (pre-promotion probe):"]
    for q in intel.get("questions", []) or []:
        line = f"  - [{q.get('type')}] {q.get('label')}"
        if q.get("options"):
            line += f"  OPTIONS: {' | '.join(q['options'])}"
        lines.append(line)
    lines.append("")
    lines.append("STEP 3")
    return "\n".join(lines)


def screen_entry_prepromotion(entry, url=None, answer_bank=None):
    """Screen a LIVE-verified lead for input blockers BEFORE READY promotion.

    Runs the full screen_packet commitment/essay/attestation/question checks
    against an HTTP form-intel probe of the posting, so blocked leads route
    straight to needs_input without ever promoting or burning a packet build.

    Returns {"verdict": "CLEAN"|"PARK"|"UNKNOWN", "reasons": [...]}.
    Fail-OPEN by design for leads with no URL to screen: returns CLEAN.
    A form-intel probe that FAILS returns UNKNOWN -- the screen could not
    run, so the lead remains verification work and must NOT be treated as
    clean (fail-closed on screen failure; the promotion path raises
    VerificationUnavailable on UNKNOWN). Never raises.
    """
    try:
        e = entry or {}
        probe_url = url or e.get("ats_url") or e.get("application_url") or ""
        if not probe_url:
            return {"verdict": "CLEAN", "reasons": []}
        try:
            import form_intel as _fi
        except Exception:
            return {"verdict": "CLEAN", "reasons": []}
        try:
            intel = _fi.probe_url(probe_url)
        except Exception as ex:
            # Fail-closed: the screen failed, so the verdict is UNKNOWN --
            # the lead remains verification work. Never CLEAN.
            return {"verdict": "UNKNOWN",
                    "reasons": [f"form-intel probe failed: {ex}"]}
        if not isinstance(intel, dict):
            return {"verdict": "CLEAN", "reasons": []}
        # NOTE: empty questions do NOT early-return CLEAN. An ATS with no
        # HTTP extraction still runs the blind employer-prior path inside
        # screen_packet — identical to the post-build behavior for
        # intel-less packets. Only a URL/import/probe failure fails open.
        bank = answer_bank
        if bank is None:
            bank_path = os.path.join(BASE, "answer_bank.json")
            bank = json.load(open(bank_path)) if os.path.exists(bank_path) \
                else {}
        packet = {
            "role_id": e.get("role_id"),
            "company": e.get("company") or "",
            "title": e.get("title") or "",
            "ats": e.get("ats") or intel.get("ats") or "",
            "brief": render_probe_brief(intel),
            "posting_text": e.get("posting_text") or "",
        }
        return screen_packet(packet, bank)
    except Exception:
        # Fail-open: any unexpected trouble degrades to the pre-change
        # behavior (promote; post-build prescreen guards).
        return {"verdict": "CLEAN", "reasons": []}


def extract_form_intel(brief):
    """Return the FORM INTEL section of the brief (the verified pre-launch
    question list). Empty string when the packet carries no intel (e.g. Ashby
    packets where the per-job API is unavailable)."""
    if not brief:
        return ""
    m = re.search(r"FORM INTEL.*?(?=\nSTEP 3)", brief, re.S)
    if m:
        return m.group(0)
    return ""


def extract_required_text_questions(intel):
    """Pull required `[text] Label*` questions from the FORM INTEL section."""
    questions = []
    for m in re.finditer(r"^[ \t]*-\s*\[text\]\s*(.+?)\s*$", intel, re.M):
        label = m.group(1).strip()
        if label.endswith("*"):
            questions.append(label.rstrip("*").strip())
    return questions


def question_mappable(question, answer_bank):
    """True only if an answer_bank answer key or banded-question rule clearly
    covers the question. `answer_bank` is the loaded answer_bank.json dict."""
    if _no_ai_hard_stop(question):
        return False, None
    answers = (answer_bank or {}).get("answers", {})
    banded = (answer_bank or {}).get("banded_questions", {})
    valid_keys = set(answers) | set(banded)
    for pattern, key in BANK_MAP:
        if pattern.search(question) and key in valid_keys:
            return True, key
    return False, None


def _hit(patterns, text):
    return [p.pattern for p in patterns if p.search(text)]


def screen_packet(packet, answer_bank, employer_patterns=None):
    """Screen one launch packet.

    Returns {"verdict": "CLEAN"|"PARK", "reasons": [str, ...]}.
    Every PARK reason is worded with GENUINE_PAT-recognized terms so the
    parked lead is never mistaken for verification-only by verify_retry.
    """
    reasons = []
    brief = packet.get("brief", "") or ""
    company = packet.get("company", "") or ""
    intel = extract_form_intel(brief)

    # Inspect each complete question before narrower pre-authorized mappings.
    # Combined checkbox statements may put the no-AI clause hundreds of
    # characters beyond the truthfulness clause, outside a match window.
    for line in intel.splitlines():
        if re.search(r"\[(?:text|checkbox|radio|dropdown)\]", line, re.I) and _no_ai_hard_stop(line):
            reasons.append("Required no-AI / unaided-work attestation needs the applicant's explicit word (attest): " + line[:160])

    # Posting-text eligibility: hard blockers that live on the posting,
    # invisible to form-intel screening. Screened ONLY on posting_text —
    # never the brief (its GATES template would false-positive).
    for reason in posting_eligibility_screen(packet.get("posting_text") or ""):
        reasons.append(reason)

    for pat in _hit(OFFICE_RELOCATION_RE, intel):
        m = re.search(pat, intel, re.I)
        label = m.group(0).strip() if m else pat
        reasons.append(
            f"Required office/relocation/travel commitment question needs the applicant's "
            f"explicit answer (travel): \"{label[:120]}\""
        )

    for pat in _hit(ESSAY_RE, intel):
        m = re.search(pat, intel, re.I)
        label = m.group(0).strip() if m else pat
        reasons.append(
            f"Required essay / own-original-writing question needs the applicant's own "
            f"words (essay): \"{label[:120]}\""
        )

    for pat in _hit(ATTEST_RE, intel):
        m = re.search(pat, intel, re.I)
        if not m:
            continue
        label = m.group(0).strip()
        # Window the match so question_mappable sees the actual question,
        # not the whole intel blob.
        window = intel[max(0, m.start() - 250): m.end() + 250]
        # Hard-stop classes first: no-AI / unaided-work / personally-completed
        # would be FALSE if answered by an agent — always park.
        if any(p.search(window) for p in NO_AI_ATTEST_RE):
            reasons.append(
                f"Required attestation needs the applicant's explicit word (attest): "
                f"\"{label[:120]}\""
            )
            continue
        # Pre-authorized routing: standard legal attestations whose text maps
        # to a banked pre-authorized key skip the park — the brief carries the
        # operator's standing answer. Anything else parks.
        ok, _key = question_mappable(window, answer_bank)
        if ok and _key in PREAUTHORIZED_ATTEST_KEYS:
            continue
        reasons.append(
            f"Required attestation needs the applicant's explicit word (attest): "
            f"\"{label[:120]}\""
        )

    for q in extract_required_text_questions(intel):
        ok, _key = question_mappable(q, answer_bank)
        if ok:
            continue
        frp_out = _frp_unmapped(q, packet, answer_bank)
        if frp_out == "answered":
            continue  # answered from verified facts/banked applicant answer
        if frp_out:
            reasons.append(frp_out)
            continue
        reasons.append(
            f"Required free-text question not in answer bank -- needs the applicant's "
            f"own words, must not invent: \"{q[:120]}\""
        )

    patterns = employer_patterns if employer_patterns is not None else load_employer_patterns()
    company_lc = (packet.get("company") or "").lower()
    brief_lc = brief.lower()
    # Fallback: some packets leave `company` empty; the brief's target line
    # ("Submit a job application for <Applicant> to <Employer> for the role")
    # still names the employer.
    if not company_lc:
        m = re.search(r"application for .+? to (.+?) for the role", brief, re.I)
        if m:
            company_lc = m.group(1).strip().lower()
    # "Has intel" means the packet actually enumerates form questions. A FORM
    # INTEL section containing only a resume-file line (typical for Ashby
    # packets, where the per-job API is unavailable) counts as NO intel.
    has_form_questions = bool(re.search(r"\[(text|dropdown|radio|checkbox)\]",
                                        intel, re.I))
    for emp_key, blockers in patterns.items():
        if not company_lc or emp_key not in company_lc:
            continue
        if not has_form_questions:
            # No form intel in this packet (e.g. Ashby per-job API unavailable)
            # but this employer has confirmed blocker patterns: PARK pending a
            # live form check or the applicant's explicit input. Never assume clean.
            for blocker in blockers:
                reasons.append(
                    f"Employer form pattern for {company or emp_key} with no form "
                    f"intel in packet to rule it out -- needs the applicant's explicit "
                    f"input (attest/travel): \"{blocker[:120]}\""
                )
        else:
            for blocker in blockers:
                if blocker.lower() in brief_lc:
                    reasons.append(
                        f"Employer form pattern hit for {company or emp_key} -- needs "
                        f"applicant's explicit input (attest/travel): \"{blocker[:120]}\""
                    )

    # De-dupe while preserving order.
    seen, uniq = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)

    return {"verdict": "PARK" if uniq else "CLEAN", "reasons": uniq}


# ---------------------------------------------------------------------------
# Parking -- conventional fields only (see module docstring).
# ---------------------------------------------------------------------------

def _load_queue(path):
    if not os.path.exists(path):
        return []
    data = json.load(open(path))
    return data if isinstance(data, list) else data.get("leads", [])


def _save_queue(path, leads):
    json.dump(leads, open(path, "w"), indent=1)


# Ported 2026-09-18 from the main pipeline
# (engines/application-executor/prescreen.py, J-20260915-2140-gate-316,
# ARM 71): synthetic test traffic must never emit production gate events.
# Unit tests and ad-hoc probes call park_lead() with fixture role_ids
# (STRIPE-TEST-1, TEST-*, ...); without this guard every test run appended
# gate_blocked rows to production telemetry/events.jsonl (~43 rows since
# 2026-09-15, J-20260918-2130-gate-2313). The guard lives here — at the
# single production emission choke point in this fork — so no test file
# needs its own mock to stay clean. Token-delimiter anchored: FIRETEST-*
# are REAL sweep leads (not synthetic) and must keep emitting.
_SYNTHETIC_RID_PAT = re.compile(
    r"(^|[-_])(test|fixture|synthetic|smoke|mock|demo|example|sample)([-_]|$)",
    re.IGNORECASE,
)


def is_synthetic_role_id(role_id):
    """True when role_id is test/synthetic traffic, not a real lead."""
    return bool(_SYNTHETIC_RID_PAT.search(str(role_id or "")))


def _primary_gate(reasons):
    text = " ".join(reasons).lower()
    # ARM 72 (2026-09-15): recording consent is its own integrity class --
    # classify it as recording_consent (in GATE_TYPES), never needs_input.
    if "recording consent" in text:
        return "recording_consent"
    if "essay" in text:
        return "essay"
    if any(w in text for w in ("relocation", "hybrid", "office", "travel")):
        return "travel"
    if "attest" in text or "arbitration" in text:
        return "attest"
    return "needs_input"


def park_lead(role_id, reasons, queue_dir=None, backup=True):
    """Move a lead from its owning queue (standard OR strategic) to
    needs_input-queue.json using ONLY the conventional fields the
    classifier reads. Overwrites any stale status_reason. Backs up the
    touched queue files first. One-lead-one-queue is preserved: the lead
    is removed from exactly the queue file that held it.

    Returns {"ok": True, ...} or {"ok": False, "error": ...}.
    """
    qdir = queue_dir or QUEUE_DIR
    std_path = os.path.join(qdir, "standard-queue.json")
    strat_path = os.path.join(qdir, "strategic-queue.json")
    ni_path = os.path.join(qdir, "needs_input-queue.json")

    std_leads = _load_queue(std_path)
    strat_leads = _load_queue(strat_path)
    ni_leads = _load_queue(ni_path)

    owner = None
    owner_leads = None
    for name, leads in (("standard-queue.json", std_leads),
                        ("strategic-queue.json", strat_leads)):
        hit = [l for l in leads if l.get("role_id") == role_id]
        if hit:
            if owner is not None:
                # Cross-queue duplicate role_id: fail closed — never move a
                # lead while its identity is ambiguous.
                return {"ok": False,
                        "error": f"{role_id} found in both "
                                 f"{owner} and {name}; refusing to move"}
            owner, owner_leads, lead = name, leads, hit[0]
    if owner is None:
        return {"ok": False,
                "error": f"{role_id} not found in standard- or "
                         f"strategic-queue.json"}

    ts = datetime.now(PDT).strftime("%Y%m%d-%H%M%S")
    ts_short = datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")

    if backup:
        bdir = os.path.join(qdir, f"_backup-{ts}-prescreen")
        os.makedirs(bdir, exist_ok=True)
        owner_path = std_path if owner == "standard-queue.json" else strat_path
        for src in (owner_path, ni_path):
            if os.path.exists(src):
                shutil.copy(src, os.path.join(bdir, os.path.basename(src)))

    reason_text = "; ".join(reasons)
    # Overwrite -- never append to -- stale text like "posting confirmed live...
    # promoted READY", which would make verify_retry resurrect this lead.
    lead["status"] = "PARKED-NEEDS-INPUT"
    lead["status_reason"] = (
        f"prescreen.py {ts_short}: parked, needs applicant input. {reason_text}"
    )
    lead["unresolved"] = list(reasons)
    lead["gate_note"] = reason_text
    lead["queue_notes"] = (
        (lead.get("queue_notes") or "")
        + (f" | prescreen {ts}: parked awaiting applicant input"
           if lead.get("queue_notes") else
           f"prescreen {ts}: parked awaiting applicant input")
    )
    lead["status_updated"] = ts_short

    owner_leads = [l for l in owner_leads if l.get("role_id") != role_id]
    ni_leads.append(lead)
    owner_path = std_path if owner == "standard-queue.json" else strat_path
    _save_queue(owner_path, owner_leads)
    _save_queue(ni_path, ni_leads)

    # J-20260918-2130-gate-2313 (port of J-20260915-2140-gate-316): synthetic
    # test traffic never emits production gate events (see
    # is_synthetic_role_id above).
    if not is_synthetic_role_id(role_id):
        try:
            log_event.log(
                "gate_blocked",
                role_id=role_id,
                company=lead.get("company", ""),
                source="prescreen",
                details={"gate": _primary_gate(reasons), "reasons": reasons},
            )
        except Exception as e:
            print(f"  prescreen: telemetry log failed (non-fatal): {e}", file=sys.stderr)

    return {"ok": True, "role_id": role_id, "backup": backup,
            "from_queue": owner}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def scan_packets(packet_dir=None, answer_bank=None, employer_patterns=None):
    pdir = packet_dir or PACKET_DIR
    bank = answer_bank
    if bank is None:
        # Prefer the personalized working copy in data/ (edited after setup.sh);
        # the engines/ copy is only a fallback for runs outside a workspace.
        data_bank = os.path.join(PIPELINE, "data", "answer_bank.json")
        engines_bank = os.path.join(BASE, "answer_bank.json")
        bank_path = data_bank if os.path.exists(data_bank) else engines_bank
        bank = json.load(open(bank_path)) if os.path.exists(bank_path) else {}
    patterns = employer_patterns if employer_patterns is not None else load_employer_patterns()
    results = []
    for fp in sorted(glob.glob(os.path.join(pdir, "*.json"))):
        try:
            packet = json.load(open(fp))
        except Exception as e:
            results.append({"role_id": os.path.basename(fp), "verdict": "ERROR",
                            "reasons": [f"unreadable packet: {e}"], "file": fp})
            continue
        if not isinstance(packet, dict) or "brief" not in packet:
            results.append({"role_id": os.path.basename(fp), "company": "",
                            "verdict": "SKIP",
                            "reasons": ["not a launch packet (no brief)"],
                            "file": fp})
            continue
        res = screen_packet(packet, bank, patterns)
        res["role_id"] = packet.get("role_id", os.path.basename(fp))
        res["company"] = packet.get("company", "")
        res["file"] = fp
        results.append(res)
    return results


def main():
    ap = argparse.ArgumentParser(description="Pre-launch packet screening gate.")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Scan packets and print verdicts (default).")
    ap.add_argument("--live", action="store_true",
                    help="Apply parking for PARK verdicts.")
    args = ap.parse_args()
    live = args.live

    results = scan_packets()
    parked, clean, errors, skipped = 0, 0, 0, 0
    for r in results:
        v = r["verdict"]
        if v == "PARK":
            parked += 1
        elif v == "CLEAN":
            clean += 1
        elif v == "SKIP":
            skipped += 1
        else:
            errors += 1
        print(f"[{v:5}] {r['role_id']} ({r.get('company','?')})")
        for reason in r["reasons"]:
            print(f"         - {reason}")

    print(f"\n{len(results)} files: {clean} CLEAN, {parked} PARK, "
          f"{skipped} SKIP, {errors} ERROR")

    if live and parked:
        print("\nApplying parking (--live)...")
        for r in results:
            if r["verdict"] != "PARK":
                continue
            out = park_lead(r["role_id"], r["reasons"])
            print(f"  {'PARKED' if out['ok'] else 'SKIP'}: {r['role_id']}"
                  + ("" if out["ok"] else f" ({out.get('error')})"))
    elif not live:
        print("\nDry-run only. Re-run with --live to apply parking.")


if __name__ == "__main__":
    main()
