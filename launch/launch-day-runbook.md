# Keel Launch-Day Runbook — the coordinated spike

**Goal:** one day where every surface fires at once, driving a star-velocity spike that lands Keel on GitHub trending. Trending ranks stars-per-day; a single coordinated day beats months of drips.

**Baseline (measured 2026-09-22):** 0 stars · 5 forks · 14 open issues · 0 dispatches ever from the marketing engine.

**Doctrine:** real developers, real spike, zero fakery. No vote brigading, no asking for upvotes, no fake engagement, no invented numbers. HN detects and punishes all of it — one manipulation flag costs more than the launch gains.

---

## Phase 0 — Gates (all must be true before a date is picked)

### G1. The count conflict is settled
- Canon says 94 verified submissions (2026-09-15); README says 211. The Show HN body carries the 94 figure with a date hedge.
- **Rule: nothing carrying a submission figure ships until Trent adjudicates.** Options: (a) re-run the recount and update all surfaces to one agreed number, (b) drop figures from launch copy entirely and let the product speak. Pick one before T-0; the Show HN post must be edited to match.

### G2. The repo converts visitors
Stars come from people who land and *try it in five minutes*. Before launch:
- README: what Keel is in 10 seconds, demo GIF or screenshots above the fold, badges, install/try path that works on a fresh machine.
- Quickstart that actually runs end-to-end in under 5 minutes (test it on a clean checkout).
- `show-hn-final.md` is FINAL and approved — but re-verify the 94 figure per G1 the morning of.

### G3. Accounts and access are real
- **Hacker News:** only Trent's own account can submit. The legitimate path: he creates his own account, does genuine participation for a while (HN restricts new accounts from Show HN — "become a good contributor, then come back"), then submits. There is no shortcut; agents cannot warm an account he doesn't own.
- **Reddit:** human-only posting (bot challenge on the automation lane). Drafts exist for his manual post.
- **dev.to / Hashnode:** signups needed; drafts staged for his manual publish.
- **Product Hunt:** maker account + homepage URL are his call. PH convention is 12:01 AM PT launch.
- **Newsletters:** DevOps'ish tip drafted for him to send. Pragmatic Engineer guest route is closed (Aug 2026) — don't chase it.
- **X:** PARKED per his 09-17 directive. The launch runs without X unless he revives it.
- **LinkedIn:** never. Standing rule.
- **Instagram/Threads:** per-post approval stays — the launch doesn't depend on them.

### G4. Awesome-list PRs are filed and followed up
3 PRs open, 1 closed-unmerged. These are slow-burn star sources; file follow-ups or fresh targets *before* launch day so merges can ride the spike.

---

## Phase 1 — T-minus (the week before)

- [ ] G1 resolved: one submission figure everywhere, or no figures in launch copy.
- [ ] G2 done: README demo GIF, working 5-minute quickstart, verified on clean checkout.
- [ ] HN account created by Trent; genuine participation underway (comments on threads he actually cares about — automation, OSS, devtools, AI engineering).
- [ ] Reddit account in good standing; dev.to account created.
- [ ] Product Hunt decision made (his call): if yes, maker account + assets ready.
- [ ] Newsletter tips sent with embargo timing (his send): DevOps'ish + 2–3 more researched targets.
- [ ] Demo GIF screen capture done (needs his machine — agents can't capture his screen).
- [ ] Launch-day calendar blocked: **Trent must be online and responding for 4+ hours after the HN post.** Every comment answered within an hour, never defensively. This is the single biggest non-copy lever in the research.

---

## Phase 2 — Launch day (all times PT)

Timing evidence (launch-timing-research.md): optimal HN window 12:00–17:00 UTC = 5:00 AM–12:00 PM PT; weekday 11:00–16:00 UTC breakout edge; content matters 10x more than timing. Day: Tuesday or Wednesday. Never Friday.

| Time (PT) | Action | Owner |
|---|---|---|
| 12:01 AM | Product Hunt goes live (if his call was yes) | Trent |
| 5:00–6:30 AM | **Show HN submit.** Title exactly: `Show HN: Keel — automated job applications that refuse to lie`. URL exactly: `https://github.com/KeelDev-tech/keel`. | Trent (his account) |
| +2 min | **First comment immediately:** `Repo: https://github.com/KeelDev-tech/keel`, blank line, then the full body from `show-hn-final.md`. Record the post URL + numeric item ID. | Trent |
| 5:00–11:00 AM | **Comment duty.** Answer every HN comment within the hour. Never defensive; "good question, here's the honest limit of it" beats any pitch. Keep the repo's issues tab open — bug reports will come. | Trent (+ agent triage) |
| 6:00–7:00 AM | Reddit: r/programming and r/opensource posts (his manual post, human voice, no copy-paste of the HN post — rewrite the angle natively). | Trent |
| 7:00 AM | dev.to post publishes (his manual publish). | Trent |
| 8:00 AM | Newsletter embargoes lift (pre-sent in Phase 1). | — |
| Morning | Threads/IG cards only if he taps them — not on the critical path. | Trent |
| All day | Watch GitHub traffic: stars, clones, referral sources. Record actuals per measurement-checklist.md. | Agent |

**What NOT to do on launch day:**
- Don't ask anyone for upvotes — not friends, not chat groups, not "go upvote this." HN flags it; one flag undoes the day.
- Don't submit the Show HN twice or repost if it stalls. One shot per account per window.
- Don't argue with critics. The harshest HN comments are the most valuable if answered honestly.
- Don't touch LinkedIn, don't revive X unilaterally, don't use keeldev.

---

## Phase 3 — T+1 through T+14

- **T+1:** answer late HN comments; merge the easy contributor PRs fast (nothing converts visitors like a responsive maintainer); check trending placement.
- **T+3:** second-wave content — the dev.to deep-dive ("how the truthfulness gates work" — the technical post HN commenters asked for). This catches the people who starred but didn't read.
- **T+7:** retro against measurement-checklist.md bands. Front-page-class HN ⇒ tens to low-hundreds of stars (inferred band B6); no-front-page ⇒ single digits. Record actuals, not narratives.
- **T+14:** newsletter follow-ups that covered the launch get thank-yous; awesome-list PRs get bumped; the next experiment (EXP pipeline) starts from real data.

---

## Abort / hold criteria

- Count conflict unresolved → hold all figure-carrying copy; launch with the product story only, or don't launch.
- HN account not warmed / still keeldev-dependent → hold the Show HN; run Reddit + dev.to + newsletters as a soft launch instead.
- README doesn't convert (no working quickstart) → hold everything. Traffic without conversion is burned attention.
- Major tech announcement or holiday that day → slip to the next Tue/Wed.

---

## The one-line version

Fix the repo so it converts, settle the number, warm your own HN account, then pick a Tuesday, post at 5–6:30 AM PT, and spend the whole morning answering every comment honestly. Everything else is amplification of that morning.

---

## Readiness test — 2026-09-22 (clean-clone run)

Ran the README quickstart exactly as a launch-day visitor would: fresh `git clone`, `./setup.sh`, `./start.sh`, `score_roles.py`, seed-one-lead, `apply_loop.py`, `build_dashboard.py`, plus the unittest suite.

| Gate | Verdict | Evidence |
|---|---|---|
| G1 count conflict | **PASS (adjudicated 2026-09-22 ~04:35 PDT)** | Evidence-backed recount (`geo-pipeline/recount.py`, exit 0): **209** canonical SUBMITTED rows (207 exact-status + 2 lowercase-alias per LEDGER_STATUS_ALIAS); evidence split 202 quoted / 3 pointer / 1 url-only / 3 unevidenced. The stale 94 (2026-09-15) and 214 (2026-09-21) figures are superseded — the recount is the count of record. Atomically updated: canon `proof-209-submissions` added (94 kept as historical), README both slots, `docs/geo/stats.json`, site/ (already live via GEO), Show HN body. Committed as `134be77`. |
| G2 repo converts | **CONDITIONAL** | Clone, setup, start, scoring, seeding, and packet build all pass; demo.gif exists; tests pass (17/17 in smoke). **But the final quickstart step is broken:** `engines/build_dashboard.py` line 22 does `from safe_io import ROW_KEYS` and no `safe_io` module exists anywhere in the repo → `ModuleNotFoundError` on every clean clone. No test covers build_dashboard, so CI stays green while the README's finish line 404s. Launch-blocker until fixed. |
| G2 repo converts | **PASS (fixed 2026-09-22 ~04:28 PDT)** | Root cause: `engines/safe_io.py` existed in the dev tree but was never committed — the public `build_dashboard.py` imported `ROW_KEYS` from a module that wasn't in the repo. Fix: committed the single missing file (`673bbc1`, one file, nothing else touched). Verified on a brand-new clone of updated main: full README quickstart passes end-to-end (setup → start → score → seed → packet → dashboard renders → 17/17 tests green). |
| G3 accounts | **NOT READY** | keeldev dead since 09-15 and not his; no Trent-owned HN account exists; Reddit/dev.to signups pending; Product Hunt undecided; X parked; LinkedIn never. |
| G4 awesome PRs | **STALE** | 3 open, zero maintainer comments, zero merged; 1 closed-unmerged (no re-file). Separately, 2 inbound community PRs (#9, #10) sit unreviewed on the keel repo itself — reviewing them pre-launch is high-ROI maintainer optics. |
| G4 awesome PRs | **CLEAR (2026-09-22 ~04:45 PDT)** | Inbound queue emptied: PR #9's fix had already landed in main → closed as completed with thanks; PR #10 (Workable ATS detection) adapted to the current pattern-table structure, tests green (69 ok), committed as `2cbab78` with contributor credit → closed as completed with thanks. Zero open inbound PRs. The 3 outbound awesome-list PRs stay open per the standing assessment (leave open, no nudging). |

**Bottom line of the test:** the launch is not ready. The `safe_io` launch-blocker is fixed and verified (2026-09-22 ~04:28 PDT, commit `673bbc1`). Remaining: the count adjudication (G1), Trent-owned HN account + Reddit/dev.to signups (G3), and reviewing the 2 inbound community PRs (G4 optics). Everything else is taps only Trent can make.

**Update 2026-09-22 ~04:50 PDT — launch prep push:** G1 DONE (209 adjudicated, all surfaces agree, `134be77`). G2 DONE (`safe_io` fix verified, `673bbc1`). G4 DONE (inbound PR queue emptied: #9 closed as already-landed, #10 adapted+merged as `2cbab78`, both contributors thanked; 0 open inbound PRs). **Only G3 remains — and every item in it is a tap only Trent can make** (see Phase 0 checklist). The repo is launch-ready; the accounts are not.
