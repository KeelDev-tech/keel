"""Words-needed digest: a passive, zero-nagging doc of parked leads that need the applicant's own words.

Reads queue/needs_input-queue.json, collects genuine input blockers from the
conventional `unresolved` field, and renders ~/workspace/your_files/words-needed-digest.md
grouped by blocker kind. Standing rule: parked items are never surfaced or prompted
about -- this doc is passive option value the operator can open whenever they want.

Usage:
    python3 words_needed_digest.py            # dry run: print what would change
    python3 words_needed_digest.py --rebuild   # regenerate the doc

Idempotent: same queue state -> byte-identical output. No timestamps in the doc.
Never invents answers; never includes secrets (unresolved text is quoted verbatim,
so keep PII out of queue unresolved fields upstream).
"""

import json
import os
import re
import sys

from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
QUEUE_PATH = os.path.join(DATA, "queues", "needs_input-queue.json")
DOC_PATH = os.environ.get(
    "KEEL_DIGEST_PATH",
    os.path.join(HOME, "your_files", "words-needed-digest.md"))

# Blocker kinds and the keyword patterns that detect them in unresolved text.
KIND_PATTERNS = [
    ("office_travel", re.compile(
        r"office|in[- ]person|relocat|hybrid|commute|attendance|travel|on[- ]site"
        r"|days?\s*(per|a)\s*week|\d+\s*times?\s*a\s*week", re.I)),
    ("essay", re.compile(
        r"essay|own words|tell us something|personal wording|unassisted writing"
        r"|original,?\s*unassisted|in their own words", re.I)),
    ("attestation", re.compile(
        r"attest|arbitration|certif|personally completed|consent to|binding "
        r"(legal|agreement)|undersigned", re.I)),
    ("screener", re.compile(
        r"screener|free[- ]text|self[- ]characterization|self[- ]description", re.I)),
]

GROUP_META = {
    "office_travel": {
        "title": "Office / travel commitments",
        "why": "Only you can commit to where and how often you'd work in person. "
               "The pipeline will never answer an office-attendance, relocation, or "
               "travel question on your behalf -- that would be a false commitment.",
        "unblock": "Reply in chat with your answer per role (e.g. \"OpenAI Launch Programs: "
                   "yes, 3 days/week in SF works\") and the lead gets re-queued.",
    },
    "essay": {
        "title": "Essays / personal writing",
        "why": "These must be your own words. The pipeline never drafts personal essays, "
               "motivation statements, or \"tell us about yourself\" answers under your name.",
        "unblock": "Reply in chat with your text for any of these and the lead gets re-queued.",
    },
    "attestation": {
        "title": "Legal attestations & certifications",
        "why": "Binding agreements (arbitration clauses) and \"personally completed\" "
               "certifications need your explicit decision. Agents can't sign for you, "
               "and won't.",
        "unblock": "Reply in chat with your decision per role (e.g. \"Sierra: accept the "
                   "arbitration agreement\") and the lead gets re-queued.",
    },
    "screener": {
        "title": "Experience screeners",
        "why": "These ask for specific experience claims in your own phrasing. Anything not "
               "already in the verified answer bank would be invented, so the pipeline "
               "stops instead.",
        "unblock": "Reply in chat with your answers per role and the lead gets re-queued.",
    },
    "other": {
        "title": "Other decisions & actions",
        "why": "These need an action only you can take (sending an application email yourself, "
               "creating a platform account, picking a program track). The pipeline never "
               "sends mail or creates accounts under your identity without your say-so.",
        "unblock": "Reply in chat when done (or with your pick), and the lead gets re-queued.",
    },
}

GROUP_ORDER = ["office_travel", "essay", "attestation", "screener", "other"]


def split_role_key(role_key):
    """Parse 'Company | Title | location' role_keys into (company, title)."""
    if not role_key or "|" not in role_key:
        return None, None
    parts = [p.strip() for p in role_key.split("|")]
    return (parts[0] or None, parts[1] if len(parts) > 1 else None)


def lead_identity(lead):
    company = lead.get("company")
    title = lead.get("title")
    if not company or not title:
        rk_company, rk_title = split_role_key(lead.get("role_key"))
        company = company or rk_company or "Unknown employer"
        title = title or rk_title or "Unknown role"
    return company, title


def classify(text):
    """Return the set of blocker kinds a single unresolved string matches."""
    kinds = {kind for kind, pat in KIND_PATTERNS if pat.search(text or "")}
    _op = os.environ.get("KEEL_OPERATOR_NAME", "").strip()
    if not kinds and _op and re.search(r"\b" + re.escape(_op) + r"\b",
                                       text or "", re.I):
        kinds.add("other")
    return kinds


def build_groups():
    """Scan the queue; return (groups, skipped_count)."""
    queue = json.load(open(QUEUE_PATH))
    leads = queue if isinstance(queue, list) else queue.get("leads", [])
    groups = {kind: [] for kind in GROUP_ORDER}
    skipped = 0
    for lead in leads:
        unresolved = lead.get("unresolved") or []
        if not unresolved:
            skipped += 1
            continue
        matched = {}
        for item in unresolved:
            for kind in classify(str(item)):
                matched.setdefault(kind, []).append(str(item))
        if not matched:
            skipped += 1  # verification/research-only; not words the applicant must supply
            continue
        company, title = lead_identity(lead)
        fit = lead.get("fit_score")
        entry = {
            "role_id": lead.get("role_id"),
            "company": company,
            "title": title,
            "fit": fit,
            "blockers": matched,
        }
        for kind in matched:
            groups[kind].append(entry)
    for kind in groups:
        groups[kind].sort(
            key=lambda e: (-(e["fit"] if isinstance(e["fit"], (int, float)) else -1),
                           e["company"], e["title"]))
    return groups, skipped


def render(groups, skipped):
    total = len({e["role_id"] for kind in groups.values() for e in kind})
    lines = []
    lines.append("# Words Needed")
    lines.append("")
    lines.append("A passive list of parked job leads that are stuck only because they need "
                 "your own words, answers, or decisions. Nothing here will ever ping you -- "
                 "open it whenever you like, ignore it the rest of the time.")
    lines.append("")
    lines.append(f"{total} parked lead{'s' if total != 1 else ''} waiting on you. "
                f"({skipped} other parked items need research/verification, not your words, "
                "so they're not listed.)")
    lines.append("")
    for kind in GROUP_ORDER:
        entries = groups[kind]
        meta = GROUP_META[kind]
        lines.append(f"## {meta['title']} ({len(entries)})")
        lines.append("")
        lines.append(f"Why the pipeline can't do this itself: {meta['why']}")
        lines.append("")
        lines.append(f"How to unblock: {meta['unblock']}")
        lines.append("")
        if not entries:
            lines.append("_None right now._")
            lines.append("")
            continue
        for e in entries:
            fit = e["fit"] if isinstance(e["fit"], (int, float)) else "?"
            lines.append(f"### {e['company']} — {e['title']} (fit {fit})")
            lines.append("")
            for b in e["blockers"][kind]:
                # Quote verbatim; never paraphrase into an answer.
                lines.append(f"- {b}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv):
    groups, skipped = build_groups()
    content = render(groups, skipped)
    existing = open(DOC_PATH).read() if os.path.exists(DOC_PATH) else None

    if "--rebuild" in argv:
        if existing == content:
            print(f"up to date: {DOC_PATH} unchanged")
            return 0
        os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
        with open(DOC_PATH, "w") as f:
            f.write(content)
        n = len({e["role_id"] for kind in groups.values() for e in kind})
        print(f"rebuilt {DOC_PATH} ({n} leads, {skipped} skipped)")
        return 0

    # Default: dry run -- report what would change.
    if existing is None:
        n = len({e["role_id"] for kind in groups.values() for e in kind})
        print(f"would create {DOC_PATH} with {n} leads "
              f"({', '.join(f'{k}:{len(v)}' for k, v in groups.items() if v)})")
        return 0
    if existing == content:
        print("up to date: no changes")
        return 0
    print(f"would update {DOC_PATH}: content differs from queue state")
    # Summarize per-group drift without dumping full text.
    old_groups = existing.count("## ")
    print(f"  groups in existing doc: {old_groups}; groups now: {len(GROUP_ORDER)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
