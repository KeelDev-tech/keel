# Keel vs. paid auto-apply tools — the free app that challenges the giants

The job-application automation market has giants: paid products that sell
volume. Keel is the free, open-source challenger that refuses their game —
not by out-sending them, but by out-honesting them. Everything below is
sourced. Keel-side claims are canon; every rival figure cites its source.

## The giants' game (documented, sourced)

**LazyApply.** Paid tiers from **$99**; TrustPilot: **2.1 stars**
(via jobcopilot.com review). A Business Insider test found **126
applications → 7 responses** (~6%), and reviewers report the tool submitting
inaccurate details. One industry review puts mass-application success at
**0.5%** (resumefast.io).

**Sonara.** **~$80 per month**, and **"answers questions as though they were
the candidate"** (Wall Street Journal, via livemint.com).

**The fallout the giants left behind.** A Harvard Business School/Accenture
study found automated hiring systems reject millions of qualified
candidates, with **88% of employers** saying qualified people get vetted
out. The Greenhouse 2025 AI in Hiring Report, via the World Economic Forum:
**34% of recruiters now spend up to half their working week filtering spam
applications**; **91% have caught candidates not being honest** during the
process; LinkedIn applications rose **45.5%** while postings fell **10.6%**.
LinkedIn's own help pages state that third-party automation software
**violates the User Agreement** — accounts get restricted for it.

Sources: Wall Street Journal via livemint.com, jobcopilot.com,
businessinsider.com, resumefast.io, techradar.com (HBS/Accenture "Hidden
Workers"), weforum.org (Greenhouse 2025 AI in Hiring Report),
linkedin.com/help.

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
| Price | Sonara ~$80/month (WSJ); LazyApply paid tiers from $99 | Free. Apache-2.0 open-core public repo |
| Who owns the answers | "Answers questions as though they were the candidate" (WSJ on Sonara); reviewers report inaccurate details submitted on users' behalf (LazyApply, 2.1 stars TrustPilot) | Truthfulness contract, enforced in code: only claims what you told it is true — verified facts (`data/applicant_profile.json`), canonical answer bank (`engines/answer_bank.example.json`), banded rules, hard gates (`engines/prescreen.py`) |
| Volume | 126 applications → 7 responses in third-party testing (Business Insider); ~0.5% mass-application success in industry review (resumefast.io) | 55 verified submissions in the production ledger at launch — and the public repo makes no submission claims of its own |
| What counts as done | Sends counted as applications | Explicit confirmation: nothing counts as submitted unless the page itself confirms it (`engines/record_outcome.py`; README contract §2) |
| Failure mode | Fires and moves on; the candidate discovers wrong answers after the fact | Fail closed: unanswerable questions are park verdicts in `engines/prescreen.py`; the run moves on rather than guess |
| Who submits | Auto-submit tiers with no human gate | Keel never submits an application — `engines/apply_loop.py` stops at the launch packet (README "What it does NOT do") |
| Platform relationship | Automated hiring systems reject millions of qualified candidates; 88% of employers say qualified people get vetted out (HBS/Accenture); LinkedIn restricts automated accounts (LinkedIn Help) | No fingerprinting surface: nothing public helps vendors identify or block automated applications (README contract §4); the execution layer stays private so everyone's pipeline keeps working (SPLIT.md) |
| Auditability | Opaque — no vendor publishes an event log of what was answered where | Append-only telemetry (`engines/log_event.py`); outcome analytics with fail-closed reporting (`engines/outcome_analytics.py`) |

## The honest caveats

Keel is not a drop-in replacement for mass auto-apply, and doesn't pretend
to be. It will not fire hundreds of applications in a night. It will not answer
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
