# Show HN timing research — Keel launch

Compiled 2026-09-14. Research only; no posting scheduled.

## Sources

1. **Launch-Day Diffusion: Tracking Hacker News Impact on GitHub Stars for AI Tools** (arXiv, Nov 2025; 138 HN↔repo pairs, 2024–2025)
   https://arxiv.org/pdf/2511.04453v1
   - Posting hour strongly affects launch success: optimal hours gain ~200 more stars than poor ones (OLS regression, robust SE, controlled for baseline stars, HN score, repo age, title length, posting day).
   - **Best hour window: 12:00–17:00 UTC** — catches US morning activity and European afternoon browsing.
   - Weekend vs weekday effect: negligible (+10.2 stars at 48h, p=0.81).
   - Caveat: Show HN tag itself showed no significant advantage after controlling for project maturity — selection bias (Show HN = early solo-developer experiments). The contract/story carries the post, not the tag.
2. **janbussieck/hn-skill** (analysis of 157,000+ Show HN posts)
   https://github.com/janbussieck/hn-skill/blob/HEAD/.claude/skills/hn-optimize/SKILL.md
   - Best windows (UTC): Sunday 11:00–16:00 (~12–14% breakout rate), Saturday 14:00–20:00 (12–14%), **weekday 11:00–16:00 (~10.5%)**, Sunday 0:00–2:00 (~15.7%, = Sat 7–9 PM US).
   - Avoid: 03:00–07:00 UTC any day (lowest engagement).
   - "Timing provides a 20–30% edge at best — content quality matters 10x more."
3. **spacia-ai/burn-copilot oss-launch-engine** (multi-platform launch playbook)
   https://github.com/spacia-ai/burn-copilot/blob/HEAD/.codex/skills/oss-launch-engine/SKILL.md
   - Launch-day sequence: Product Hunt 12:01 AM PT → blog post 30 min before HN → **HN 8:00–9:30 AM ET** → X simultaneous with HN → Reddit 30–60 min after.
   - Decay half-life 2–5 days; plan T+3/T+7/T+14 events.
4. **detrin/brow Show HN planning issue** (browser-automation repo, comparable category)
   https://github.com/detrin/brow/issues/3
   - **Tuesday, Wednesday, or Thursday; 9–11 AM US Eastern**; avoid holidays and major tech announcements.
   - Checklist: polished README, demo, be online to respond to comments for 4+ hours after posting, answer every comment within 1 hour, don't get defensive.

## Convergence

- **Day: Tuesday or Wednesday.** (Today is Monday 2026-09-14; Monday also still needs Trent's approval flow, so Tuesday is the earliest realistic option.)
- **Time: 8:00–9:30 AM ET = 5:00–6:30 AM PT** — sits inside both the arXiv 12–17 UTC window (12:00–17:00 UTC = 5:00 AM–12:00 PM PT) and the 157k-posts weekday 11:00–16:00 UTC window (4:00–9:00 AM PT), and matches the playbook norm of HN at 8–9:30 AM ET.
- Weekend vs weekday is statistically negligible, so don't push to the weekend on timing grounds alone; weekday gives the biggest overlap of US + EU eyeballs.
- Content >> timing (10x weight per the 157k analysis). The finalized copy is already the higher-leverage asset.
- **Human-availability requirement:** Trent (or the responding agent) must be online and responding for 4+ hours after posting; every comment answered within an hour, never defensively. This is the single biggest non-copy lever.

## Recommended posting window (this week)

- **Primary: Wednesday 2026-09-16, 8:00 AM ET / 5:00 AM PT.** Tuesday 2026-09-15, same time, is the backup if copy approval lands early and Trent wants it out sooner.
- Avoid Friday (launch-day troubleshooting playbook flags Friday launches as a classic mistake) and avoid any day with a major tech announcement or holiday.
- Posting at 5 AM PT is early for Trent — if 5:00 AM PT is not workable, 8:00–9:00 AM PT (11:00 AM–12:00 PM ET / 15:00–16:00 UTC) is still inside both evidence-backed windows, but do not push past ~9:00 AM PT: the US-morning/EU-afternoon overlap decays after 17:00 UTC.
- **Note on today's date:** Monday 2026-09-14 is still in progress at plan time; the Show HN copy is FINALIZED but unapproved. No posting is authorized regardless of day until Trent's explicit go-ahead.

## What this research does NOT support

- No source justifies inventing or inflating any claim in the post; several sources penalize promotional framing ("Product launch" type = very low front-page rate vs. genuine Show HN).
- No source here gives platform-specific guidance for X/IG timing; the thread rollout timing is sequenced relative to HN (see launch-thread-rollout-plan.md).
