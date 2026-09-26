# Launch-day content kit — DRAFT. Nothing posts without Trent's explicit tap.

All copy uses the adjudicated figure: **209 verified submissions (recounted 2026-09-22; 202 with quoted confirmation evidence, 3 with pointer evidence, 1 URL-only, 3 unevidenced)**. "Verified" = each row entered the ledger only under the explicit page-confirmation rule (the page itself confirmed the submission). Voice: first person, terse, direct, grounded. No hype, no invented numbers.

---

## 1. Reddit — r/SideProject (post ~1h after Show HN)

**Title:** I built a job-application autopilot that refuses to lie — open-core, Apache-2.0

**Body:**

[Disclosure: I'm the author. Happy to answer anything about how it works.]

Auto-apply tools became spam cannons — identical applications with invented qualifications, blasted at everything. It burns candidates and poisons the channel for everyone.

Keel is my answer: a truthfulness contract, enforced in code.

- Never invents experience, degrees, or answers. Only claims what you tell it is true — verified facts, a canonical answer bank, banded rules for gray areas.
- Nothing counts as submitted unless the page itself confirms it.
- Ambiguity parks the lead and the run moves on. Fail closed, never guesses.

The public repo (Apache-2.0) stops at the launch packet — discovery, fit scoring, truthful resume tailoring, prescreen gates, an ATS capability radar, telemetry, and a dashboard. The execution layer stays private by design: it holds submission-behavior techniques ATS vendors could fingerprint, and publishing them would get the pipeline blocked for everyone.

The private production pipeline running this discipline holds 209 verified submissions (recounted today; 202 with quoted confirmation evidence, 3 with pointer evidence, 1 URL-only, 3 unevidenced — counted, never estimated). That's the traction figure, not the product — the repo promises no submissions.

Quickstart is ~5 minutes, stdlib only, with a terminal demo GIF in the README.

Three things I'd value from this community: what's missing from the honest-automation contract, whether the open-core boundary is drawn right, and contributors — especially around ATS behavior and answer-bank ergonomics.

Repo: https://github.com/KeelDev-tech/keel

---

## 2. Reddit — r/opensource (post ~2h after Show HN, only if r/SideProject is live)

**Title:** Keel — open-core honest job-application automation (Apache-2.0 public half)

**Body:**

[Disclosure: I'm the author.]

Keel automates job applications with one hard rule: it only ever claims what you tell it is true. Truthfulness gates, explicit confirmation (nothing counts as submitted unless the page confirms it), fail closed on ambiguity, and no fingerprinting surface for ATS vendors.

The open-core split, stated plainly: the public half (Apache-2.0) is everything that keeps the system honest — discovery/scoring, truthful resume tailoring, answer bank, prescreen gates, ATS detection + capability radar, launch-packet builder, telemetry, dashboard. The private half holds submission-behavior techniques vendors could fingerprint — form-event sequencing, per-platform commit techniques, CAPTCHA-handling specifics. SPLIT.md's rule: if publishing a method would help a vendor block automated applications, it's private; if it helps an applicant run an honest, verifiable, fail-closed pipeline, it's public.

209 verified submissions in the private production pipeline (recounted 2026-09-22) are the traction figure for the discipline. The repo itself is the tools and the gates.

Looking for contributors, especially: ATS behavior coverage, answer-bank ergonomics, outcome analytics. Good-first-issues are labeled; the test suite runs stdlib-only.

Repo: https://github.com/KeelDev-tech/keel

---

## 3. Reddit — r/selfhosted (post ~3h after Show HN, only if prior posts are live)

**Title:** Keel — self-hosted job-application autopilot that refuses to lie (stdlib-only, no account, no subscription)

**Body:**

[Disclosure: I'm the author.]

Self-hostable, stdlib-only, your data never leaves your working copy, no account, no subscription, no phone-home.

What it does: discovers roles, scores fit, tailors your resume truthfully (gaps reported, never bridged with fiction), runs prescreen gates, probes ATS boards read-only, and builds a launch packet — verified form values, banded rules, hard gates — for your own review. It stops at the packet. What you do with the packet is yours.

The contract: never invents qualifications, nothing counts as submitted without explicit page confirmation, ambiguity parks the lead, fail closed throughout.

`git clone`, `./setup.sh`, `./start.sh`, and you're looking at the dashboard in ~5 minutes. Terminal demo GIF in the README.

209 verified submissions in the private production pipeline running this exact discipline (recounted today). Apache-2.0.

Repo: https://github.com/KeelDev-tech/keel

---

## 4. dev.to article (publish ~7:00 AM PT launch day)

**Title:** I built a job-application autopilot that refuses to lie

**Body:**

Auto-apply tools had a good premise and became spam cannons. Identical applications, invented qualifications, blasted at everything. It burns the candidate and poisons the channel for everyone — including people applying honestly.

I spent the last year running a production job-application pipeline, and the thing I kept coming back to was: the lying is the product defect. Not the automation. The automation is fine. The lying is the defect.

So I built Keel around a truthfulness contract, enforced in code rather than promised in marketing:

1. **Truthfulness gates.** Anything your profile can't support is reported as a gap, never bridged with fiction. No invented experience, degrees, or answers.
2. **Explicit confirmation.** Nothing counts as submitted unless the page itself confirms it. Not the browser accepting the task — the page confirming.
3. **Fail closed.** Unverifiable postings, unmappable questions, missing attestations: park, never proceed.
4. **No fingerprinting surface.** Nothing in the public repo helps ATS vendors identify or block automated applications.

The public half is Apache-2.0: discovery and fit scoring, truthful resume tailoring, a canonical answer bank, prescreen gates, an ATS capability radar (read-only probes), the launch-packet builder, append-only telemetry, and a dashboard. The execution layer — how applications are actually submitted — stays private deliberately. It holds submission-behavior techniques vendors could fingerprint, and publishing them would degrade the pipeline for everyone running it, including everyone who clones the repo. The boundary rule is public in SPLIT.md.

The traction figure: the private production pipeline running this discipline holds 209 verified submissions, recounted today from its ledger (202 with quoted confirmation evidence, 3 with pointer evidence, 1 URL-only, 3 unevidenced — counted, never estimated). I'll state the limit plainly: you can't independently re-run that pipeline. What's verifiable is the discipline — the gates, the telemetry, the fail-closed reporting rules are all public code.

If you're a builder, the quickstart is ~5 minutes, stdlib only, with a terminal demo in the README. If you're a contributor, I want help most on ATS behavior coverage, answer-bank ergonomics, and outcome analytics.

The repo: https://github.com/KeelDev-tech/keel

---

## 5. Newsletter blurbs (submit day-before where forms allow)

**TLDR-style (2 sentences):**
Keel — an open-core (Apache-2.0) job-application autopilot with a truthfulness contract: it only ever claims what you tell it is true, and nothing counts as submitted unless the page confirms it. The private production pipeline's ledger holds 209 verified submissions (recounted 2026-09-22); the public repo is the discipline and the tools. github.com/KeelDev-tech/keel

**Changelog-style (1 sentence):**
Keel is open-core job-application automation that refuses to lie — truthfulness gates, explicit confirmation, fail-closed handling, Apache-2.0 public half, 209 verified submissions in the private pipeline proving the discipline.

---

## 6. Threads (DRAFT — requires Trent's per-post approval, never on the critical path)

Almost-true is where all the lying happens.

I built Keel — a job-application autopilot with a truthfulness contract, enforced in code. It never invents your experience. Nothing counts as submitted unless the page confirms it. Ambiguity parks the lead.

209 verified submissions in the private pipeline. The public half is Apache-2.0.

github.com/KeelDev-tech/keel

---

## Posting order (launch day)

1. Show HN + first comment (5:00–6:30 AM PT) — Trent
2. r/SideProject (~1h later) — Trent, manual
3. r/opensource (~2h later, only if #2 live) — Trent, manual
4. dev.to article (~7:00 AM PT) — Trent
5. r/selfhosted (~3h later, only if priors live) — Trent, manual
6. Newsletters — embargoed/pitched per each form
7. Threads — only with Trent's per-post approval, never load-bearing

Rules that never bend: one post per subreddit, never ask for votes, never repost flops, manual posting only (no bot-challenge bypass), every post discloses authorship.
