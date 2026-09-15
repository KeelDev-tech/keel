# Competitive map — auto-apply & job-search tools — DRAFT (internal, not for publication)

Prepared 2026-09-14. Internal research draft for the Keel marketing
formation. Nothing here is approved copy. Every public claim cites a
source; anything not sourced is marked UNVERIFIED or INFERRED.

---

## 1. The spam-cannon market (named players, documented mechanics)

### LazyApply (lazyapply.com)
- Chrome extension; browser-automation mass-apply across LinkedIn, Indeed,
  ZipRecruiter (jobcopilot.com review).
- Paid "Job GPT" tier: applied to nearly **1,000 jobs in one night**
  (Wired, via TechTimes). Answers to application questions **"seemed
  guessed"** (TechTimes).
- Conversion documented by Wired's tester: ~20 interviews from ~1,000+
  applications ≈ **0.5%**, vs. **10%** on his manual applications.
- TrustPilot: **2.1 stars**; page renamed to parent company "PEVE VISIONS",
  which the review site flags as possibly hiding negative reviews.
  Chrome Web Store: 3.6 stars, mixed reliability/refund complaints
  (jobcopilot.com review).
- Criticized on-record for: no verification of listings (wasted
  applications to fake/outdated postings), limited control over
  application quality, quantity-over-quality focus, potential platform
  detection of automated applications, no resume tailoring per role
  (jobcopilot.com review).
- Independent tester (iesemba.com) found it applied to **irrelevant
  listings** (internships, co-founder roles, wrong-industry "Operations"
  roles) and had to keep the browser window active while running.

### Sonara (sonara.ai)
- Founder: Victor Schwartz, ex-Duke; **$80/month**; finds jobs, "answers
  questions as though they were the candidate," and applies to up to
  **370 positions per month** (Wall Street Journal, via livemint.com).
- Match quality "can vary"; users trade specificity for volume
  (adzuna.co.uk review, 2026). Adzuna notes the "spamming employers"
  complaints surfaced around other auto-apply tools in comparison.

### Simplify (Simplify Jobs / simplify.jobs) — Simplify Copilot
- Autofill + tracker Chrome/Firefox extension.
- Public user review (extpose.com, 2026-09-10): on open-ended questions
  the extension answers correctly most of the time but **"probably 25% of
  the time" responds with an off-the-wall or completely incorrect
  answer**. Firefox reviews report computer-freezing issues and login
  failures (addons.mozilla.org).

### AIApply (aiapply.co)
- Credit-based auto-apply (1 credit = 1 application) bundled with resume
  builder, ATS scanner, cover letters, interview tools. Positions itself
  as quality+volume (aiapply.co comparison page).

### JobCopilot (jobcopilot.com)
- Mass auto-apply service that publishes head-to-head comparisons
  positioning itself against LazyApply (jobcopilot.com).
  Mechanic/harm specifics: UNVERIFIED from sources found — cited here
  only as a named player in the category.

### LoopCV (loopcv.pro)
- "Automation loops": scheduled apply workflows, analytics
  (Medium review, Jan 2026).

### Open-source bot ecosystem (GitHub)
- Multiple Selenium/LLM bots automate LinkedIn Easy Apply
  (mihadcm/linkedin-job-apply-bot; GodsScion/Auto_job_applier_linkedIn;
  siddharthjain094/linkedin-ai-apply). Their own READMEs warn that
  automating LinkedIn **violates the User Agreement and can get accounts
  restricted or banned**.

---

## 2. Documented harms (sourced, public)

**Candidate burn — account-level.** LinkedIn's official help pages state
that third-party software that automates activity **violates the User
Agreement**; accounts restricted for automated activity must disable the
tool before re-enablement
(linkedin.com/help/linkedin/answer/a1341387, a1340567). The restriction
mechanism is platform policy, documented — not a counted tally of bans.

**Employer burn — volume flood.** World Economic Forum, citing Greenhouse's
**2025 AI in Hiring Report** (4,100 recruiters/hiring managers):
- **34% of recruiters now spend up to half their working week filtering
  spam applications.**
- **91% have caught candidates not being honest during the process.**
- LinkedIn applications rose **45.5%** in late 2024 while postings
  **fell 10.6%**; NACE 2025: students using AI submitted ~60 applications
  on average vs. 30 overall.
(weforum.org — "How to fight AI resume spam with 'trace hiring'")

**ATS/employer countermeasures.** David Fano, CEO of **Teal**
(tealhq.com): *"Email confirmation after you apply is now a built-in
requirement in many ATS systems **because of auto-apply tools**. ...
Auto-apply may seem like a quick win, but it can flood the system with
unqualified applications, making it harder for qualified candidates to
get noticed."* — a direct causal chain from auto-apply tools to a
friction cost borne by honest applicants.

**Retaliatory AI.** Forrester (via SHRM): *"Use of bots and generative AI
by candidates will require recruiters to employ retaliatory, protective AI
in response."* WSJ (via livemint.com): bot-vs-bot war, **"Everyone is
losing."** Gartner (via eweek.com): predicts **25% of job candidates could
be fake by 2028**; eWeek: LinkedIn sees ~**11,000 applications a minute**.

**Invented qualifications.** Sonara "answers questions as though they
were the candidate" (WSJ). LazyApply Job GPT: "some answers seemed
guessed" (TechTimes). Simplify Copilot: ~25% off-the-wall answers on
open-ended questions (user review). Greenhouse: 91% of recruiters caught
dishonesty. INFERRED: each invented answer is a misrepresentation the
candidate legally and reputationally owns.

**UNVERIFIED (gaps, not claims):** no public, attributable case of a
named employer blacklisting a named candidate for auto-apply use; no
counted tally of LinkedIn bans tied to named tools; no causal study tying
the 45.5% application surge to specific products.

---

## 3. Adjacent positioning players (who owns which corner)

| Corner | Players | Mechanic | Relation to Keel |
|---|---|---|---|
| Application tracking | Teal (tealhq.com), Huntr (huntr.co), Careerflow | Kanban/table trackers, Chrome extensions, resume builders; no auto-submit | Complementary, not competitive; Keel ships its own telemetry + dashboard |
| Resume tailoring / ATS matching | Jobscan (ATS scanner, optimization), Rezi | Beat-the-ATS resume optimization | Adjacent; Keel does "truthful resume tailoring from your verified profile" (README) — honesty-first framing distinguishes it from keyword-stuffing |
| Honest-automation open source | **JobRollo** (partth101/jobrollo, GitHub) | Local-first agent (Ollama), human-gated, **"never submits, never lies"**; deliberately avoids LinkedIn/Indeed/Glassdoor because "those sites ban automated accounts"; fills every field, **stops at review** for the user to submit | **Closest positioning competitor to Keel.** Same "human gate + honesty" corner. Keel's edge: full contract (canonical answer bank + banded rules + prescreen gates + explicit-confirmation ledger + append-only telemetry + SPLIT.md open-core boundary) |
| Honest-automation open source | linkedin-ai-apply (siddharthjain094, GitHub) | Review-before-apply flow; "learns your answers" (answer-bank-like); parks captchas/login walls for human review with screenshot — "never silently mis-submitted" | Closest on answer-bank + parking semantics; narrower scope (LinkedIn-centric) |
| Counter-positioning ideas | WEF "trace hiring" proposal | Restoring effort as a trust signal in hiring | Tailwind for Keel's thesis: if applications must prove effort again, spam volume loses and verified honesty wins |

---

## 4. Keel differentiation table

Keel column grounded ONLY in talking-points.md and the public repo
(README.md). Spam-cannon column grounded in the sources above; cells
marked (INFERRED) are contrasts drawn from those sources, not quotes.

| Dimension | Spam-cannon pattern (sourced) | Keel (repo/talking-points only) |
|---|---|---|
| Who owns the answers | Sonara "answers questions as though they were the candidate" (WSJ); LazyApply "answers seemed guessed" (TechTimes); Simplify Copilot ~25% wrong on open-ended questions (user review) | **Truthfulness contract**: only ever claims what you told it is true — verified facts, canonical answer bank, banded rules for gray areas, hard gates that stop the run when a question can't be answered honestly. Never invents experience, degrees, or answers (talking-points.md; README contract §1) |
| What counts as submitted | ~1,000 applications in one night (Wired); clicks/submissions unreviewed; "no verification of job listings" (jobcopilot review) — the ledger, if any, counts sends (INFERRED) | **Explicit confirmation**: nothing counts as submitted unless the page itself confirms it; the ledger increments only on real confirmation evidence (README contract §2; talking-points.md). The public repo itself makes no submission claims |
| ATS relationship | Tools triggered platform countermeasures: email-confirmation now built into many ATS *because of auto-apply tools* (Teal CEO); LinkedIn restricts accounts for automated activity (LinkedIn Help); fingerprinting risk named by JobRollo ("sites ban automated accounts") | **No fingerprinting surface**: nothing in the public repo helps ATS vendors identify or block automated applications (README contract §4). The submission-behavior layer stays **private by design** — publishing it "would get everyone's pipeline blocked" (README; SPLIT.md) |
| Fit discipline | Applies to irrelevant roles (internships, co-founder, wrong-industry ops — iesemba.com); trades specificity for volume (Adzuna on Sonara); quantity over quality (jobcopilot review) | **100-point fit model** with banded action policy (README; engines/fit-scoring-model.md); prescreen parks unmappable required questions, essays, attestations to the input queue — never invents (README; prescreen.py) |
| Failure mode | Fires and moves on; user discovers wrong answers after the fact (INFERRED from Simplify review; LinkedIn restriction after the fact per LinkedIn Help) | **Fail closed**: ambiguity parks the lead and moves on — unverifiable postings, unmappable required questions, missing attestations: park, never proceed (README contract §3) |
| Oversight | No human gate in the loop on mass-apply tiers (LazyApply auto-submit; Sonara auto-apply) | Human gates by design: the public pipeline **stops at the launch packet** per SPLIT.md; Keel never submits an application (README "What it does NOT do") |
| Auditability | Opaque — no public telemetry of what was answered where (INFERRED; no vendor publishes an event log) | Append-only telemetry event log; outcome analytics with fail-closed reporting rules (README; log_event.py, outcome_analytics.py) |
| Open-core | Closed products; submission mechanics visible to platforms by construction (INFERRED) | Apache-2.0 public core (discovery, scoring, tailoring, answer bank, prescreen, ATS detection + capability radar, launch-packet builder, telemetry, dashboard); private execution layer protects the pipeline for all users (talking-points.md; SPLIT.md) |

---

## 5. Strongest attack lines (grounded, sourced)

1. **"They got the ATS changed on you."** Teal's CEO says email-confirmation
   is now a built-in ATS requirement *because of auto-apply tools* — the
   spam-cannon era left a friction tax on honest applicants.
   (tealhq.com)
2. **"91% of recruiters caught candidates being dishonest."** Greenhouse
   2025 AI in Hiring Report via WEF — and the bots' whole mechanic is
   answering *as though they were the candidate* (WSJ on Sonara).
   The dishonesty pipeline is the product, not a bug.
3. **"Your volume is their spam."** 34% of recruiters spend up to half
   their week filtering spam; LinkedIn applications +45.5% while postings
   fell 10.6% (WEF/Greenhouse/LinkedIn data).
4. **"0.5% interview rate."** Wired's LazyApply test: ~1,000 applications
   in a night → ~20 interviews (0.5%) vs. 10% manual. Volume doesn't
   convert.
5. **"LinkedIn will restrict your account."** Official LinkedIn policy —
   third-party automation violates the User Agreement (LinkedIn Help).
   The risk is documented by the platform itself.
6. **"Their wrong answers are your lies."** Simplify Copilot ~25% wrong on
   open-ended questions; LazyApply answers "guessed." Every invented
   answer is a misrepresentation the candidate owns.

## 6. Gaps — where Keel has no evidence yet

1. **No head-to-head conversion data.** Keel's proof figure is
   90 verified submissions (ledger-verified 2026-09-15 14:28 PT) — a volume figure from the
   private pipeline, not a comparison vs. spam cannons or vs. manual
   applying. The claim "discipline converts better" is asserted, not
   measured. Keel's own outcome analytics (outcome_analytics.py) is the
   instrument that could close this — needs decisive-outcome volume.
2. **The honest-automation corner is not unique.** JobRollo
   ("never submits, never lies," human-gated, open source) and the
   linkedin-ai-apply repo (review-before-apply, learned answers, parked
   captchas) already occupy adjacent honest ground. Keel's defense is the
   *full* contract (answer bank + banded rules + prescreen gates +
   explicit-confirmation ledger + telemetry + SPLIT.md boundary) — that
   needs to be the claim, not "honest automation" alone.
3. **No attributable candidate-burn cases.** Harm evidence is
   policy-level (LinkedIn restrictions) and survey-level (Greenhouse).
   There is no named employer that blacklisted a named candidate for bot
   use, and no counted tally of bans on named tools. Candidate-facing
   attack lines should stay at the policy/survey level until real cases
   are sourced.
4. **Execution-layer efficacy is private and unverified publicly.** The
   repo's central promise — that the private execution layer avoids
   fingerprinting — is asserted via SPLIT.md design, not demonstrated in
   the public repo. The 55-verified-submissions figure is the only public evidence it works at all.
5. **Numbers are 2024–2025 vintage.** The headline stats (45.5%,
   34%, 91%, 11,000/min, 0.5%) come from 2024–2025 reporting; freshness
   caveat if opponents cite newer data.

---

## 7. Open questions

- Should Keel name names (LazyApply, Sonara, Simplify) in public copy,
  or keep the attack generic ("spam cannons")? Naming invites rebuttal
  and review-bomb dynamics; generic keeps the Show HN post above the fray.
- Does the 55-verified-submissions figure risk sounding like a spam-cannon boast?
  Current framing ("traction figure, not the product") handles this — keep
  it verbatim everywhere.
- JobRollo is the closest credible competitor: evaluate whether to cite
  it as validation of the corner ("we're not alone") or leave it unmentioned.
- Teal's CEO quote is the single strongest third-party endorsement of the
  thesis — check it's acceptable to quote in HN comments (it's a public
  blog post; standard practice allows quoting with attribution).

---

## Source list (public, citable)

- WSJ via livemint.com — "AI bots are taking over the job application
  process. Everyone is losing."
  http://livemint.com/technology/ai-bots-are-taking-over-the-job-application-process-everyone-is-losing-11715504962260.html
- TechTimes — LazyApply review (Wired's 1,000-in-a-night test, guessed
  answers).
  https://www.techtimes.com/articles/298368/20231106/lazyapply-ai-bot-help-apply-jobs-easily-reliable.htm
- jobcopilot.com — LazyApply review (2.1★ TrustPilot, quantity-over-quality).
  https://jobcopilot.com/lazyapply-best-alternative/
- iesemba.com — independent LazyApply test (irrelevant applications).
  https://iesemba.com/2023/11/i-tested-lazyapply-the-bot-that-applies-to-jobs-automatically/
- adzuna.co.uk — Sonara review 2026 (volume trade-offs).
  https://www.adzuna.co.uk/blog/sonara-ai-review-2025/
- extpose.com — Simplify Copilot review (~25% wrong open-ended answers).
  https://extpose.com/ext/pbanhockgagggenencehbnadejlgchfc
- aiapply.co — AIApply vs Sonara comparison (mechanics, pricing).
  https://aiapply.co/compare/aiapply-vs-sonara
- tealhq.com — Teal's Sonara review + CEO David Fano quote on ATS
  countermeasures. https://www.tealhq.com/post/sonara-review
- LinkedIn Help — prohibited software/extensions; automated activity
  restrictions. https://www.linkedin.com/help/linkedin/answer/a1341387
  and https://www.linkedin.com/help/linkedin/answer/a1340567
- weforum.org — trace hiring piece citing Greenhouse 2025 AI in Hiring
  Report (34% / 91% / +45.5% / −10.6% figures).
  https://www.weforum.org/stories/jobs-and-the-future-of-work/how-trace-hiring-can-reclaim-human-authenticity-in-the-age-of-ai/
- eweek.com — LinkedIn 11,000 applications/minute; Gartner 25%-fake-by-2028.
  https://www.eweek.com/news/ai-job-applications-linkedin/
- SHRM — Forrester "retaliatory, protective AI" prediction.
  https://shrm.org/ResourcesAndTools/hr-topics/technology/Pages/employers-fooled-by-bot-applicants.aspx
- jobscan.co — Jobscan vs Huntr (ATS usage stats, adjacent corner).
  https://www.jobscan.co/blog/jobscan-vs-huntr/
- careerflow.ai — Huntr vs Teal vs Careerflow (tracker corner).
  https://www.careerflow.ai/blog/huntr-vs-teal-vs-careerflow
- github.com/partth101/jobrollo — honest-automation adjacent player.
- github.com/siddharthjain094/linkedin-ai-apply — honest-automation adjacent.
