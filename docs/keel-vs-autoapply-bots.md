# Keel vs auto-apply bots

A factual comparison of Keel against commercial auto-apply tools. Every
competitor claim below is either cited to a public source or explicitly
marked **unverified/omitted**. Keel's own claims link to the engine code
that enforces them, so they can be checked in this repo.

**Disclosure:** several of the best public comparisons of this category
are written by vendors of competing products. Where a source has a
financial interest, that is noted. Keel's position in this document is
also self-interested; read the citations, not the adjectives.

*Research date: 2026-09-22.*

---

## The one question that separates the category

When a tool says your application was "sent", what does that word mean?
For most of the category, "sent" means "our automation clicked a submit
button and nothing crashed" — nobody checked whether the form actually
went through, whether a CAPTCHA silently ate it, or whether the posting
was already closed. (This framing comes from a 2026 comparison written
by the founder of AI Applyd, a competing product — disclosed interest:
<https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82>.)

Keel's answer: a submission counts only on **explicit confirmation
evidence** — the confirmation the employer's own system shows
(`engines/submit_intent.py::mark_submitted` raises `ValueError` on empty
confirmation text). An ambiguous attempt goes UNKNOWN and bars any retry
on any transport, so a duplicate application cannot be risked. That is
the whole row Keel claims as its difference.

---

## Feature / truthfulness comparison

|  | Keel | Simplify Jobs | LazyApply | LoopCV | JobCopilot | Sonara |
|---|---|---|---|---|---|---|
| Auto-submits applications | Only with explicit confirmation evidence, human-run executor; nothing counts otherwise | **No** — autofill only; you review and click submit yourself (source: [remotejobassistant.com](https://www.remotejobassistant.com/blog/simplify-jobs-review)) | Yes — mass-applies LinkedIn Easy Apply from your browser session (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | Yes — applies via job boards and by emailing your CV (source: same comparison) | Yes — searches and submits daily, up to 50/day on top plan (source: same comparison) | Yes — promises "apply until you're hired" with no manual intervention (source: [fastapply.co](https://blog.fastapply.co/auto-apply-jobs-tools-compared-2026)) |
| What "submitted" means | Quoted confirmation text from the employer's system; graded evidence (202 quoted / 3 pointer / 1 URL-only / 3 unevidenced of 209 — see [honesty report](https://github.com/KeelDev-tech/keel/blob/main/site/honesty-report.html)) | You clicked submit — no verification question because you are the verification (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | **Unverified** — no public documentation of a verification step found | **Unverified** — email/boards path has no receipt step (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | **Unverified** — no public documentation of a verification step found | **Unverified** — users describe it as a "black box"; a journalist's paid batch fell well short of promised volume (source: [fastapply.co](https://blog.fastapply.co/auto-apply-jobs-tools-compared-2026)) |
| Answers unknown form questions | **Abstains** — unanswerable questions are reported, never invented (`engines/answer_resolver.py`: fail-closed ABSTAIN; refusal values cannot be rendered as answers) | N/A — you answer everything yourself | Reportedly generates answers with ChatGPT, with "predictable hallucinations" (source: [Medium review, unverified independently](https://medium.com/@rcsalsbury2404/thats-because-lazyapply-it-a-terrible-piece-of-software-3161d1277d27)) | **Unverified/omitted** | "Reddit occasionally reports embellished qualifications in its generated answers" (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | **Unverified/omitted** |
| Handles unverifiable postings | **Parks** — unverifiable postings are never pursued blind (`engines/prescreen.py`; "unverifiable" gate: 15 leads stopped) | N/A — you review each one yourself | Reportedly applies regardless of fit ("any posting that remotely resembles a person's area") (source: [Medium review, unverified independently](https://medium.com/@rcsalsbury2404/thats-because-lazyapply-it-a-terrible-piece-of-software-3161d1277d27)) | **Unverified/omitted** | **Unverified/omitted** | **Unverified/omitted** |
| Duplicate-submission protection | Intent state machine: one open attempt per role; ambiguous attempts never retried or switched transports (`engines/submit_intent.py`) | N/A (human submits) | **Unverified/omitted** | **Unverified/omitted** | **Unverified/omitted** | **Unverified/omitted** |
| Refusals logged and published | Yes — 3,427 leads stopped by gates, published per-gate in [docs/geo/stats.json](https://github.com/KeelDev-tech/keel/blob/main/docs/geo/stats.json) | N/A | **Unverified/omitted** | **Unverified/omitted** | **Unverified/omitted** | **Unverified/omitted** |
| Open source | Apache-2.0, this repo | **Unverified** (proprietary extension; free tier) | Proprietary | Proprietary | Proprietary | Proprietary |
| Pricing | Free, no paid tier, no quotas | Free tier (autofill, tracking); Simplify+ $39.99/mo for AI features, no annual plan, no free trial of paid tier (source: [remotejobassistant.com](https://www.remotejobassistant.com/blog/simplify-jobs-review), prices checked on simplify.jobs 2026-03-10) | Lifetime deal historically ~$130 (source: [levels.fyi thread](https://www.levels.fyi/community/thread/At200r/anyone-used-lazyapply)); current pricing **unverified** | Free plan; paid from about €10/mo (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | Paid, no free tier; plans reported ~$19–49/mo (source: [third-party research notes](https://github.com/nickdelicto/ai-resume-builder/blob/HEAD/docs/FUTURE-AUTO-APPLY-FEATURE.md)) — **unverified current** | $2.95 trial then $23.95/mo base (source: [fastapply.co](https://blog.fastapply.co/auto-apply-jobs-tools-compared-2026)) — **unverified current** |
| Public reputation signal | — | 1M+ Chrome installs, 4.9/5 Chrome Web Store (source: [remotejobassistant.com](https://www.remotejobassistant.com/blog/simplify-jobs-review)); paid tier 3.0/5 on Trustpilot (n=9) with billing complaints | ~2.1–2.4 on Trustpilot with billing/reliability complaints (sources: [jobcopilot.com vendor page](https://jobcopilot.com/lazyapply-best-alternative/), [AI Applyd comparison](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)); parent company renamed Trustpilot page to "PEVE VISIONS" (source: jobcopilot.com vendor page) | **Unverified/omitted** | "Solid Trustpilot rating, though heavily invited reviews" (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | Temporary service discontinuation reported mid-2025 (source: [fastapply.co](https://blog.fastapply.co/auto-apply-jobs-tools-compared-2026)) |
| Platform-ToS posture | Human-run execution only; no automation inside personal account sessions | Runs in your browser, you submit | Automation inside your own LinkedIn session is against LinkedIn's terms and has reportedly triggered account restrictions (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)) | **Unverified/omitted** | **Unverified/omitted** | **Unverified/omitted** |

---

## Also in the category (not tabled)

- **Teal** — an organize/track tool (bookmark, track, autofill assist), 4.9 on the Chrome Web Store; explicitly *not* an auto-apply bot — the human owns the submit button (source: [product-teardowns](https://github.com/pz0227/product-teardowns/blob/HEAD/jobright-ai/teardown.md)).
- **AutoApplyMax** — open-source core with a Chrome Web Store full version; does auto-submit; AI features reported at $9.90/mo (source: [third-party research notes](https://github.com/joeyspagnoli/agentic-job-applier/blob/HEAD/.research/2026-05-22-204703-autonomous-apply-north-star/artifacts/search-002-simplify-competitors-and-gaps.md)).
- **Massive** — iOS swipe-to-apply; reviewers report invented credentials and trial auto-billing with cancellation difficulty; Trustpilot 2.1 (source: [AI Applyd comparison, vendor-disclosed](https://medium.com/@firstexhotic/i-compared-6-auto-apply-tools-only-one-proves-your-application-arrived-ce017a20fe82)).
- **Jobright** — agentic (matches, tailors, fills, can complete submission); independent product teardown documents answer-accuracy failure classes and notes LazyApply's public reviews independently reproduce them (source: [product-teardowns](https://github.com/pz0227/product-teardowns/blob/HEAD/jobright-ai/teardown.md); author notes competitors were not independently audited).

---

## What this document does not claim

- No interview rates, no callback rates, no "X% more interviews" — Keel does not publish outcome-rate claims (rates would be estimates; see the [honesty report](https://github.com/KeelDev-tech/keel/blob/main/site/honesty-report.html)).
- Competitor internals are described only where a public source describes them; everything else is marked **unverified/omitted** rather than inferred.
- Keel's own figures (209 verified submissions, 3,427 gate stops) are recounts of the private production pipeline, not claims the public repo produced them — see the honesty report's checkability limits.
- Corrections welcome: if a cited claim is wrong or stale, the fix is a PR with a better source.
