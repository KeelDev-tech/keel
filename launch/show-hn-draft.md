# Show HN draft — Keel (open-core job-application autopilot)

Status: DRAFT ONLY — nothing published. Posted copy must be approved first.

## Title options (pick one)

1. Show HN: Keel — an open-core job-application autopilot with truthfulness gates
2. Show HN: Keel — automated job applications that refuse to lie
3. Show HN: Keel — open-core automation for job applications, honest by construction

## Body

Job applications are hours of grind per role: find the posting, tailor the
resume, answer the same screening questions, survive the ATS form, do it
again. Existing "auto-apply" tools answer that grind by becoming spam
cannons — blasting identical applications with invented qualifications,
which hurts candidates and poisons the channel for everyone.

Keel is an open-core autopilot that takes the opposite bet: automation
constrained by a truthfulness contract. It's built around three rules.
First, truthfulness gates: Keel only ever claims what you told it is true —
your verified facts, an answer bank of canonical answers, banded rules for
gray areas, and hard gates that stop the run cold when a question can't be
answered honestly. It never invents experience, degrees, or answers. Second,
explicit confirmation: nothing counts as submitted unless the page itself
says so; the ledger increments only on real confirmation evidence, and every
event lands in an append-only telemetry log. Third, fail closed: ambiguity
parks the lead and keeps moving, never guesses.

Keel is open-core by design. The public repo (Apache-2.0) holds everything
that doesn't create adversarial risk: discovery/scoring, truthful resume
tailoring, the answer bank, prescreen gates, ATS detection and a capability
radar, the launch-packet builder (per SPLIT.md, the public pipeline stops at
the launch packet), telemetry, and a dashboard. The private execution layer —
the submission-behavior techniques that ATS vendors could fingerprint and
block — stays private deliberately; publishing it would degrade the
pipeline for everyone running it.

The private production pipeline holds 55 verified submissions at launch — that's the proof the discipline works, not the product. The repo
itself makes no submission claims; it's the tools, the gates, and the
telemetry that produced them.

I'd love feedback from HN: the honest-automation contract (what's missing?),
the open-core boundary (did we draw the line in the right place?), and
contributors — especially around ATS behavior, answer-bank ergonomics, and
outcome analytics. Repo link in comments.
