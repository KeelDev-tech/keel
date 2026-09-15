# Keel Launch — Measurement Checklist
**Status:** Show HN post FINALIZED, NOT yet posted (awaiting the founder's go-ahead).
**Purpose:** Pre-registered measurement plan. What to record, when, where to read it, and which benchmark band it gets compared against. **Do not execute any measurement until its checkpoint.**
**Rules:** Record actuals; never invent numbers. Benchmarks are sourced averages, not guarantees. Nothing here authorizes posting.

---

## Benchmark bands (sourced — reused from the internal growth-metrics draft, 2026-09-15)

| # | Band | Value | Source | Status |
|---|---|---|---|---|
| B1 | Front-page consideration | 30+ points within 4h of submission | Cabinet Dancer HN launch issue (github.com), sourced 2026-09-15 | AVERAGE/THRESHOLD, not a guarantee |
| B2 | Front-page anchor at rank ~30 | ~161 mean points (2023 data) | HN discussion news.ycombinator.com/item?id=34941474 | DATED (2023) anchor, not a 2026 threshold |
| B3 | Early-velocity rule | ~4–6 upvotes in first 20–40 min on /newest → bottom of homepage; Show HN has softer gravity (easier on, shorter stay) | HN comment thread news.ycombinator.com/item?id=36590226 | QUALITATIVE rule |
| B4 | Front-page traffic | 1k–5k unique visits to the linked site within 24h if post reaches /front | Same launch-planning doc as B1 | RANGE, not a guarantee |
| B5 | Posting-hour lever (AI tools, 138 HN↔repo pairs, arXiv Nov 2025) | Optimal hours (12:00–17:00 UTC) gain ~200 more stars than poor ones; weekend-vs-weekday negligible; Show HN tag itself showed no significant star advantage after controlling for maturity | https://arxiv.org/pdf/2511.04453v1 | STUDY AVERAGE — content/contract carries the post |
| B6 | Repo-traffic expectation | INFERRED: front-page-class HN ⇒ tens to low-hundreds of stars (low-single-digit % conversion on 1k–5k uniques); no-front-page ⇒ single digits | INFERRED from B4, **not sourced** | SANITY BAND, report actuals against the measured zero baseline |

### Baselines (measured, not estimated)
- **Repo** (measured 2026-09-15 ~00:05 PT, `github.com/KeelDev-tech/keel`, public API): **0 stars / 0 forks / 0 watchers**. Re-check at T=0 to confirm still zero.
- **IG** (@optimisticpropagandist, account insights, captured 2026-09-14 23:54 PT):
  - 7d: reach 612 · content views 6,788 · total interactions 99 · profile visits 1,031 · bio-link taps 1
  - 24h: reach 287 · content views 1,073 · total interactions 11 · profile visits 152 · bio-link taps 0
  - Scheduled re-snapshots: 24h due 2026-09-15 ~23:54 PT, 72h due 2026-09-17 ~23:54 PT, 7d due 2026-09-21 ~23:54 PT.

---

## Checkpoint T=0 — Post time
Record the moment the Show HN post goes live (the founder's go-ahead only). Metrics:

| # | Metric | Where to read | Compare against |
|---|---|---|---|
| T0.1 | Thread URL + item ID | The posted thread page | — (identifiers only) |
| T0.2 | Exact posting timestamp (PT) | Clock at post time; confirm on thread page ("X minutes ago") | Planned window: Wed 2026-09-16, 8:00 AM ET / 5:00 AM PT (primary); Tue same time backup — check actual time vs. optimal-hours lever (B5) |
| T0.3 | X thread: posted? URL + timestamp | X/Twitter UI | — (identifiers only) |
| T0.4 | Repo baseline re-check: stars / forks / watchers | `github.com/KeelDev-tech/keel` (public page or API) | Measured zero baseline above — confirm still 0/0/0 |
| T0.5 | IG baseline snapshot: 24h window (reach, content views, interactions, profile visits, bio-link taps) | `instagram-cli account-insights` 24h window | 2026-09-14 23:54 PT baseline above |

**Gate:** If the thread URL cannot be produced with confirmation evidence, the launch did not happen. Never report "posted" without it.

## Checkpoint T+1h
| # | Metric | Where to read | Compare against |
|---|---|---|---|
| T1.1 | Points | HN thread page | B3 early-velocity rule (4–6 upvotes in first 20–40 min ⇒ bottom-of-homepage); B1 trajectory check (on pace for 30 by 4h?) |
| T1.2 | Rank: front page? /newest? /ask? position | news.ycombinator.com/front, /newest, /ask | B3 (softer Show HN gravity: easier on, shorter stay) |
| T1.3 | Comment count | HN thread page | — (qualitative: are comments substantive or drive-by?) |
| T1.4 | Repo deltas: stars / forks / watchers vs. T0.4 | `github.com/KeelDev-tech/keel` | Measured baseline (0/0/0) |
| T1.5 | X thread impressions (if available) | X post analytics | — (report actuals or "unavailable") |

## Checkpoint T+4h — Front-page consideration check
| # | Metric | Where to read | Compare against |
|---|---|---|---|
| T4.1 | Points | HN thread page | **B1: 30+ points ⇒ front-page consideration** (the key binary check) |
| T4.2 | Rank: front page? /newest? /ask? position | news.ycombinator.com/front, /newest, /ask | B2 (~161 at rank ~30, 2023 anchor — where we sit relative to it, dated caveat noted) |
| T4.3 | Comment count | HN thread page | — |
| T4.4 | Repo deltas vs. T0.4 | `github.com/KeelDev-tech/keel` | Measured baseline |
| T4.5 | X thread impressions | X post analytics | — |

**Verdict rule:** ≥30 points at 4h ⇒ "in consideration"; <30 ⇒ "building, no front-page signal yet." Neither is a verdict on the launch — only on the front-page path.

## Checkpoint T+24h
| # | Metric | Where to read | Compare against |
|---|---|---|---|
| T24.1 | Points, final rank, comment count | HN thread page + /front | B1, B2 |
| T24.2 | Did it reach /front? (yes/no, peak position if known) | /front observation or thread state | **B4: reached /front ⇒ expect 1k–5k uniques to the linked site in 24h** |
| T24.3 | Repo deltas vs. T0.4 (stars/forks/watchers) | `github.com/KeelDev-tech/keel` | B6 sanity band; measured zero baseline |
| T24.4 | IG 24h window deltas: reach, content views, interactions, profile visits, bio-link taps | `instagram-cli account-insights` 24h window | T0.5 baseline; funnel view: views → profile visits → bio taps (current 7d: 6,788 → 1,031 → 1) |
| T24.5 | X thread impressions (if available) | X post analytics | — |
| T24.6 | Repo traffic estimate vs. band | **Cannot measure without auth** (GitHub traffic API requires auth) — record as INFERRED/UNAVAILABLE unless a token is authorized | B4 (only if /front was reached) |

## Post-mortem T+72h
| # | Metric | Where to read | Compare against |
|---|---|---|---|
| T72.1 | Final HN: points, comment count, peak rank | HN thread page | B1–B3 |
| T72.2 | Final repo deltas: stars / forks / watchers vs. T0.4 | `github.com/KeelDev-tech/keel` | B6; measured zero baseline |
| T72.3 | IG 7d window comparison: reach, content views, interactions, profile visits, bio-link taps | `instagram-cli account-insights` 7d window | Pre-launch 7d baseline (captured 2026-09-14 23:54 PT); note the thin funnel (1 bio tap / 7d) |
| T72.4 | Per-post like deltas on the six tracked posts | Per-post hydration | Per-post reads from the draft report (e.g. POST-015 "The Gargoyle's Feed" led with 6) |
| T72.5 | What the numbers say vs. the bands | Compiled from T0–T24 | B1–B6 — state which bands were hit, missed, or not applicable |
| T72.6 | One-line lesson | Synthesized | — (recorded for the technique/charter loop) |

---

## Known measurement caveats
1. **Per-post vs. account-level IG likes do not reconcile.** Hydrated per-post likes summed to 19 while account-level 7d likes = 91 (2026-09-14 snapshot). The post-level `likes` field is partial/rolling or a different counter than the insights metric. **Report both; do not sum or substitute one for the other.**
2. **GitHub traffic API needs auth.** Stars/forks/watchers are public; traffic/visitor counts require an authenticated token. Until the founder authorizes one, repo-traffic expectations (B4/B6) are inference only — mark any traffic figure as INFERRED, never measured.
3. **HN benchmarks are public-source averages (2023–2026), not guarantees.** The arXiv study explicitly found the Show HN tag adds no measured star advantage — content and the honesty contract carry the post. Label averages as averages.
4. **Interaction dimension codes unverified.** `interactions_by_media_type` returns numeric dimension codes ('2', '3', '16') with no legend — leave uninterpreted, not used in comparisons.
5. **Comment reads:** zero comments existed on all six posts at last snapshot; if comments appear on the thread or posts, use `fetch-post-comments` for post comments. Thread comment quality is qualitative signal, not a benchmark.
6. **Nothing launched yet.** All checkpoints are pre-registered definitions. If a checkpoint fires with no thread URL, the launch did not happen — log the gap, don't backfill numbers.

## Snapshot log format (append, don't overwrite)
```
[T=0] 2026-09-16 05:02 PT — thread: <URL> — repo: 0/0/0 — IG 24h: reach=X views=Y int=Z pv=W taps=V
[+1h] ... — points=X — rank=<front?/newest #N> — comments=C — repo: S/F/W — X impr=I
[+4h] ... — points=X — B1: hit|miss — repo: S/F/W
[+24h] ... — /front: yes|no — repo: S/F/W — IG 24h deltas: ...
[+72h] ... — final: points=X comments=C — repo: S/F/W — IG 7d vs baseline — lesson: "..."
```
