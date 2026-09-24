# Keel — Launch/Distribution Channel Hit-List

**Date:** 2026-09-22
**Scope:** FREE only. No paid placements. No vote-manipulation, brigading, coordinated stars, or manufactured social proof.
**Closed lanes (excluded):** Product Hunt, Changelog.com, LinkedIn, X/Twitter, TLDR. Already covered: Reddit, HN, dev.to.
**Canonical traction wording (use everywhere):** "209 verified submissions (202 with quoted confirmation evidence, 3 pointer, 1 URL-only, 3 unevidenced — counted from the ledger, never estimated)."

URL confidence legend: **verified** = opened directly this run; **reported** = seen in search results/secondary docs, re-check before submitting; **unverified** = confirm before use.

---

## TIER 1 — Do now (high reach × high SEO/GEO, no gates)

### 1. Python Weekly (Cooperpress)
- **Route:** "Suggest a link" footer link in any issue, or editorial email `hello@cooperpress.com` / `editor@cooperpress.com` (reported; editorial ≠ ads — `sales@` is separate, don't use it)
- **Cost:** free
- **Requirements/gotchas:** one item per outlet; disclose authorship; lead with the engineering story (stdlib-only public half is the hook); no hype-speak; inclusion not guaranteed
- **Why:** ~460k-subscriber Cooperpress network, strongest topical fit on this list (Python dev tool → Python newsletter); archived issues get indexed and quoted by AI answers

### 2. Console.dev
- **Route:** https://console.dev/submit-a-tool/ (also reported as https://console.dev/submit) — **reported**, re-verify before use (page open failed this run)
- **Cost:** free tip submission; editorial curation, no paid placement for the tip itself
- **Requirements/gotchas:** curated weekly of dev tools that "specifically loves open source"; pitch the open-core honesty contract, not a product blurb
- **Why:** exact audience (tool-buying developers); archived issues indexed → GEO surface for dev-tool queries

### 3. Indie Hackers — Milestones
- **Route:** indiehackers.com — Milestones feature ("go to IndieHackers.com/Milestones and post a milestone"; free account)
- **Cost:** free
- **Requirements/gotchas:** no hard karma gate, but strict social norms: milestone → how you got there → what you learned → what's next → a question for the community. Real numbers required (the 209 figure is native here). Pure link-drops get ignored. Frame as build-in-public.
- **Why:** high-authority founder community; milestone pages rank in Google; IH threads are heavily represented in LLM training data

### 4. HackerNoon
- **Route:** https://hackernoon.com/p/publish — **verified** ("HOW TO PUBLISH ON HACKERNOON"; free account → Write → submit for review)
- **Cost:** free
- **Requirements/gotchas:** human editorial review (~3 business days); the form asks "Is this story original on HackerNoon?" — answer **No** for a cross-post and supply the canonical URL of the first-published copy (dev.to/docs); requires featured image, excerpt, TL;DR, tags, category; disclose vested interest
- **Why:** ranks aggressively in both Google and AI answers for dev-tool topics — one of the strongest GEO surfaces available free

### 5. AlternativeTo
- **Route:** https://alternativeto.net/manage/add-application/ (reported; also `/software/add/`; flow confirmed in AlternativeTo's own FAQ at https://alternativeto.net/faq/ — verified FAQ page)
- **Cost:** free listing (paid "boosts" exist as a promo product — skip; submission/approval is free)
- **Requirements/gotchas:** free account + email verification; **account must be 7+ days old to submit** (anti-spam) → create the account NOW, submit next week; moderator review backlog (days–1 week); License = "Open Source" (Apache-2.0 ✓); Platforms = Self-Hosted/Linux/Mac/Windows (CLI)
- **Why:** dofollow backlink from a very-high-authority domain (~8M+ visits/mo); ranks for "alternative to X" queries

### 6. SaaSHub
- **Route:** https://www.saashub.com/submit (reported; via account → "Add Product")
- **Cost:** free basic listing (paid boost upsells exist — skip)
- **Requirements/gotchas:** account + email confirm; product must be launched (no waitlist pages) — GitHub repo + README counts; English; hand-reviewed (1–2 days typical); claim/verify via domain email after approval (note: no Keel domain exists — verify what's accepted at claim time)
- **Why:** dofollow backlink, DR ~80; strong on software-comparison/alternative queries — best dev-tool directory fit on the list

### 7. Hashnode — canonical mirror
- **Route:** hashnode.com — publish via editor; canonical-URL field in article settings (reported via multiple secondary sources)
- **Cost:** free tier covers everything
- **Requirements/gotchas:** set **Canonical URL → the dev.to article or docs page** so Google de-dupes correctly; mirror the launch article (don't rewrite from scratch); audience skews senior dev
- **Why:** second indexed copy with strong dev-domain authority, zero duplicate-content risk via canonical; Hashnode articles surface in AI answers for dev-tool queries

### 8. Medium — canonical import
- **Route:** Medium Help Center — https://help.medium.com/hc/en-us/articles/360033930293-Set-a-canonical-link — **verified**: Edit story → three-dot → More settings → Advanced Settings → "This story was originally published elsewhere." Import auto-sets rel=canonical to source.
- **Cost:** free
- **Requirements/gotchas:** gotcha — the import tool copies the *original's* revision date as publish date, burying the post in your profile; for a launch post, **paste into a fresh draft and set the canonical manually** to get today's date
- **Why:** Medium domain authority + canonical protection = safe syndication; Medium pieces are frequently cited by AI search

### 9. awesome-job-search-resources (GitHub PR)
- **Route:** https://github.com/dr-mushtaq/awesome-job-search-resources — README "Contributing" section (no separate file): fork → add row in category → PR; or open an issue as alternative intake. README: https://github.com/dr-mushtaq/awesome-job-search-resources/blob/HEAD/README.md
- **Placement:** `📈 Job Tracker Tools` (alongside Huntr, Teal — direct product analogues)
- **Cost:** free
- **Requirements/gotchas:** table row format — Name (linked), 15–20 word description, Tags (e.g. `Jobs, AI, Free, Open Source`). **No star or age minimum stated.** Caveat: single-curator list with visible quality drift; low editorial authority vs the big lists. Zero gates — submit first.
- **Why:** exact topical adjacency for "job application tracker/automation" queries; crawled awesome-list data feeds LLM corpora

### 10. Lemmy — programming.dev / relevant communities
- **Route:** programming.dev (dev-focused instance); lemmy.world/c/selfhosted
- **Cost:** free
- **Requirements/gotchas:** no karma minimums; **read each community's sidebar rules first**; verified selfhosted norms: no spam, don't paste full blog/GitHub text (link instead), headline must match the article title, no low-effort posts. Frame as a practical tool, feedback-oriented, one GitHub link, no star-begging. Cross-posting is first-class (use the button, don't manually duplicate). programming-flavored communities fit better than selfhosted unless framed as self-hostable automation.
- **Why:** the exact post-Reddit-exodus FOSS crowd; federated posts get indexed; community disproportionately represented in open-source discourse

---

## TIER 2 — Do this week (solid backlinks / credibility)

### 11. SourceForge project registration
- **Docs:** https://sourceforge.net/p/forge/documentation/Create%20a%20New%20Project/ (verified in search)
- **Cost:** free
- **Requirements/gotchas:** account; project must be OSI-compliant open source (Apache-2.0 ✓); **first project requires phone/PIN verification**; GitHub release importer exists (import straight from GitHub releases)
- **Why:** high-authority profile page with screenshot/gallery fields; SourceForge still ranks well in software categories

### 12. Launching Next
- **Route:** https://www.launchingnext.com/submit/ (reported)
- **Cost:** free (**$99 optional expedited review upsell — skip it**)
- **Requirements/gotchas:** no account needed; form wants name, URL, 5–8-word headline, description (≤2,500 chars), 5–10 tags, simple math captcha; editorial review daily; live products only
- **Why:** dofollow link, low traffic — do it for the link equity, not the audience (10 minutes)

### 13. Lobsters — one careful technical post
- **Route:** lobste.rs — submission form; rules at https://lobste.rs/about (verified)
- **Cost:** free
- **Requirements/gotchas:** self-promo explicitly capped: *"self-promo should be less than a quarter of one's stories and comments"*; submissions **must** be tagged from the predefined list (`python` fits); authors participating is welcomed but "not to exploit it as a write-only tool for product announcements"; new accounts may require an invitation (**unverified** — confirm). Best: participate first, then post once, framed technically (the truthfulness-contract architecture, not the pitch).
- **Why:** small but elite dev audience; stories get scraped into training corpora and cited in technical answers

### 14. GitHub Discussions (on the Keel repo)
- **Route:** repo Settings → Discussions (repo setting, no external URL)
- **Cost:** free
- **Requirements/gotchas:** set up categories: Announcements, Q&A, Ideas; post the launch announcement in Announcements
- **Why:** discussion threads are indexed by Google and are prime AI-citation material — a Q&A category is effectively a living FAQ corpus that LLMs quote; also signals repo health

### 15. BetaList
- **Route:** https://betalist.com/submit (reported)
- **Cost:** free submission (**$129 "featured" upsell — skip**)
- **Requirements/gotchas:** geared to new/beta launches; review can take weeks. Keel is past initial launch — best angle is a v1.0/launch milestone moment, not "we exist."
- **Why:** dofollow, early-adopter audience

### 16. Black Duck Open Hub (formerly Ohloh)
- **Route:** https://www.openhub.net/ — sign up and add/claim the project (exact add path **unverified** — confirm on site)
- **Cost:** free
- **Requirements/gotchas:** open-source project with a code repository
- **Why:** credibility signal for OSS (developer/enterprise trust); indexed project analytics page

### 17. Slant
- **Route:** add Keel as an "option" on a relevant question (e.g. "best job application automation tools") — exact add URL **unverified**
- **Cost:** free; community-curated, no paid favorable placement
- **Requirements/gotchas:** write like a user, **disclose authorship**; hype gets flagged by the community
- **Why:** comparison-driven traffic, dofollow per roundups

### 18. Startup Stash
- **Route:** https://startupstash.com/add-listing/ (reported) — free, no login
- **Cost:** free
- **Requirements/gotchas:** audience is other founders submitting (low intent) — backlink value only
- **Why:** cheap dofollow win

### 19. StackShare
- **Route:** add tool via stackshare.io — exact URL **unverified**, one more verification pass before use
- **Cost:** free tool listings (dofollow per roundups; ~300k/mo dev-tool audience)
- **Why:** strong fit for dev-tool stack decisions; worth the verification pass

### 20. LibHunt
- **Mechanism:** GitHub-data-driven (stars + mentions); new projects discovered automatically. Bonus manual route (reported): libhunt.com/site/project_submit. Python section: libhunt.com/l/python/
- **Cost:** free; nothing to submit for auto-discovery
- **Requirements/gotchas:** repo must be discoverable — good README, topics set, description set. PRs to backing "awesome" lists also surface there.
- **Why:** direct audience fit for a Python dev tool; zero effort beyond repo hygiene

---

## TIER 3 — Deferred / conditional (real value, but gated)

### 21. awesome-cli-apps — best list, gated
- **Repo:** https://github.com/agarrharr/awesome-cli-apps (canonical — moved off aharris88)
- **Contribution route:** https://github.com/agarrharr/awesome-cli-apps/blob/HEAD/contributing.md
- **Placement:** `## Utilities → ### Professional: Resume` (exists); fallback `## AI → ### Agents`
- **Cost:** free
- **Requirements/gotchas:** **repo >3 months old (bot-enforced, auto-closes PRs)**; **>20 GitHub stars (bot-enforced)**; one PR per app, title exactly `Add APP_NAME`, PR body must include the literal string `contributing.md`; **AI-generated PRs explicitly unwelcome — the "why it's awesome" paragraph must be human-written (Trent authors/discloses)**. Free + open-source license ✓ (Apache-2.0); easy install; well documented.
- **Why:** the only high-traffic list with a purpose-built category for this product; heavily mirrored (awesome-cn, star-ranked forks) so one merge propagates into many crawled datasets

### 22. DZone — technical deep-dive, not a launch post
- **Route:** https://dzone.com/writers-zone; guidelines https://dzone.com/articles/dzones-article-submission-guidelines?fromrel=true (verified)
- **Cost:** free
- **Requirements/gotchas:** **not a Show-post venue** — "Do not submit content with the goal of advertising your product"; 800+ words (1000+ realistic), original (not published elsewhere), real-name personal account, **no AI-generated content**; review backlog ~30 business days. Right play: "how we built the truthfulness gates" deep-dive; use the "Original Source" attribution field for the backlink.
- **Why:** strong developer-domain backlink + long-tail Google traffic for the deep-dive topic

### 23. freeCodeCamp News — tutorial pitch, long shot
- **Route:** https://www.freecodecamp.org/news/how-to-write-for-freecodecamp/ (verified); standards https://www.freecodecamp.org/news/publication-standard-of-ethics-corrections/
- **Cost:** free
- **Requirements/gotchas:** selective editorial: pitch ideas first, draft to a style guide, copy-edit/fact-check. A launch announcement won't clear; an educational tutorial ("Automate your job applications with this open-source Python tool") might. Months-long timeline.
- **Why:** massive reach (millions of devs), enormous domain authority; one accepted piece = top-tier backlink + GEO citation source

### 24. awesome-selfhosted — deferred ~4 months post first tagged release
- **Repo:** https://github.com/awesome-selfhosted/awesome-selfhosted (submissions as YAML to `awesome-selfhosted/awesome-selfhosted-data`)
- **Contribution route:** https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/HEAD/.github/PULL_REQUEST_TEMPLATE.md
- **Cost:** free
- **Requirements/gotchas:** one item per PR; kebab-case `.yml` per their `addition.md`; **first released >4 months ago (hard gate)**; working installation instructions; **tagged releases expected** (maintainers reject no-release projects via canned reply); must be Free software; Keel is stdlib-local and self-hostable but the list's philosophy is "replaces something you'd pay for" — plausible under `Misc/Other`, niche read
- **Why:** one of the most-crawled awesome lists; long-lived discovery traffic

### 25. vinta/awesome-python — long-term only
- **Repo:** https://github.com/vinta/awesome-python; contributing: https://github.com/vinta/awesome-python/blob/HEAD/CONTRIBUTING.md
- **Cost:** free
- **Requirements/gotchas:** active (<12 mo commits), stable (production-ready), documented, established (≥1 month); entry must be "obvious choice" (≤3 per use case) or "challenger" with **adoption-trajectory evidence**; older bar: 5,000+ stars or 100–500 stars + ≥3 months + proven real-world usage; **display name must be the PyPI package name — Keel isn't on PyPI**; entry PRs can't create categories. Poor fit today.
- **Why:** one of the most-starred lists on GitHub — worth the long game after PyPI publication + adoption

### 26. Bytes (ui.dev) — uncertain, weak fit
- **Route:** bytes@ui.dev / hello@bytes.dev editorial email, or reply to any issue (reported; **conflicting sources** — one claims paid-sponsorship-only; treat editorial pickup as uncertain)
- **Cost:** free if picked up editorially
- **Requirements/gotchas:** JS/web humorous tone; weak-to-medium fit for a Python job-automation tool; needs a punchy demo angle
- **Why:** high referral traffic if picked up; not a form — an editorial pitch

### 27. Sidebar.io — verified form, weak fit
- **Route:** https://sidebar.io/submit — **verified live** "Submit a Link" page; requires login
- **Cost:** free; curated by Sacha Greif
- **Requirements/gotchas:** design/frontend-leaning curation — weak fit for an automation tool
- **Why:** included because the form is confirmed working; rank last

---

## Assessed and skipped (do not pursue)
- **GitHub Marketplace:** only publishes GitHub Actions and GitHub Apps (https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace — verified). Keel is a Python CLI — no fit unless a thin wrapper Action is deliberately built later.
- **Capterra / G2:** free basic profiles exist but both are commercial, sales-led, review-gated, pay-to-play in practice. SaaSHub/SourceForge cover the same comparison-page value for free.
- **Stack Overflow:** confirmed not a promotion venue (https://stackoverflow.com/help/promotion). Only touchpoint is answering genuinely relevant questions with disclosure — slow, question-by-question.
- **Discord servers:** near-zero SEO (not indexed); feedback channel only, not reach. Note: Python Discord removed its showcase channels — don't assume one exists anywhere; read rules first.
- **awesome-devtools / awesome-foss family / awesome-hrtech / awesome-ats / sindresorhus/awesome:** fragmented, stale, recruiter-side, or meta-list — wrong domain for Keel.

---

## GEO/SEO tactics checklist (repo + docs site — all free, mostly one-time)

### Repo hygiene (do first)
- [ ] **Description field:** keyword-first one-liner, e.g. "Open-source Python tool that automates job applications — 209 verified submissions" (first ~160 chars show in search)
- [ ] **Topics (max 20 allowed; use 8–12):** `python`, `automation`, `job-search`, `job-applications`, `ats`, `resume`, `open-source`, `cli`, `career`, `recruitment`, `self-hosted`, `agents`, `workflow-automation`, `job-board`, `devtools`, `productivity` — lead with high-traffic established topics (`python`, `automation`, `cli`, `open-source`) for browse volume, then niche intent topics (`job-search`, `ats`, `resume`) for exact-match. Prefer topics that already have repos attached (they surface in topic pages); custom coinages with zero existing repos add nothing. GitHub's ML suggests topics — accept/reject rather than fighting it. Reference: https://docs.github.com/en/articles/classifying-your-repository-with-topics
- [ ] **Social preview image:** 1280×640 PNG/JPG/GIF under 1MB via Settings → Social preview (https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)
- [ ] **README:** keyword-rich H1/H2 hierarchy (problem → solution → quickstart → features → installation); badges; demo GIF with alt text; no dead links; first 200 words carry the value prop + keywords. GitHub topic pages (not raw search) are what Google/LLMs surface most — optimize for those.
- [ ] Enable **Discussions** (Announcements / Q&A / Ideas); cut a **Release** with notes; keep commit activity steady

### Docs site
- [ ] **FAQ page (`/faq`):** real Q/A pairs phrased the way people ask AI — "Is Keel free?", "What Python version does Keel need?", "How does Keel verify its 209 submissions?", "Does Keel store my data?", "What license is Keel under?" — Q&A pairs are the most AI-cited format after the homepage
- [ ] **Proof/methodology page (`/proof`):** what "209 verified submissions" means, the evidence split, how verification works, timestamped counts. AI engines cite pages with specific numbers, stated methodology, and clear claim boundaries — vague marketing copy doesn't get cited
- [ ] **schema.org JSON-LD** (`@graph` block per page is Google's recommended pattern):
  - `SoftwareApplication` on homepage: name, `applicationCategory: "DeveloperApplication"`, `operatingSystem`, `offers` with `price: "0"`, `url`, `sameAs` (GitHub repo). Skip `aggregateRating` — only with real reviews (fake ratings risk manual action)
  - `FAQPage` on the FAQ page (`Question` + `acceptedAnswer`, must be visible on page)
  - `HowTo` on the install/quickstart guide (step array)
  - `Article`/`BlogPosting` on the launch post; `Organization` (+ `Person` for Trent) site-wide; `BreadcrumbList` on every page
- [ ] **llms.txt:** hand-write a small curated file at the docs-site root (`/llms.txt`): `# Project name` → `>` blockquote summary → `## Section` headings → `- [Page title](url): one-line description` link lists. Spec: llmstxt.org. Honest framing: proposed by Jeremy Howard (Answer.AI) Sept 2024, **not a formal standard**; ~10% of surveyed domains have one (SE Ranking, early 2026); **zero confirmed consumption** by major AI platforms (Google's John Mueller: Search doesn't use it). Cheap to ship, possible future upside. **Log requests to /llms.txt by user agent** — the only honest measurement of whether anything fetches it.
- [ ] **robots.txt + sitemap.xml:** GitHub Pages serves static files as-is; for Jekyll, `jekyll-sitemap` + `jekyll-seo-tag` are on the Pages whitelist. robots.txt: allow all + `Sitemap:` directive; **explicitly allow AI crawler user-agents** (GPTBot, ClaudeBot, PerplexityBot, CCBot, anthropic-ai, Google-Extended, Applebot-Extended) — robots.txt is the one file that actually controls crawler behavior. Submit sitemap to Google Search Console + Bing Webmaster Tools.
- [ ] **Canonical chain:** decide the docs site as canonical home; dev.to article = first-published hub; Hashnode + Medium copies point canonical back to it (or docs). Consistent chain prevents duplicate-content dilution and concentrates ranking signals.

### Sequencing / Trending note
- GitHub Trending ranks **stars gained inside a window** (daily/weekly/monthly), not lifetime total — velocity vs. the repo's own baseline; filtered per language (Python-daily is a smaller pond); recomputed ~every 4–6 hours; stars from brand-new/inactive accounts are discounted.
- Practical implication: **compress promotion into one tight 24–48h window** (awesome-list merges, topic additions, README polish, launch posts landing together, ideally Mon–Tue UTC) rather than spreading across weeks. Awesome-list entries themselves create the star bursts that feed Trending — time the first-wave PRs with the push.
- Never buy stars or bot bursts — flagged within hours, disqualifying.
