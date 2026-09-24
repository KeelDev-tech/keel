# Comparator Design Teardowns + Gaps in Ours

**Author:** Keel Competitive Intel, 2026-09-19. Research: four parallel teardown agents, public pages only, no signups/trials/payments.
**Evidence standard:** every competitor claim below is **ADVERTISED** (from the vendor's own public pages, with URLs) — never measured quality, never repeated as fact. Third-party-sourced facts are labeled. Keel's own positions are labeled CODE / TESTED / OPEN / BLOCKED / SHELVED per the spec matrix.
**Purpose:** "flush out their design" — understand how each competitor's product is architected as a system, not just its feature list — "and find gaps in ours" — name where Keel's *design* (not just feature parity) falls short, with each gap's evidence status.

---

## PART 1 — The four designs, flushed out

### 1. JobCopilot (jobcopilot.com)

**Design thesis:** volume-first automation with a training-gated trust ramp.

The core object is the **copilot** — one per search segment (one resume, one filter set, one writing style per copilot; Elite buys 3). The daily batch is the heartbeat: every day it finds matches on **500,000+ official company career pages** (explicitly not job boards — anti-fake-listing positioning) and either auto-applies or pre-fills for review. The daily ceiling (20 matches/day Premium, 50/day Elite) is the central product primitive: it is simultaneously the matching quota, the application cap, and the pricing lever.

The trust ramp is the cleverest design move: **review mode is framed as the training phase for auto-apply, not a permanent mode.** You start reviewing and correcting; after 5–10 edits the copilot "learns your writing" and you flip to auto-apply. Edits compound into a personal answer model. The **Chrome extension** is the escape hatch for everything outside the career-page corpus — and there it deliberately *withholds* auto-submit: fill only, user clicks submit. Two trust models in one product: fully autonomous on "verified" employer sites, assisted-only everywhere else.

**Pricing (ADVERTISED, official page):** Premium "starts from $0.93/day" (1 copilot, 20 matches/day); Elite "starts from $1.05/day" (3 copilots, 50 matches/day, per-application resume tailoring, undisclosed quantity of hiring-manager contact credits). Weekly/monthly/quarterly billing; checkout totals unverified. Career tools bundled (mock interviews, career advisor, salary negotiation advice).

**Where their design is honest:** they admit mass-apply is user-configurable and hedge volume against quality in their own copy. They admit the agent can't verify everything (no employer-portal accounts claimed; bot-blocked sites redirected to manual).

**Where their design stops (from their own pages):** no blocker-resolution surface beyond a one-time setup questionnaire and a "enter 1 for salary" hack; no submission-receipt model; no outcome-based learning (matching improves via edits only); no interview-stage workflow beyond a mock-interview tool; employer-side verification ("verified jobs") asserted with no published methodology; the Elite credit count is undisclosed.

Sources: jobcopilot.com/pricing/, /automate-job-applications/, /ai-apply/, /chrome-extension/, /ai-agent-job-applications/, /best-internship-sites/, /jobseekers/, /career-change-advisor/

### 2. Jobright Agent (jobright.ai)

**Design thesis:** a two-sided marketplace where the jobseeker product is also the employer's inventory.

The agent is the headline ("the first AI career agent," launched June 24, 2025) — resume upload → proactive matching from an advertised 8M-job corpus → per-job tailored resume → one-click apply → tracking. But the more important design fact is structural: Jobright's **employer product** ("The AI recruiter for tech companies") sources from the *same 2M-professional network* the jobseeker product builds. The candidate's profile is simultaneously application material and employer sourcing inventory. That's a real flywheel — and an undisclosed consent/scope tension (nothing public addresses the dual use).

Design details worth noting: **Orion**, a 24/7 AI coaching layer (fit explanations, skill-gap insights, interview prep) retained from the pre-agent product — a hedge against automation distrust that also generates engagement the funnel doesn't. **Insider Connections** finds alumni/hiring managers at target companies with outreach templates — networking treated as a first-class product feature, not an afterthought. Two execution routes: a browser-less Agent (auto-apply) and a free 1-click Chrome extension (autofill only) — the dual routes quietly admit the agent's coverage is bounded (founders told The Register it covers US tech/education/government; "thousands of ATS platforms" is claimed only for the extension).

**Pricing (candidate):** not published on any public page — the price appears at signup/upgrade. First-party blog lists $29.99/mo (July 2025); third-party 2026 reviews report a "Turbo" tier at $39.99/mo — both unverified. Employer side: $499/month per active role, 2-week free trial, cancel anytime (jobright.ai/employers).

**Stat discipline (theirs, observed):** 6-second vs 10-second resume-tailoring claims on different pages; 520K professionals at launch → 2M+ now with no reconciliation; "8,000,000+ jobs" and "400,000+ new today" identical across pages with no freshness definition; "80% of job seekers" and "35% vs <5% interview rate" claims with no methodology. This is the format Keel must never mirror.

Sources: jobright.ai/ai-agent, jobright.ai/orion-copilot, jobright.ai/employers, jobright.ai/blog (several), theregister.com (founder interview 2025-06-24)

### 3. LoopCV (loopcv.pro)

**Design thesis:** volume-first closed loop — scan → submit → track → tweak — with outreach as a *separate second funnel* and distribution as a *second business*.

The core object is the **Loop**: a saved job search with its own auto-apply toggle, running against 30+ aggregated boards daily (hourly on the Done-For-You tier) on LoopCV's servers "even when the user is offline." A Master Auto-Apply kill switch governs globally. The **Questions tab** is their human-in-the-loop answer: every screening question encountered is collected, answer-once-reuse-forever, unanswerables visibly parked until answered manually (AI Answering is Premium+ only, and their own FAQ recommends manual answering first to "feed the algorithm" — the automation's accuracy rests on unpaid user labor with no advertised correctness measurement).

Three things are genuinely more advanced than the other comparators: (1) **recruiter outreach is a parallel channel** — Email Finder for recruiter addresses, templated sends with CV attached, fired even where the form can't complete, with open/reply analytics; (2) the **tracker is free forever** and rich (Kanban through Hired, interview tracker with thank-you/follow-up reminders, response-rate analytics by board, CV-version A/B); (3) **distribution is productized** — white-label for coaches, multi-client dashboards, a public REST API + webhooks, and an MCP server exposing `search_jobs` / `apply_to_job` to AI agents. They monetize the engine, not just the seat.

**Pricing (ADVERTISED):** free forever plan (no card); paid from €9.99/month officially; third-party listings show Standard €8.99 and Premium €14.99 with credit quotas (100 / 300 apps+emails per month), Done-For-You €87.99/month with hourly collection and advisory calls. Lifetime $39 deals on reseller sites. Their own blog admits the agent "can't complete every portal" (multi-page Workday custom screeners) — honest, but it concedes execution depth.

**Where their design is weakest (from their own pages):** credit-metered everything means the business model incentivizes *more applications* — the exact wrong-behavior incentive Keel rejects; the quota ceilings (300 apps/month on top consumer tier) make the volume philosophy explicit; platform stats ("1,495,000+ jobs collected daily," "50% more interviews") repeat with no methodology; the paid candidate price is not on any public page.

Sources: loopcv.pro/autoapply/, /ai-job-application-agent/, /ai-question-answering/, /pricing/, /job-application-tracker/, /career-advisors/, /developers/, /lazyapply-alternative/

### 4. Simplify Copilot (simplify.jobs) — adjacent free baseline

**Design thesis:** the one-profile → four-function flywheel, semi-automation by design.

One centralized profile → ranked matches from 50+ boards/career pages (+ curated "playlists") → free browser-extension autofill on 100+ ATS portals (user always reviews and submits — no auto-apply on the free tier) → AI writing help at decision points (1-click answers from resume+history, resume scoring with keyword gaps) → every submission auto-saved to a tracker. Unanswerable questions: the user's answer is saved and reused on similar questions — incremental per-user learning.

**The price floor (ADVERTISED, official FAQ):** "Simplify is free for job seekers — matches, autofill, and the tracker included," and the copilot FAQ commits: **"There is no limit on how many applications you can autofill."** No application cap. Revenue from Simplify+ and employers. Simplify+ (pricing from in-app/third-party, not a public page): $19.99/week, $39.99/mo, $89.99/qtr — the priciest of the three paid offerings per day.

**What this means for Keel:** form filling, application tracking, basic resume/keyword analysis, and discovery feeds are all **$0 with unlimited volume**. A paid product cannot charge for plumbing; Simplify+ itself monetizes *AI writing labor* (tailored bullets, cover letters, custom-question answers) plus networking suggestions. Keel's premium must live in *judgment* (what to apply to, verified eligibility, constraint accuracy) or *execution* (verified completion, receipts, recovery) — never in workflow infrastructure. Also note their trust tension: the extension requests `debugger` and all-host permissions — a server-side/API-first form-filling architecture is a genuine privacy differentiator, still unexploited by anyone.

Their ceiling is also our opening: no auto-apply, no submission receipt, no eligibility story, no outcome measurement (their "25% more hear-back" and "3× interview rate" claims carry no methodology). Their free tier defines the floor; their design leaves the evidence gap completely open.

Sources: simplify.jobs, simplify.jobs/copilot, wobo.ai review, remotejobassistant.com, adzuna.co.uk review, chrome-stats.com extension record

---

## PART 2 — The cross-cutting view: what nobody has, what everyone assumes

**Nobody advertises:** a submission-receipt model (proof the application arrived); answer provenance/scoping with expiry; constraint enforcement across the whole journey; interview-outcome cohorts with denominators and uncertainty; a real-dollar cost-per-completion; an execution-failure recovery design (LoopCV's honest "we surface what we can't complete" is the closest). This is Keel's white space — and it is white because it is hard, not because it's a secret.

**Everyone assumes (and nobody evidences):** their matching is "relevant" (no published accuracy methodology anywhere); their volume numbers mean something (all scale stats are unverified marketing); their learning loop improves outcomes (JobCopilot's edit loop and LoopCV's question reuse improve *answers*, not *results*; nobody closes the outcome loop publicly).

---

## PART 3 — Gaps in OUR design

Each gap is named as a design gap (not a feature checklist item), with what the comparator design proves and our current status.

### G-1. No user-facing recurring machine
LoopCV's Loop and JobCopilot's copilot give users a visible recurring object — saved searches that run on a cadence, per-search controls (toggles, filters, kill switches), inspectable run history. Keel has refill logic, launch locks, cooldowns, and the never-halt lane — all *engine* machinery, none of it visible or controllable by a second user. Our READY pool is a supply controller the user never sees.
**Status: OPEN.** The canonical readiness evaluator + visible shortage states (SUPPLY_LIMITED / PROVIDER_LIMITED / AWAITING_DECISION) are designed but not shipped; no user-facing recurring-search object exists. Ties to T1 and the usability pilot (T2).

### G-2. No execution escape hatch as a designed surface
Every comparator ships a second execution surface for unsupported routes: JobCopilot's fill-only extension, LoopCV's browser-assisted route, Simplify's always-review autofill. Keel's browser path exists in the engine, but there is no designed assisted-completion surface — no sandboxed form review, no explicit unsupported-route handling a user can see. F43/F46 (supported-route coverage) are still open, and unsupported routes are not *visible* anywhere.
**Status: OPEN.** The supported-provider matrix is a queued intel item; the assisted surface is unbuilt.

### G-3. Blocker resolution is our strongest mechanism and our weakest surface
Keel's tray — family-grouped cards, bank-draft suggestions, NEEDS-YOU vs SYSTEM-BLOCKED taxonomy, aging, recurrence, tray-answer retro-resolution — is deeper than anything any comparator advertises (JobCopilot has a setup questionnaire; LoopCV has a Questions tab; Simplify saves per-question answers). But it is operator tooling, not a product surface: a new user in the 15-minute usability test would never find it. The mechanism compounds; the surface doesn't exist.
**Status: CODE-complete mechanism, OPEN surface.** Tied to T2 (8/10 unaided) — the guided first-run is the acceptance event.

### G-4. The pipeline ends at submission; theirs continues to interview
LoopCV has an interview tracker (rounds, thank-you/follow-up reminders) and response analytics; Jobright retains Orion as an interview-prep and skill-gap coaching layer. Keel's application-to-interview workflow — prep generated from the exact resume/answers/JD used — is a market-plan *opportunity*, not a design. The moment an application becomes a real opportunity, our product currently goes silent.
**Status: OPEN.** Outcome ingestion with consent (T5) and the interview-prep continuity are both future work.

### G-5. No productized cost story — theirs is published (even if weak), ours is unmetered
Every comparator publishes *some* price. Simplify proves AI writing is what candidates will pay for, not plumbing. Keel has no price, no published cost model, and composite cost units that are explicitly not dollars. Worse: without attempt-level metering we cannot even price ourselves honestly, let alone prove the "published cost per verified eligible completion" (T3) that is supposed to be a differentiator.
**Status: OPEN / BLOCKED-adjacent.** Attempt-level metering is Build's weeks-4–6 work; a bounded paid active-search plan (market plan §9) is designed, not priced.

### G-6. Discovery breadth is a design input we haven't productized
JobCopilot's 500K career-page corpus, LoopCV's 30+ boards, Jobright's advertised 8M jobs — all unverified, but they are *named* and *shaped* (boards vs career pages vs two-sided network). Keel's discovery is pipeline-internal; there is no declared corpus, no freshness contract a user could read, no segment-specific source strategy (one occupation/geography as market plan §6 requires). Source freshness tracking exists in code; it is not a product promise.
**Status: OPEN.** Tied to the focused segment choice (still open — the pilot plan is shelved) and F43/F46.

### G-7. Volume controls are internal levers, not user policy
Match ceilings (20/50/day), per-loop auto-apply toggles, Master kill switch, credit quotas — comparators expose volume as *user policy*: the user sets the aggression. Keel's launch locks, verify cadence, and refill watermarks are operator controls. Given our standing rule (never lower eligibility to fill inventory), user-settable bounded plans are the honest version of quotas — designed in the market plan, not built.
**Status: OPEN.** Tied to T1 shortage states and the paid-plan design.

### G-8. No outcome feedback loop in the product
LoopCV at least advertises response-rate analytics and CV-version A/B (unverified quality, but the loop exists as a product concept). Keel's telemetry is richer (edge cases, learning proposals, sieve) — and none of it is user-facing. The learning loop serves the *engine*, not the *user*. T5 (interview cohorts) is the measurement; the productized feedback (what's working for *me*, from my history) doesn't exist.
**Status: OPEN.** Tied to outcome ingestion with consent, T5, and the dashboard work.

### G-9. Distribution is their second business; ours is a deliberate blank
LoopCV's white-label/coach dashboard/API+MCP and Jobright's live two-sided employer marketplace are structural advantages, not features. Keel defers distribution by strategy (market plan: "expansion to defer") — the right call — but the gap should be named as a *strategic deferral*, not mistaken for parity. When grants land, `funded-sidebar.md` should treat distribution as a funded unlock, and the coach/API route should be re-evaluated then.
**Status: STRATEGIC DEFERRAL (named).** Not a defect today; a moat we are choosing not to build yet.

### G-10. Privacy architecture is unexploited by everyone — including us
Simplify's extension permission set (`debugger`, all-host access) is the inherent tension of extension-based autofill, and none of the three core comparators addresses it in their design. Keel's answer is architectural — ATS API paths and a server-side browser path instead of a user-installed extension — but it is not productized, not measured, and not communicated. An advertised, measured privacy advantage (no browser extension required, scoped credentials, restricted connector boundaries) is sitting in our design unclaimed.
**Status: OPEN — opportunity, not defect.** Tied to F27–F33/F38/F39 (trust perimeter) and the Build queue.

---

## PART 4 — What changes in the spec matrix

This teardown deepens `spec-matrix.md` without contradicting it:

1. **Parity table stands**, but two rows get sharper status: the "guided user workflow" row is now G-1/G-3 (recurring machine + blocker surface), and "cost visibility" is now G-5. No advertised comparator capability was found that forces a new parity row — the bundle the market analysis named still covers it.
2. **The differentiation section gains its proof-of-absence:** none of the four products advertises submission receipts, answer provenance/scoping, constraint enforcement, outcome cohorts, or real-dollar cost per completion — the white space Keel is built to occupy is confirmed across all four designs, not assumed.
3. **The "what we do NOT claim" list grows by one:** never mirror their unverified-stat format (Jobright's drifting user counts, LoopCV's "50% more interviews," Simplify's "3× interview rate"). Our credibility is the differentiator; their format is the anti-pattern.
4. **New strategic-deferral row:** distribution (white-label/coach/API, two-sided marketplace) — deliberate blank, re-evaluate when funded.

## Sources — master citation list

- JobCopilot: https://jobcopilot.com/pricing/ · https://jobcopilot.com/automate-job-applications/ · https://jobcopilot.com/ai-apply/ · https://jobcopilot.com/chrome-extension/ · https://jobcopilot.com/ai-agent-job-applications/ · https://jobcopilot.com/best-internship-sites/ · https://jobcopilot.com/jobseekers/ · https://jobcopilot.com/career-change-advisor/
- Jobright: https://jobright.ai/ai-agent · https://jobright.ai/orion-copilot · https://jobright.ai/employers · https://jobright.ai/blog/ · https://www.theregister.com/software/2025/06/24/ai-may-take-your-job-or-this-ai-agent-might-get-you-one/693869
- LoopCV: https://www.loopcv.pro/autoapply/ · https://www.loopcv.pro/ai-job-application-agent/ · https://www.loopcv.pro/ai-question-answering/ · https://www.loopcv.pro/pricing/ · https://www.loopcv.pro/job-application-tracker/ · https://www.loopcv.pro/career-advisors/ · https://www.loopcv.pro/developers/
- Simplify: https://simplify.jobs · https://simplify.jobs/copilot · https://www.wobo.ai/blog/simplify-review/ · https://www.remotejobassistant.com/blog/simplify-jobs-review · https://chrome-stats.com/d/pbanhockgagggenencehbnadejlgchfc

**Method note:** public pages only, 2026-09-19, no signups/trials/payments/contact. Paid-tier figures for Simplify and LoopCV tiers are third-party-attributed where the vendor's own pages don't publish them. Every competitor capability above is ADVERTISED, never measured. Comparator product trials remain BLOCKED until Trent's explicit word.
