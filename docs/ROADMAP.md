# Keel Roadmap

Where the project is, and where it's going. No invented timelines: items are
**Shipped**, **Planned** (real direction, no dates promised), or **Exploring**
(ideas under consideration). Claims reference canon claim IDs, never free-text
numbers.

## Shipped

### v0.1.0 — 2026-09-14 (first public release)
- Discovery/scoring engine: search playbooks, sweep prompts, the 100-point
  fit model, generic scorer template.
- Truthful resume tailoring: templates driven by the operator's applicant
  profile, with truthfulness gates.
- Sanitized answer bank: canonical answers + banded-question rules + hard
  gates (ships with example data only — real profiles are never committed).
- Prescreen gates: automated checks that park leads needing operator input
  instead of inventing answers.
- ATS detection and capability radar: platform identification + HTTP-level
  probes of which submission paths are viable (detection only — no
  submission behavior).
- Launch-packet builder: the public application loop prepares complete
  launch packets and stops there, per the open-core boundary in `SPLIT.md`.
- `verify_retry`: verification worker that re-checks parked leads over
  plain HTTP and promotes live postings or marks dead ones.
- Feeder watchdog: watches the ready queue and verification pool so a dry
  queue surfaces instead of going silent.
- Telemetry framework: append-only, additive-only event logging.
- Outcome analytics: ledger + telemetry analysis with fail-closed
  reporting.
- Pluggable inbox listener: template for wiring operator responses back
  into the system.
- HTML dashboard builder: self-contained dashboard generated from the
  ledger and queues.
- Setup wizard and stock config: guided first run with sane defaults and
  example data.
- Apache-2.0 license; stdlib-only (no third-party dependencies).

### Post-0.1.0 (on main, not yet released)
- GEO serving layer: `llms.txt`, sitemap, keyword pages, `stats.json`,
  releases feed, demo GIF (`site/`).
- GEO measurement pipeline: canonical recounts, probes, snapshots.
- Brand: Keel spine logo (`docs/assets/keel-logo.png`) + README header.

### Production evidence
The private production pipeline behind this repo runs the truthfulness
contract (`three-rules`) continuously. Canon `proof-94-submissions`: the
ledger holds **94 verified submissions** as of 2026-09-15 — 94 evidenced /
0 url-only / 0 unevidenced. The public repo itself makes no submission
claims; it's the tools, the gates, and the telemetry.

## Planned (real direction, no dates)

- Show HN launch (post pending warmup; see `launch/show-hn-final.md`).
- X launch thread (drafted; blocked on the founder's X account call).
- Public feedback round on the honest-automation contract and the open-core
  boundary — the three questions from canon `the-ask`.
- Contributor intake around the three areas named in the launch: ATS
  behavior, answer-bank ergonomics, outcome analytics.
- Ongoing GEO/measurement refreshes so every public number stays
  ledger-verifiable.

## Exploring (no commitments)

- Seed-stage conversations with people who care about automation that
  refuses to lie (canon `the-ask`).
- Answer-bank ergonomics driven by operator feedback.
- Stronger outcome analytics (more observed outcomes before any metric
  claims — analytics fail closed with < 3 data points).

## Not planned

- Publishing the private submission-behavior layer (SPLIT.md boundary is
  deliberate: those techniques are fingerprintable by ATS vendors).
- User counts, growth projections, revenue, or funding-status claims
  (canon `no-projections`).
- Anything that weakens the truthfulness contract for volume.
