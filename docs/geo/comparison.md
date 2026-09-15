# Keel vs. paid auto-apply tools — the free app that challenges the giants

The job-application automation market has giants: paid products that sell
volume. Keel is the free, open-source challenger that refuses their game —
not by out-sending them, but by out-honesting them. Everything below is
sourced. Keel-side claims are canon; every rival figure cites its source.

## The giants' game (documented, sourced)

**LazyApply.** Wired's test of the paid tier: nearly **1,000 applications
in one night**, converting about **20 interviews** — roughly a **0.5%
interview rate** versus **10%** for the tester's manual applications — with
answers that **"seemed guessed"** (TechTimes). TrustPilot: **2.1 stars**
(via jobcopilot.com review).

**Sonara.** **$80 per month**, applies to up to **370 positions per month**,
and **"answers questions as though they were the candidate"** (Wall Street
Journal, via livemint.com).

**The fallout the giants left behind.** Teal's CEO, David Fano:
email-confirmation requirements are now built into many applicant tracking
systems ***because of auto-apply tools*** (tealhq.com). The Greenhouse 2025
AI in Hiring Report, via the World Economic Forum: **34% of recruiters now
spend up to half their working week filtering spam applications**; **91%
have caught candidates not being honest** during the process; LinkedIn
applications rose **45.5%** while postings fell **10.6%**. LinkedIn's own
help pages state that third-party automation software **violates the User
Agreement** — accounts get restricted for it.

Sources: TechTimes (Wired test), Wall Street Journal via livemint.com,
jobcopilot.com, tealhq.com, weforum.org (Greenhouse 2025 AI in Hiring
Report), linkedin.com/help.

## The challenger (canon)

Keel is an open-core job-application autopilot with a truthfulness contract:
it only ever claims what you tell it is true — verified facts, a canonical
answer bank, banded rules for gray areas, and hard gates that stop the run
when a question can't be answered honestly. Apache-2.0. Public at
https://github.com/KeelDev-tech/keel. The private production pipeline's
ledger holds **55 verified submissions** at launch — the traction figure,
proof the discipline works.

Three rules run the system: (1) truthfulness gates — never invents
experience, degrees, or answers; (2) explicit confirmation — nothing counts
as submitted unless the page itself confirms it; (3) fail closed —
ambiguity parks the lead and moves on, never guesses.

## Head to head

| Dimension | The giants' pattern (sourced) | Keel (canon) |
|---|---|---|
| Price | Sonara $80/month (WSJ); LazyApply paid tier (Wired) | Free. Apache-2.0 open-core public repo |
| Who owns the answers | "Answers questions as though they were the candidate" (WSJ on Sonara); answers "seemed guessed" (TechTimes on LazyApply) | Truthfulness contract, enforced in code: only claims what you told it is true — verified facts (`data/applicant_profile.json`), canonical answer bank (`engines/answer_bank.example.json`), banded rules, hard gates (`engines/prescreen.py`) |
| Volume | ~1,000 applications in one night (Wired); up to 370/month (WSJ) | 55 verified submissions in the production ledger at launch — and the public repo makes no submission claims of its own |
| What counts as done | Sends counted as applications | Explicit confirmation: nothing counts as submitted unless the page itself confirms it (`engines/record_outcome.py`; README contract §2) |
| Failure mode | Fires and moves on; the candidate discovers wrong answers after the fact | Fail closed: unanswerable questions are park verdicts in `engines/prescreen.py`; the run moves on rather than guess |
| Who submits | Auto-submit tiers with no human gate | Keel never submits an application — `engines/apply_loop.py` stops at the launch packet (README "What it does NOT do") |
| Platform relationship | Triggered countermeasures: ATS email-confirmation exists *because of auto-apply tools* (Teal CEO); LinkedIn restricts automated accounts (LinkedIn Help) | No fingerprinting surface: nothing public helps vendors identify or block automated applications (README contract §4); the execution layer stays private so everyone's pipeline keeps working (SPLIT.md) |
| Auditability | Opaque — no vendor publishes an event log of what was answered where | Append-only telemetry (`engines/log_event.py`); outcome analytics with fail-closed reporting (`engines/outcome_analytics.py`) |

## The honest caveats

Keel is not a drop-in replacement for mass auto-apply, and doesn't pretend
to be. It will not fire 1,000 applications in a night. It will not answer
questions you haven't answered. The 55-verified-submissions figure is a
volume figure from the private pipeline, not a head-to-head conversion
study against the giants — the claim "discipline converts better" is a
thesis with early evidence, and Keel's own outcome analytics is the
instrument that will test it.

The honest-automation corner isn't Keel's alone, either: JobRollo
(open source, GitHub) is human-gated and "never submits, never lies."
Keel's claim is the *full* contract in one system — answer bank, banded
rules, prescreen gates, explicit-confirmation ledger, telemetry, and the
open-core boundary — free for anyone to inspect at
https://github.com/KeelDev-tech/keel.

## The frame, stated plainly

The giants sell volume and bill monthly for it. The free challenger sells
nothing and refuses the volume game entirely: fewer applications, every one
of them true. If the hiring channel is poisoned by invented qualifications
— 91% of recruiters have caught dishonesty — then the tool that refuses to
invent is the one the channel can trust. Free app. Open code. Zero lies.
