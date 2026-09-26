# OSS Growth Playbook for Keel

Researched 2026-09-22 across 8 explosive-growth OSS projects (n8n, Cal.com, Supabase, PostHog, AppFlowy, Documenso, LangChain, ToolJet). Every pattern below is tied to a specific project and source. Keel's current state for reference: 0 stars, demo GIF + 5-min stdlib quickstart already in README, Apache-2.0, Show HN planned Tue/Wed 5:00–6:30 AM PT.

## 1. README conversion mechanics

- **One-line value prop + visual in the first screen, no scrolling to try it.** n8n: banner + "The Platform for AI Agents and Workflow Automation" + screenshot + one-line Docker install. (https://github.com/n8n-io/n8n)
- **Copy-to-value quickstart, not a setup walkthrough.** LangChain's README opens with `uv add langchain` + a 6-line snippet that returns a real result. (https://github.com/langchain-ai/langchain)
- **Explicit star CTA baked into the README.** ToolJet: "If you find ToolJet useful, please consider giving us a star." AppFlowy has a how-to-star GIF section. (https://github.com/ToolJet/ToolJet, https://github.com/AppFlowy-IO/AppFlowy)
- **Convert launch wins into permanent social proof the same day.** Cal.com's founder added the HN #1 badge to the README the day they hit it; Documenso's README carries its Product Hunt #1 badges permanently. (https://news.ycombinator.com/item?id=34507672, https://github.com/documenso/documenso)
- **Badge row as activity proof:** contributors, "PRs Welcome", Docker pulls, commit activity (PostHog); stars + commits-per-month (Cal.com). (https://github.com/PostHog/posthog)
- **Mission section pre-empts the trust question.** AppFlowy's "Why Are We Building This?" sits mid-README for a data-sensitive tool — Keel's honesty framing belongs here, not just in features. (https://github.com/AppFlowy-IO/AppFlowy)

## 2. Launch mechanics

- **HN is the spike; sequence everything else around it.** ToolJet's same-day blitz: Product Hunt yielded 70 signups/50 stars in 6h; HN hit #1 within an hour → 1,000+ stars in 8 hours. PH alone underperforms — never open with it. (https://blog.tooljet.com/building-and-launching-tooljet/)
- **Order: HN first, Reddit niche subs, PH as second wave.** Documenso: HN own-voice, no superlatives, link straight to repo; PH as a +300%-style second bump. (https://github.com/documenso/documenso)
- **Title pattern that works: "Show HN: [Project] — [open X alternative / technical differentiator]".** AppFlowy: "An Open-Source Notion Alternative Built with Flutter" — differentiator in the title, not just "clone". n8n's launch post was third-party; the founder's first comment was a personal build story inviting contributions. (https://github.com/AppFlowy-IO/AppFlowy, https://news.ycombinator.com/item?id=21191676)
- **Founder answers every comment all day, especially hard trust questions.** Cal.com's cofounders answered calendar-scope objections in-thread and turned them into trust ("verifiable because it's open source"). (https://news.ycombinator.com/item?id=34507672)
- **Launch gate is practical, not calendar-based.** PostHog: "make the product usable by strangers without hand-holding before launch" — they launched 5 weeks in, when that was true. (https://PostHog.com/founders/first-1000-users)
- **Recurrence beats one-shot.** Supabase runs Launch Week every 3–4 months (one feature/day, format open-sourced as launchweek.dev); PostHog's founder journey posts re-hit HN repeatedly; ToolJet 2.0 relaunched via r/selfhosted. Plan launches #2 and #3 before #1. (https://github.com/supabase-community/launchweek.dev/blob/HEAD/n/rorstro.mdx, https://dev.to/tooljet/how-tooljet-gained-20000-github-stars-and-400-contributors-4ee0)

## 3. Contributor flywheel

- **Merge community PRs within days from birth.** LangChain merged external PRs #8/#9/#18/#24 in its first week → 4,500 contributors. Responsiveness is the signal. (https://www.latent.space/p/langchain?ref=blog.langchain.dev)
- **Two-tier good-first-issue labels.** AppFlowy: "good first issue for devs" AND "for experienced devs" + named mentors + comment-claim triage. (https://github.com/appflowy-io/appflowy-docs/blob/HEAD/essential-documentation/contribute-to-appflowy/contributing-to-appflowy.md)
- **Plural contribution paths, not just code.** n8n: workflow templates via Creator program, community npm nodes, tutorials — 7,700+ community templates became the marketing. Cal.com: paid bounties board (Algora) for first PRs. (https://docs.n8n.io/contribute, https://github.com/betagouv/gitscan/blob/HEAD/repos/betagouv/cal.com-test/README.md)
- **Public roadmap + visible activity.** Cal.com's public roadmap and ~19 external contributors/week signaled a living project. PostHog's README links roadmap voting + beta early access as the on-ramp. (https://dev.to/opensauced/how-does-calcom-check-out-e5b, https://github.com/PostHog/posthog)
- **Name early contributors publicly.** ToolJet credited its first five contributors by name in the launch post. (https://blog.tooljet.com/building-and-launching-tooljet/)

## 4. Docs & demo

- **Self-host IS the demo.** n8n, Cal.com, ToolJet, Documenso all lead with one-command Docker; Documenso documents Docker/Compose/Railway/K8s + a troubleshooting guide. Privacy-conscious devs convert on the self-host path. (https://github.com/ToolJet/ToolJet, https://github.com/documenso/documenso)
- **Templates as a working demo layer.** n8n's 9,000+ in-product templates; LangChain Hub for shareable prompts. (https://github.com/n8n-io/n8n)
- **Docs as a distribution channel.** Supabase's Head of Growth: "If a developer can clearly see how your developer experience can help their workflow then using your product becomes an obvious choice." (https://Dev.To/fmerian/how-dev-first-startup-supabase-grew-from-0-to-50k-github-stars-5d4d)
- **License boundary stated upfront.** PostHog: repo is MIT except `ee/`, plus a `posthog-foss` mirror — stated in the README, never discovered. (https://github.com/PostHog/posthog)

## 5. What NOT to do

- **Don't over-claim vs incumbents before parity.** AppFlowy's star count drew a "quite crazy compared to the quality of the app" backlash thread — the hype-reality gap is where cynicism forms. (https://forum.yunohost.org/t/appflowly-an-opensource-alternative-to-notion-so/19931)
- **Don't oscillate on license — settle it before launch.** n8n's fair-code license fuels a permanent "is not open source" critique cycle; Cal.com going closed-source in 2026 triggered a 391-point skeptical HN thread ("no longer confident in securing data"). ToolJet changed GPLv3→AGPLv3 three months post-launch with a public explainer — early is the only safe window. (https://medium.com/@robw_18337/n8n-is-not-open-source-3de57b57a413, https://github.com/ToolJet/ToolJet)
- **Don't market self-host if only the hosted path works.** PostHog dropped paid self-hosted support while marketing "open source" — HN called it a gimmick. (https://news.ycombinator.com/item?id=40564345)
- **No gimmicks on HN.** Cal.com's launch-page rickroll put people off the product; keep memes out of the launch. (https://news.ycombinator.com/item?id=34507672)
- **Don't ship a year of breaking 0.x releases.** LangChain's 0.0.x churn made APIs a "moving target" until v0.1.0; the "LangChain Is Pointless" thread (268 pts) attacked abstraction excess — the founder conceded on HN. (https://news.ycombinator.com/item?id=36645575)

## Top 10 pre-launch changes for Keel, ranked by star-impact

1. **Add an explicit star CTA + launch badges to the README** (ToolJet's direct ask; Cal.com added HN #1 badge same-day). Have the badge slots pre-written so they go live the day wins happen.
2. **Keep the quickstart provably working — re-verify on a clean clone the week of launch** (PostHog's gate: usable by strangers; the `safe_io` incident proved CI misses this). Keel's stdlib-only 5-min path is already the right shape.
3. **Hold the Show HN title pattern + founder-all-day comment plan** — "Show HN: Keel — automated job applications that refuse to lie" matches the proven differentiator-in-title pattern (AppFlowy); Trent answers every comment, especially trust objections (Cal.com).
4. **Put the gate-refusal moment first in the demo GIF** — the "refuses to lie" kill is Keel's one visual that no incumbent can show (AppFlowy lesson: lead with the differentiator, not the clone).
5. **Warm the first ~100 stars before HN** so visitors never land on 0 (AppFlowy takeaway; friends, early users, the defense-contractor community).
6. **Ship a two-tier good-first-issue set + named mentor contact before launch** (AppFlowy) — day-one visitors who can't use the tool yet should be able to contribute.
7. **State the Apache-2.0 license and open-core boundary prominently in the README now** (PostHog) — never let "is it really OSS?" become a thread.
8. **Sequence launch as HN → Reddit niche subs → PH second wave** (ToolJet measured PH alone at 50 stars/6h vs HN's 1,000+/8h; Documenso's PH amplified existing momentum).
9. **Draft launches #2 and #3 now** — a Launch Week cadence (Supabase) or a 2.0 moment (ToolJet) turns one spike into a flywheel.
10. **No claims beyond evidence parity** — the 209 figure is recount-backed; don't pitch Keel as beating incumbents until users say so (AppFlowy backlash).
