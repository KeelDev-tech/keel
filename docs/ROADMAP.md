# Keel Roadmap

Where the project is, and where it's going. No invented features: every item
below is traceable to something real — a shipped release, a committed file, a
draft document, or a tracked canon claim. Each item names its source.

**Buckets:** **Now** (shipped or in progress, with evidence), **Next**
(planned — real direction with a concrete artifact in the repo, no dates
promised), **Later** (exploring — explicitly speculative, no commitment).

Only what's in this repo and the public record is claimed. The private
production pipeline behind Keel is separate; the public repo makes no
submission claims of its own.

## Now — shipped

- **v0.1.0 — 2026-09-14 (first public release).**
  Discovery/scoring engine (search playbooks, sweep prompts, 100-point fit
  model, generic scorer template); truthful resume tailoring with
  truthfulness gates; sanitized answer bank (canonical answers,
  banded-question rules, hard gates — ships with example data only);
  prescreen gates that park leads instead of inventing answers; ATS
  detection + capability radar (detection only — no submission behavior);
  launch-packet builder (the public loop ends at the packet, per the
  open-core boundary in `SPLIT.md`); `verify_retry` verification worker;
  feeder watchdog; append-only telemetry; outcome analytics with
  fail-closed reporting; pluggable inbox listener; HTML dashboard builder;
  setup wizard + stock config. Apache-2.0, stdlib-only.
  _Source: `CHANGELOG.md` ([0.1.0]), `docs/releases/v0.1.0.md`._
- **GEO serving layer (on `main`, post-0.1.0, not yet released).**
  `site/llms.txt`, sitemap, keyword pages, `stats.json`, releases feed,
  demo GIF — the machine-readable public surface for citation engines.
  _Source: git commits `2eb6470`, `9890a0c`; `site/`, `docs/geo/`._
- **GEO measurement pipeline (on `main`, post-0.1.0).**
  `geo-pipeline/recount.py` (canonical ledger+telemetry recount →
  `docs/geo/stats.json`), `probe.py` (public-web probes, no auth),
  `snapshot.py` (archives the pair). Public-safe: reads private local
  files but outputs aggregates only. Fail-closed: any source read error →
  non-zero exit, no partial write.
  _Source: git commit `4b21742`; `geo-pipeline/README.md`._
- **Brand identity.**
  Keel spine logo + README header.
  _Source: git commit `8944a2b`; `docs/assets/keel-logo.png`._
- **Sponsorship entry points.**
  `.github/FUNDING.yml` names the platforms actually set up (GitHub
  Sponsors org profile placeholder, Open Collective provisional slug);
  unset platforms stay commented out so no dead buttons render.
  _Source: `.github/FUNDING.yml`; git commit `42bb29e`._

## Next — planned (evidence in hand, no dates)

- **Show HN launch.**
  Post is finalized; submission pending the `keeldev` HN account warmup and
  the founder's explicit approval to post.
  _Source: `launch/show-hn-final.md`; `launch/hn-warmup-log.md`;
  `launch/launch-thread-rollout-plan.md`._
- **Launch thread (6-post thread).**
  Copy is finalized; platform TBD at approval time; rollout plan written
  around the Show HN anchor. Blocked on the founder's X account call.
  _Source: `launch/launch-thread-final.md`;
  `launch/launch-thread-rollout-plan.md`._
- **Public feedback round on the honest-automation contract.**
  The three asks carried in the launch copy: feedback on the
  honest-automation contract and the open-core boundary; contributors
  around ATS behavior, answer-bank ergonomics, and outcome analytics;
  seed-stage conversations with people who care about automation that
  refuses to lie.
  _Source: canon claim `the-ask` (locator: `launch-thread-final.md` 6/,
  `talking-points.md`)._
- **Contributor intake around the three ask areas.**
  ATS behavior, answer-bank ergonomics, outcome analytics. The standing
  DevRel posture is a good-first-issue pipeline: 5+ labeled, bounded,
  boundary-safe issues kept open.
  _Source: canon claim `the-ask`; `marketing-engine/backlog/devrel-docs.md`
  (good-first-issue pipeline)._
- **Ongoing GEO/measurement refreshes.**
  The measurement pipeline exists to be run on a schedule so every public
  number stays ledger-verifiable; snapshot history is committed in
  `geo-pipeline/history/`.
  _Source: `geo-pipeline/README.md`; `geo-pipeline/history/snapshots.jsonl`._
- **Sponsorship plumbing activation.**
  Two activation steps are tracked in-file: correct the `open_collective`
  slug on Open Source Collective approval, and enable the `github` key
  only after the GitHub Sponsors org profile is live.
  _Source: `.github/FUNDING.yml` comments._
- **Doctrine canonicalization.**
  Proposal: adopt `KEEL_DOCTRINE.md` as canonical and archive
  `DOCTRINE.md`. Awaiting the founder's approval of the doctrine text.
  _Source: `INTEGRATION_PLAN.md` P3 (proposal, 2026-09-15)._

## Later — exploring (no commitments)

- **Sibling department repos under KeelDev-tech.**
  Proposed: `keel-forge` (Forge tooling — queue schema, prompt playbooks)
  and `keel-shield` (sanitized hygiene/FIM tooling), alongside this repo.
  Each new public repo needs the founder's per-repo approval; private
  execution layers stay out of every public repo.
  _Source: `INTEGRATION_PLAN.md` P5 (proposal, 2026-09-15)._
- **Grant funding (NLnet general open call).**
  Draft exists for the 2026-11-03 deadline, framed as "Keel Honesty
  Kernel: fail-closed verification architecture for trustworthy AI
  automation." Submission requires the founder's explicit word; nothing is
  filed yet.
  _Source: `funding/nlnet-2026-11-03/draft.md` (draft, not submitted)._
- **Seed-stage conversations.**
  Canon `the-ask` names conversations with people who care about
  automation that refuses to lie — an interest, not a plan.
  _Source: canon claim `the-ask`._
- **Stronger outcome analytics as observed data grows.**
  `engines/outcome_analytics.py` reports fail-closed on thin data, so any
  metric claims wait until the ledger holds enough observed outcomes.
  _Source: `engines/outcome_analytics.py`; `docs/ARCHITECTURE.md`._
- **Answer-bank ergonomics driven by operator feedback.**
  Canon `the-ask` lists answer-bank ergonomics as a contributor area;
  nothing is designed yet.
  _Source: canon claim `the-ask`._

## Not planned

- **Publishing the private submission-behavior layer.**
  The `SPLIT.md` boundary is deliberate: those techniques are
  fingerprintable by ATS vendors, so they stay out of the public repo.
  _Source: `SPLIT.md`; `docs/ARCHITECTURE.md` ("No fingerprinting
  surface")._
- **User counts, growth projections, revenue, or funding-status claims.**
  Forbidden by canon: only claim what can be shown.
  _Source: canon `no-projections` (`marketing-engine/canon/claims.json`,
  `forbidden` list); `launch/talking-points.md`._
- **Anything that weakens the truthfulness contract for volume.**
  The three rules are the product; throughput never outranks honesty.
  _Source: `DOCTRINE.md`; `launch/show-hn-final.md`._

---

This file supersedes `docs/ROADMAP.md`. It is the repo-root public roadmap;
the issue tracker carries the fine-grained task list. No timelines are
promised — items move from Later to Next when evidence arrives, not before.
