#!/usr/bin/env python3
"""Loop 1 miner — turn field experience into learning proposals.

Reads:
  - worker-charter/edge-case-review.md (novel edge cases flagged by workers)
  - telemetry/events.jsonl since the last watermark (recurring gates/errors)

Writes draft proposals to worker-charter/learning-proposals.md for human
review. A proposal is only a candidate: promoting it to a constraint (new C-ID)
or a workspace operating-lessons doc stays human-approved, because a bad
auto-generated constraint is worse than none.

RETUNE 2026-09-15 (ARM 86, blackboard J-20260915-1422-meth-133):
the 12:05 miner run's 6 auto-drafts were ALL killed on ARM-72 triage
(learning-proposals.md, "ARM 72 triage verdicts"):
  - 4 generic gate-count drafts: "gate counts alone are not a proposal"
    (travel/technique_blocked/materials_demoted/needs_input)
  - 2 junk theme drafts: theme keywords were common stopwords
    (about, above, across, actioned, address...), and the "reports" cited
    were prior mining runs, not edge cases.
So the miner now:
  (a) never emits a bare gate-count draft for a gate class that is a
      designed sink (DESIGNED_SINKS) or already represented as a draft in
      learning-proposals.md (fingerprint dedupe) — a count is not a pattern;
  (b) filters mining-run log blocks out of theme clustering, expands the
      stopword list with the triage-identified noise, and requires a
      minimum-specificity bar before an edge-case theme drafts;
  (c) fingerprints every emitted draft and skips re-drafting classes that
      already exist in learning-proposals.md (manual arms draft directly,
      bypassing the watermark, so the miner must not re-mine their windows).

Usage: python3 mine_learnings.py [--reset-watermark]

Path convention: this module lives at the top-level worker-charter/ dir,
so it resolves paths locally with a $KEEL_HOME fallback (never the private
pipeline's workspace path).
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
KEEL_HOME = os.environ.get("KEEL_HOME") or os.path.dirname(BASE)
EVENTS = os.path.join(KEEL_HOME, "data", "telemetry", "events.jsonl")
REVIEW = os.path.join(BASE, "edge-case-review.md")
PROPOSALS = os.path.join(BASE, "learning-proposals.md")
WATERMARK = os.path.join(BASE, ".mine-watermark")

# Baseline English stopwords.
STOP = set("the a an and or of to in on for with is are was were be by as at "
           "it its this that from have has had not no vs via per".split())

# Noise words identified by the ARM-72 triage: junk "theme" drafts were
# clusters of these common words (theme keywords: "about, above, across,
# actioned, address, after, against, agent" and "abort, about, above,
# abridge, absent, action, active, adjacent"). They add zero specificity.
NOISE = set(
    "about above across after against action actions active adjacent abort "
    "absent address actioned agent also always another around because between "
    "both each more most much many some such than then these those though "
    "through under until while within without would could should will just "
    "like make made other into over only same even still back here there "
    "what when where which while whose".split())

# Bare gate-count drafts are NEVER proposals for these classes: they are the
# designed sinks (the applicant's standing skip directive / charter rules),
# so their recurrence is expected behavior, not a pattern. A novel SUB-CASE
# of one of these classes is a job for a semantic mining arm, not this
# counter. Cites: ARM-72 triage KILL of P-2026-09-15 12:05-1 (travel, 6x)
# and P-2026-09-15 12:05-4 (needs_input, 3x): "gate counts alone are not a
# proposal"; travel/commitment parks are the genuine needs_input class per
# the calibration rule.
DESIGNED_SINKS = {
    "needs_input", "travel", "essay", "attest",
    "relocation_commitment", "relocation", "office_commitment",
    "verification_code", "email_verification_code",
}

# Blocks in edge-case-review.md that are mining-run logs, not edge cases.
# The 12:05 junk theme drafts cited prior mining runs as "reports".
MINING_RUN_PAT = re.compile(
    r"(mining run|events scanned|watermark|constraints applied|mining window)",
    re.IGNORECASE)

# A theme must clear this bar before it drafts: at least this many
# distinct content keywords (not in STOP/NOISE) across the cluster union.
THEME_MIN_KEYWORDS = 3
# A theme must cite at least this many distinct non-mining-run reports.
THEME_MIN_REPORTS = 2
# Cluster overlap threshold (unchanged from the original miner).
CLUSTER_OVERLAP = 0.4


def keywords(text: str):
    """Content keywords of a text: 4+ letter words minus STOP and NOISE."""
    return {w for w in re.findall(r"[a-z]{4,}", text.lower())
            if w not in STOP and w not in NOISE}


def is_mining_run_block(text: str) -> bool:
    return bool(MINING_RUN_PAT.search(text))


def proposal_fingerprint(kind: str, key: str) -> str:
    """Stable machine-readable fingerprint for a draft proposal class."""
    norm = re.sub(r"[^a-z0-9_]+", "_", key.strip().lower()).strip("_")
    return f"{kind}:{norm}"


def drafted_fingerprints(proposals_text: str):
    """Recover the fingerprint set from existing learning-proposals.md.

    Matches both the new `- Fingerprint: kind:key` lines and legacy
    \"recurring gate `X`\" / \"recurring error\" headers so old drafts
    still dedupe.
    """
    found = set()
    for m in re.finditer(r"(?m)^- Fingerprint: (\S+)", proposals_text):
        found.add(m.group(1).strip())
    for m in re.finditer(r"(?m)^## .*recurring gate `([^`]+)`", proposals_text):
        found.add(proposal_fingerprint("gate", m.group(1)))
    for m in re.finditer(r"(?m)^## .*recurring error \(\d+x\): (.+)$", proposals_text):
        found.add(proposal_fingerprint("error", m.group(1)[:80]))
    return found


def gate_draft_allowed(gate: str, count: int, drafted) -> bool:
    """A bare gate-count draft is a proposal only if it clears the triage bar.

    Kills (per ARM-72): counts for designed sinks, counts already drafted,
    and single/low counts (thresholds: >=3 recurring events).
    """
    if count < 3:
        return False
    if gate in DESIGNED_SINKS:
        return False
    if proposal_fingerprint("gate", gate) in drafted:
        return False
    return True


def error_draft_allowed(note: str, count: int, drafted) -> bool:
    if count < 2:
        return False
    if proposal_fingerprint("error", note[:80]) in drafted:
        return False
    return True


def cluster_reviews(reviews):
    """Cluster edge-case review blocks by keyword overlap.

    Mining-run log blocks are excluded up front (they are not evidence).
    Returns a list of clusters, each a list of block texts.
    """
    blocks = [r for r in reviews if r and not is_mining_run_block(r)]
    clusters = []
    used = set()
    for i, r in enumerate(blocks):
        if i in used:
            continue
        kw = keywords(r)
        group = [r]
        used.add(i)
        for j, r2 in enumerate(blocks):
            if j in used:
                continue
            kw2 = keywords(r2)
            if kw and kw2 and len(kw & kw2) / min(len(kw), len(kw2)) >= CLUSTER_OVERLAP:
                group.append(r2)
                used.add(j)
        if len(group) >= THEME_MIN_REPORTS:
            clusters.append(group)
    return clusters


def theme_draft_allowed(group) -> bool:
    """A theme cluster drafts only if it is specific enough to review.

    ARM-72 killed junk themes whose keywords were stopwords citing mining
    runs. A draft-worthy theme needs real content keywords AND real
    (non-mining-run) reports.
    """
    union_kw = set().union(*[keywords(g) for g in group]) if group else set()
    real_reports = [g for g in group if not is_mining_run_block(g)]
    return (len(union_kw) >= THEME_MIN_KEYWORDS
            and len(real_reports) >= THEME_MIN_REPORTS)


def theme_fingerprint(group) -> str:
    union_kw = sorted(set().union(*[keywords(g) for g in group]))[:8]
    return proposal_fingerprint("theme", "_".join(union_kw))


def load_watermark() -> int:
    try:
        return int(open(WATERMARK).read().strip())
    except (OSError, ValueError):
        return 0


def mine(events, reviews_text, proposals_text):
    """Pure mining pass: returns the draft-proposal markdown and the
    number of drafts proposed. Does no I/O."""
    drafted = drafted_fingerprints(proposals_text)
    gates = Counter()
    errors = Counter()
    for e in events:
        et = e.get("event_type", "")
        d = e.get("details", {}) or {}
        if et in ("gate_blocked", "gate_encountered") and d.get("gate"):
            gates[d["gate"]] += 1
        if et == "error":
            errors[str(d.get("note", ""))[:80]] += 1

    reviews = [b.strip() for b in re.split(r"\n## ", reviews_text) if b.strip()]
    clusters = cluster_reviews(reviews)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"\n# Mining run {stamp} — events scanned this run: {len(events)} "
           f"(watermark advanced after write)\n"]
    proposed = 0

    for gate, cnt in gates.most_common(5):
        if not gate_draft_allowed(gate, cnt, drafted):
            continue
        fp = proposal_fingerprint("gate", gate)
        proposed += 1
        out.append(
            f"## P-{stamp}-{proposed}: recurring gate `{gate}` ({cnt}x since last run)\n"
            f"- Fingerprint: {fp}\n"
            f"- Evidence: {cnt} gate events with details.gate={gate}.\n"
            f"- Candidate: new negative constraint (C-ID) naming this gate's handling, "
            f"or a technique-library amendment if it is ATS-specific.\n"
            f"- Reviewer: confirm the pattern is real, then promote.\n")

    for note, cnt in errors.most_common(5):
        if not error_draft_allowed(note, cnt, drafted):
            continue
        fp = proposal_fingerprint("error", note[:80])
        proposed += 1
        out.append(
            f"## P-{stamp}-{proposed}: recurring error ({cnt}x): {note}\n"
            f"- Fingerprint: {fp}\n"
            f"- Candidate: fault-isolation note or new constraint.\n"
            f"- Reviewer: check whether the fix belongs in code or in charter text.\n")

    for group in clusters:
        if not theme_draft_allowed(group):
            continue
        fp = theme_fingerprint(group)
        if fp in drafted:
            continue
        theme = ", ".join(sorted(set().union(*[keywords(g) for g in group]))[:8])
        proposed += 1
        out.append(
            f"## P-{stamp}-{proposed}: recurring edge-case theme ({len(group)} reports)\n"
            f"- Fingerprint: {fp}\n"
            f"- Theme keywords: {theme}\n"
            f"- Reports:\n" + "".join(f"  - {g[:200].strip()}\n" for g in group) +
            f"- Candidate: new exemplar (E-ID) if it is a solved pattern, or a new "
            f"constraint (C-ID) if it is a failure to prevent.\n"
            f"- Reviewer: decide exemplar vs constraint, then promote.\n")

    if proposed == 0:
        out.append("No recurring patterns this run. Nothing to promote.\n")

    return "\n".join(out), proposed


def main():
    if "--reset-watermark" in sys.argv:
        open(WATERMARK, "w").write("0")
        print("watermark reset")
        return

    wm = load_watermark()
    events = []
    if os.path.exists(EVENTS):
        with open(EVENTS) as f:
            for i, line in enumerate(f):
                if i < wm:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    new_wm = wm + len(events)

    reviews_text = open(REVIEW).read() if os.path.exists(REVIEW) else ""
    proposals_text = open(PROPOSALS).read() if os.path.exists(PROPOSALS) else ""

    body, proposed = mine(events, reviews_text, proposals_text)

    with open(PROPOSALS, "a") as f:
        f.write(body)
    open(WATERMARK, "w").write(str(new_wm))
    print(f"mining complete: {len(events)} events scanned, {proposed} proposals -> {PROPOSALS}")
    print(f"watermark {wm} -> {new_wm}")


if __name__ == "__main__":
    main()
