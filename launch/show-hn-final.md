# Show HN — FINAL (awaiting the founder's approval to post)

## Title
Show HN: Keel — automated job applications that refuse to lie

## Body
Job applications are hours of grind per role: find the posting, tailor the
resume, answer the same screening questions, survive the ATS form, repeat.
The "auto-apply" tools that promise to fix this mostly became spam cannons —
identical applications with invented qualifications, blasted at everything.
It burns candidates and poisons the channel for everyone.

Keel takes the opposite bet: an open-core job-application autopilot
constrained by a truthfulness contract. Three rules run the whole system:

1. **Truthfulness gates** — Keel only ever claims what you told it is true.
   Your verified facts, a canonical answer bank, banded rules for gray
   areas, and hard gates that stop the run cold when a question can't be
   answered honestly. It never invents experience, degrees, or answers.
2. **Explicit confirmation** — nothing counts as submitted unless the page
   itself confirms it. The ledger increments only on real confirmation
   evidence, and every event lands in an append-only telemetry log.
3. **Fail closed** — ambiguity parks the lead and moves on. Never guesses.

It's open-core by design (Apache-2.0). The public repo holds everything that
doesn't create adversarial risk: discovery and scoring, truthful resume
tailoring, the answer bank, prescreen gates, ATS detection plus a capability
radar, the launch-packet builder, telemetry, and a dashboard. Per SPLIT.md,
the public pipeline stops at the launch packet. The private execution layer
— submission-behavior techniques that ATS vendors could fingerprint and
block — stays private deliberately. Publishing it would degrade the pipeline
for everyone running it.

Proof the discipline works: the private production pipeline's ledger holds 94 verified submissions (as of 2026-09-15; the pipeline runs continuously, so the live count only grows). That's the traction figure, not
the product — the repo itself makes no submission claims. It's the tools,
the gates, and the telemetry.

I'd love HN's feedback on three things: the honest-automation contract
(what's missing?), the open-core boundary (did we draw the line in the
right place?), and contributors — especially around ATS behavior,
answer-bank ergonomics, and outcome analytics. Repo link in comments.
