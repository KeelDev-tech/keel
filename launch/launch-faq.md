# Launch FAQ / objection handling — Keel

Response sheet for HN comments (and X replies). Every answer below is grounded in one of: talking-points.md, show-hn-final.md, launch-thread-final.md, README.md, SPLIT.md. Where no file answers, the item is marked **OPEN** — do not improvise.

## "Is this another spam cannon?"

Answered by the launch copy and README.

Keel takes the opposite bet from auto-apply spam cannons. The truthfulness contract is the product: it only ever claims what you tell it is true — verified facts, a canonical answer bank, banded rules for gray areas, and hard gates that stop the run cold when a question can't be answered honestly. It never invents experience, degrees, or answers. Per the README's "What it does NOT do" section: "Keel never submits an application" (the public loop stops at the launch packet), "Keel never invents qualifications" (anything unsupported is reported as a gap, never bridged with fiction), and "Keel makes no submission claims." Ambiguity parks the lead and moves on — never guesses.

## "Why open-core instead of fully open source?"

Answered by SPLIT.md and talking-points.md.

The split is defensive, not commercial trickery. The private half contains submission-behavior methods ATS vendors could fingerprint: form-event sequencing, per-platform commit techniques, CAPTCHA-handling specifics, credential/verification-code flows, the live API-direct submission transport. SPLIT.md's rule of thumb: "If a method's publication would help a vendor block automated applications, it is private. If it helps an applicant run an honest, verifiable, fail-closed pipeline, it is public." Publishing the private half would degrade the pipeline for everyone running it. The open half (Apache-2.0) is everything that doesn't carry that risk: discovery/scoring, truthful resume tailoring, the answer bank, prescreen gates, ATS detection and capability radar, the launch-packet builder, telemetry, and the dashboard.

## "Where's the proof? / How do I verify the 92 submissions claim?"

Answered by talking-points.md and README.md.

The figure is: the private production pipeline's ledger holds **92 verified submissions** (SUBMITTED rows counted 2026-09-15 14:44 PT; the pipeline runs continuously, so the live count only grows). Ground rules around it, stated in the files: (1) it belongs to the private production pipeline, not the public repo — the repo itself makes no submission claims; (2) the claim is built on the explicit-confirmation rule — nothing counts as submitted unless the page itself confirms it, and the ledger increments only on real confirmation evidence; (3) every event lands in an append-only telemetry log with fail-closed reporting rules for outcome analytics. Honest detail on the number: the canonical evidence audit (`engines/outcome-tracking/evidence_gate.py`) reads 92 SUBMITTED rows — 92 with explicit confirmation evidence on file, 0 with posting-URL evidence only, 0 unevidenced. Be honest about the limit: a reader of the public repo cannot independently re-run the private production pipeline. The verifiable part is the discipline (the gates, the telemetry, the fail-closed reporting rules are all public code) — the 92 figure is the traction claim for the discipline, not something the repo asks anyone to take on faith about the code.

## "How do you avoid ATS fingerprinting?"

Answered by SPLIT.md (boundary), specifics intentionally **OPEN**.

The anti-fingerprinting strategy is the open-core boundary itself: nothing in the public repo helps an ATS vendor identify or block automated applications — README states "No fingerprinting surface" as contract rule 4. The public ATS detection rules (URL patterns, board APIs) are read-only identification; detection-only, no submission behavior. The submission paths those probes discover are exercised only in the private layer. How the private layer specifically evades fingerprinting is not published — that is the whole point of the split — so do not answer technical specifics beyond the boundary. If pressed: the techniques stay private precisely so vendors can't study and block them.

## "What's the business model?"

Partially answered; the rest is **OPEN**.

What the files say: open-core, Apache-2.0 public half; README names "Managed execution is the hosted tier" as the private-layer commercial shape, and talking-points list "seed-stage conversations with people who care about automation that refuses to lie" as the ask. Do not go beyond that. Pricing, hosted-tier availability, revenue targets, or fundraising status are OPEN — not in the files.

## "Why should I trust this if the executor is private?"

Answered by SPLIT.md and README.md.

Two reasons given in the files: (1) the honest-automation contract is fully public — truthfulness gates, explicit confirmation, fail closed, no fingerprinting surface; the EXECUTOR CONTRACT documented at the end of the public apply loop specifies exactly what a submission layer must do; (2) the generic brief's per-field verification protocol (verify after every field, blur test, dropdown re-open check) is public because it's a correctness practice, not a fingerprintable technique — so the honesty-critical parts are inspectable, and what's private is only the DOM-event sequences vendors could fingerprint.

## "Why should I believe the number isn't inflated?"

Answered by the reporting rules in the files.

The ledger increments only on explicit confirmation evidence — that is the standing reporting rule ("Never count browser-task acceptance as submission" is the operational ancestor of it). Outcome analytics are computed with fail-closed reporting rules: any rate with a denominator under 5 reads "insufficient outcome data," and hypotheses are hedged and labeled. The telemetry is append-only. Say this, and don't oversell.

## Responses to never give

- Never mention any private business detail.
- Never claim a publish, submission, or metric beyond what's in the files.
- Never get defensive; never argue with a critic — acknowledge, answer from the files, and log the objection if it's novel (edge-case intake, not a fight).

## OPEN (for the founder — not answered in the files)

- Exact business model beyond "managed execution is the hosted tier" and seed conversations.
- Whether to name the private pipeline's scale (beyond the 92 figure), tech stack, or employer response rates.
- Product Hunt: in or out?
- Whether to publish a sanitized sample launch packet / dashboard screenshot as demo material (a screenshot exists at docs/assets/dashboard-screenshot.png — confirm it's launch-ready).
