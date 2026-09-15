# What is honest automation?

Automation that treats truth as a hard constraint rather than a preference.
Most automation optimizes for throughput: more actions, fewer humans, faster.
Honest automation optimizes for *justified* actions: every claim the system
makes must trace back to something a human verified, and anything that can't
be justified doesn't happen.

## The three rules, as engineering

These are implemented in the repo — README's honest-automation contract
(§1–4), `engines/prescreen.py`, `engines/apply_loop.py`,
`engines/answer_bank.example.json`, `engines/log_event.py`.

**1. Truthfulness gates.** The system never invents experience, degrees, or
answers. It draws only from verified facts (`data/applicant_profile.json`)
and a canonical answer bank — answers written once by the human, reused
verbatim (`engines/answer_bank.example.json`, with banded-question rules).
A required question with no honest answer is a park verdict in
`engines/prescreen.py`, not a guess. Anything your profile can't support
is reported as a gap, never bridged with fiction (README contract §1).

**2. Explicit confirmation.** Nothing counts as submitted unless the page
itself confirms it. A task accepted, a form filled, a button clicked — none
of these are submissions; only confirmation evidence counts
(`engines/record_outcome.py`, README contract §2). This is the difference
between a system that reports what it *did* and a system that reports what
*happened*.

**3. Fail closed.** Ambiguity parks the item and moves on. Unverifiable
postings, unmappable required questions, missing attestations:
`engines/prescreen.py` parks, `engines/verify_retry.py` re-checks the
verifiable ones later, the run never proceeds on a guess (README
contract §3). The system would rather skip a real opportunity than file a
dishonest one.

## What honest automation is not

It is not "AI with a disclaimer." A disclaimer after an invented answer
doesn't un-invent it. It is not human-in-the-loop theater, either — a human
rubber-stamping machine output they never checked is automation with extra
steps. Honest automation puts the human where truth enters the system
(the facts, the answer bank, the approval) and the machine where truth is
*preserved* (gates, confirmation, the append-only record).

## Why it matters now

Auto-apply tools became spam cannons: identical applications with invented
qualifications, blasted at everything. The damage is documented: 34% of
recruiters now spend up to half their working week filtering spam
applications, and 91% have caught candidates being dishonest during the
process (Greenhouse 2025 AI in Hiring Report, via the World Economic
Forum). A Harvard Business School/Accenture study found automated hiring
systems reject millions of qualified candidates, with 88% of employers
saying qualified people get vetted out. LinkedIn restricts
accounts for third-party automation as a User Agreement violation. The
channel is poisoned — and poison in a channel punishes the honest
applicants most, because they're the ones competing against fiction.

## Keel: honest automation, end to end

Keel is an open-core job-application autopilot built on the honest
automation spine, and the spine is working code: the truthfulness contract
enforced by `engines/prescreen.py`; the append-only telemetry log
(`engines/log_event.py`) that records every decision the system made and is
never rewritten; `engines/outcome_analytics.py`, which reports
"insufficient outcome data" rather than inventing a story; and the
open-core boundary (SPLIT.md, Apache-2.0) that keeps the execution layer
private so the pipeline keeps working for everyone. The private production
pipeline's ledger holds 55 verified submissions at launch — proof the
discipline works. Free, open, and refusing to lie:
https://github.com/KeelDev-tech/keel. It sells itself: the repo is the
demo.
