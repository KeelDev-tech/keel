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
        label = m.group(0).strip() if m else pat
        reasons.append(
            f"Required attestation needs the applicant's explicit word (attest): "
            f"\"{label[:120]}\""
        )

    for q in extract_required_text_questions(intel):
        ok, _key = question_mappable(q, answer_bank)
        if not ok:
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


def _primary_gate(reasons):
    text = " ".join(reasons).lower()
    if "essay" in text:
        return "essay"
    if any(w in text for w in ("relocation", "hybrid", "office", "travel")):
        return "travel"
    if "attest" in text or "arbitration" in text:
        return "attest"
    return "needs_input"


def park_lead(role_id, reasons, queue_dir=None, backup=True):
    """Move a lead from standard-queue.json to needs_input-queue.json using
    ONLY the conventional fields the classifier reads. Overwrites any stale
    status_reason. Backs up both queue files first.

    Returns {"ok": True, ...} or {"ok": False, "error": ...}.
    """
    qdir = queue_dir or QUEUE_DIR
    std_path = os.path.join(qdir, "standard-queue.json")
    ni_path = os.path.join(qdir, "needs_input-queue.json")

    std_leads = _load_queue(std_path)
    ni_leads = _load_queue(ni_path)

    hit = [l for l in std_leads if l.get("role_id") == role_id]
    if not hit:
        return {"ok": False, "error": f"{role_id} not found in standard-queue.json"}
    lead = hit[0]

    ts = datetime.now(PDT).strftime("%Y%m%d-%H%M%S")
    ts_short = datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")

    if backup:
        bdir = os.path.join(qdir, f"_backup-{ts}-prescreen")
        os.makedirs(bdir, exist_ok=True)
        for src in (std_path, ni_path):
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

    std_leads = [l for l in std_leads if l.get("role_id") != role_id]
    ni_leads.append(lead)
    _save_queue(std_path, std_leads)
    _save_queue(ni_path, ni_leads)

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

    return {"ok": True, "role_id": role_id, "backup": backup}


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
