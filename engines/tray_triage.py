#!/usr/bin/env python3
"""One-tap triage briefs for the Input Tray.

For each NEEDS-YOU card, attach a compact decision brief: what class of
question it is, which of the applicant's standing rules or verified facts bears on
it, and what a reply looks like. The brief NEVER answers the question -- it
only shrinks the decision to one tap. the applicant's words stay his.

Design notes:
- Deterministic classifier (keyword + family patterns). Zero credentials,
  zero hallucination risk, runs inside the digest delivery path.
- LLM backend seam: ``triage_llm.polish_best(brief)`` rewords brief text via
  the enabled LLM backends (GPT: ~/workspace/skills/openai, connector
  custom.openai; Gemini: ~/workspace/skills/gemini, connector custom.gemini;
  kill-switches in hidden_files/tray_polish.json). Every run appends a
  GPT-vs-Gemini comparison row to hidden_files/tray_polish_compare.jsonl
  (see compare_polish.py). The classifier's verdict (class + cited rule)
  is never delegated -- only wording. Any polish failure returns the
  deterministic line unchanged.
- Hard invariants (tested): essays never get a suggested answer; attestations
  and consents never get a pre-filled Yes/No; personal facts are never
  guessed; sponsorship is never defaulted.

Usage:
  tray_triage.py                       # briefs for current input-tray.json -> stdout (JSON)
  tray_triage.py --write               # also write hidden_files/input-tray-briefs.json
  tray_triage.py --line <card_key>     # one digest line for a card key
"""
from __future__ import annotations

import json
import os
import re
import sys

from keel_paths import HOME  # noqa: E402 — repo path convention
TRAY_JSON = os.path.join(HOME, "hidden_files", "input-tray.json")
BRIEFS_JSON = os.path.join(HOME, "hidden_files", "input-tray-briefs.json")

# (class, label, context, tap_hint)
# tap_hint describes the REPLY SHAPE, never the answer.
CLASSES = {
    "d1_travel": (
        "D1 travel",
        # EXAMPLE operator travel policy — replace with your own rule.
        "travel policy: defined schedule at 25% or under is fine; undefined, open-ended, or over 25% needs your judgment.",
        "reply Yes only if the posting states the travel % and it is 25% or under",
    ),
    "schedule_commitment": (
        "Schedule",
        "a schedule commitment only you can make -- no standing rule covers it.",
        "reply Yes/No in your words",
    ),
    "d1_office": (
        "D1 office",
        # EXAMPLE operator office policy — replace with your own rule.
        "office policy: relocate anywhere yes; on-site up to 3 days/week or undefined frequency is fine; 4-5 days/week parks.",
        "reply Yes/No",
    ),
    "no_ai": (
        "No-AI attestation",
        "hard stop -- never pre-authorized; answering for you would be false.",
        "your decision only -- reply with your call",
    ),
    "recording_consent": (
        "Recording consent",
        "interview recording consent is an integrity-class decision; never pre-authorized.",
        "your decision only -- reply with your call",
    ),
    "attestation": (
        "Attestation",
        "beyond the pre-authorized scope (arbitration, background check, at-will, truthfulness, data privacy); needs your explicit decision.",
        "reply Yes/No -- your call",
    ),
    "essay": (
        "Essay",
        "needs your own words -- no draft will ever be written for you.",
        "reply in your words",
    ),
    "consent": (
        "Opt-in",
        "a consent choice (marketing, messages, references) -- your preference, no verified default.",
        "reply Yes/No -- your call",
    ),
    "personal_fact": (
        "Personal fact",
        "only you know this -- not in verified records, never guessed.",
        "reply with the fact",
    ),
    "compensation": (
        "Compensation",
        "standing rule: answer at market median/midpoint unless you say otherwise.",
        "reply APPROVE to apply market-mid, or give your figure",
    ),
    "sponsorship": (
        "Sponsorship",
        "your rule: never guessed -- needs your explicit answer.",
        "reply Yes/No",
    ),
    "employment_fact": (
        "Employment check",
        "answerable from the verified record where known; otherwise your call.",
        "reply Yes/No",
    ),
    "availability": (
        "Availability",
        "shifts and scheduling are your call -- no standing rule.",
        "reply in your words",
    ),
    "your_call": (
        "Your call",
        "genuinely needs your judgment -- no rule or verified fact covers it.",
        "reply in your words",
    ),
}

# Ordered: first match wins. Specific/hard-stop classes before general ones.
PATTERNS = [
    ("no_ai", re.compile(r"no-?ai|own origin|own-origin|unassisted|personally completed|ai policy for interviewers|without (?:the )?assistance of ai|ai-generated", re.I)),
    ("recording_consent", re.compile(r"recording consent|consent to (?:being )?record|interview record", re.I)),
    ("schedule_commitment", re.compile(r"apac|schedule commitment|shift timings|home game season|24/7 operations|time off requirements", re.I)),
    ("d1_travel", re.compile(r"\btravel\b|\btrip\b|\bon the road\b|\bfield travel\b", re.I)),
    ("d1_office", re.compile(r"\boffice\b|\bon-?site\b|relocat|\bhybrid\b|\bremote\b|\bwork from\b|\bwfo\b|\brto\b", re.I)),
    ("attestation", re.compile(r"attest|certif|acknowledge|arbitration|background check|privacy policy|terms of service|non-?disclosure|nda\b|firearms|prohibited possessor|deemed export|truthfulness", re.I)),
    ("consent", re.compile(r"opt-?in|marketing communications|whatsapp|text messages|contact (?:additional )?references", re.I)),
    ("personal_fact", re.compile(r"street address|address line|github profile|company start month|pronouns|date of birth|\baddress\b", re.I)),
    ("compensation", re.compile(r"salary|compensation|hourly|pay range|pay expectation", re.I)),
    ("sponsorship", re.compile(r"sponsorship|visa|work authorization.*sponsor|require.*sponsor", re.I)),
    ("employment_fact", re.compile(r"currently (?:an? )?(?:employee|employed)|previously worked|worked directly with|terminated|fired|ever been (?:discharged|employed)", re.I)),
    ("availability", re.compile(r"availability|available|start date|when can you start|preferred shifts", re.I)),
    ("essay", re.compile(r"essay|in \d-\d sentences|tell us|why (?:are you|do you)|describe|proudest accomplishment|most impressive thing|what excites you|what interests you|good fit|three adjectives", re.I)),
]


def classify(question: str, family: str | None = None) -> str:
    """Return the triage class for a card. Deterministic; first match wins."""
    fam = (family or "").lower()
    if "travel" in fam:
        return "d1_travel"
    if "office" in fam or "relocation" in fam:
        return "d1_office"
    for cls, pat in PATTERNS:
        if pat.search(question or ""):
            return cls
    return "your_call"


def brief_for(card: dict) -> dict:
    cls = classify(card.get("question", ""), card.get("family"))
    label, context, tap_hint = CLASSES[cls]
    return {
        "card_key": card.get("key"),
        "class": cls,
        "label": label,
        "context": context,
        "tap_hint": tap_hint,
        "question": (card.get("question") or "")[:160],
        "unblock_leads": card.get("unblock_leads", 0),
    }


def brief_line(card: dict) -> str:
    """One mobile-friendly digest line. Never contains an answer.

    The deterministic line is authoritative; an optional LLM wording polish
    (triage_llm.polish_best: GPT and/or Gemini) may reword it. Polish is
    fail-safe: any failure returns this exact line, and the classifier's
    verdict is never delegated.
    """
    b = brief_for(card)
    line = f"   triage [{b['label']}]: {b['context']} ({b['tap_hint']})"
    try:
        from triage_llm import polish_best
        return polish_best(line)
    except Exception:
        return line


def load_tray() -> dict:
    with open(TRAY_JSON, encoding="utf-8") as f:
        return json.load(f)


def build_briefs() -> list[dict]:
    tray = load_tray()
    return [brief_for(c) for c in tray.get("cards", []) if c.get("status") == "NEEDS-YOU"]


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if argv[:1] == ["--line"]:
        key = argv[1]
        tray = load_tray()
        card = next((c for c in tray.get("cards", []) if c.get("key") == key), None)
        if not card:
            print(f"no card {key}", file=sys.stderr)
            return 1
        print(brief_line(card))
        return 0
    briefs = build_briefs()
    if "--write" in argv:
        tmp = BRIEFS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"briefs": briefs}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, BRIEFS_JSON)
    print(json.dumps({"briefs": briefs}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
