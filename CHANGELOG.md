# Changelog

All notable changes to Keel are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Safety rails as code: `docs/OPERATING-CONSTRAINTS.md` documents the
  numbered operating constraints (C-01…C-20), each enforced in a named
  engine module; the operator may extend them but never weaken them.
- Credentials-vault convention (`docs/CREDENTIALS.md`): one file per
  account, `0600` permissions, gitignored and excluded from packaging.
- Operator policy template (`engines/policy.example.json`): travel /
  office / relocation caps, attestation hard-stop classes, and the
  pre-authorized attestation scope — example data only.
- ATS discovery pack (`engines/ats_discovery/`): additional platform
  adapters for board enumeration (detection only — no submission
  behavior).
- Direct ATS detection: Greenhouse/Lever/Ashby JSON board enumeration
  and board classification (detection only — no submission behavior).
- Outcome-tracking module (`engines/outcome_tracking/`): evidence gate
  for submission claims and invite/offer surfaces over the append-only
  telemetry log.
- Input-resolution module (`engines/input_resolution/`): structured
  handling of operator-input blockers with banking of reusable answers.
- Launch lock (`engines/launch_lock.py`): one live application task per
  role, so concurrent lanes cannot double-fire the same lead.
- Lane watchdog (`engines/lane_watchdog.py`): evaluates pipeline health
  red lines against live data.
- Cost metering (`engines/cost_model.py`, `engines/cost_tracker.py`):
  per-submission cost dimensions recorded additively on ledger rows;
  zero-submission days render as explicit gaps, never interpolated.
- Discovery-side dedupe gate (`engines/dedupe_gate.py`) and input-tray
  classifier (`engines/genuine_pat.py`): parked items are classified so
  genuinely operator-blocked leads stay parked while verification-only
  items return to the verify pool.
- Packet starvation watchdog (`engines/packet_watchdog.py`), clean-board
  watch (`engines/clean_board_watch.py`), and page capture
  (`engines/page_capture.py`) for discovery hygiene.
- Field-question protocol (`engines/field_question_protocol.py`):
  classifies form questions as answerable, derivable, or
  applicant-only — applicant-only items park, never invented.
- Batch staged-launch helper (`engines/batch_staged_launches.py`) and
  parked-task sweep (`engines/parked_task_sweep.py`).
- Worker charter (`worker-charter/`): the autonomous-worker prompt
  template with constraint-tagged exemplars; the public assembler
  deliberately does not mine the private technique library.

### Fixed
- `setup.sh` writes queue files in the canonical top-level-list form and
  no longer mutates the tracked `keel.config.json`; identity answers land
  in `data/` only.
- `docs/PERSONALIZE.md` rewritten as the single operator-neutral
  "clone → first launch packet" guide.

## [0.2.0] — 2026-09-15

### Added
- Public web home (`site/`): static GitHub-Pages-ready page with
  `llms.txt`, sitemap, and OG hero image — a visitor-facing front door
  for the repo.
- GEO discoverability layer: citation-ready docs under `docs/geo/`
  (comparison, FAQ, alternatives keyword pages), `llms.txt` at the repo
  root, JSON-LD structured data, `docs/releases.xml` release feed, and
  `docs/assets/demo.gif`.
- GEO measurement pipeline: canonical recount probes and snapshot
  history (`docs/geo/stats.json`,
  `geo-pipeline/history/snapshots.jsonl`) tracking verified submission
  figures over time.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) running the
  stdlib test suite (`python3 -m unittest discover -s tests`) on every
  push.
- Keel Doctrine (`KEEL_DOCTRINE.md`): one operating spine across the
  five departments.
- `docs/ROADMAP.md`: public roadmap covering shipped work (v0.1.0),
  post-release items, and planned direction — no timelines.
- Keel spine logo (`docs/assets/keel-logo.png`) in the README header.
- Contributor footing: `CONTRIBUTING.md` onboarding,
  `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1, no personal contact
  data), and `.github/FUNDING.yml` sponsorship entry points.
- README cold-visitor audit: the quickstart is genuinely end-to-end —
  a documented demo-seed step bridges `score_roles` output into the
  READY queue so `apply_loop` builds a launch packet; network note
  added (read-only HTTP liveness/form-intel probes); submission figure
  refreshed to the ledger-verified canon (94 verified, 2026-09-15).
- `docs/ARCHITECTURE.md` accuracy pass: telemetry path corrected to
  `data/telemetry/events.jsonl`, event-type list aligned to
  `log_event.py`'s stable list, data-flow step 2 corrected (score_roles
  sets no status — the queue owns it), live re-verify tri-state
  documented, `keel_paths.py` added to the module guide.

### Fixed
- Pre-launch hygiene: CONTRIBUTING test command now matches the README
  (`python3 -m unittest discover -s tests`; stdlib only, no pytest).
- CI: guard `sys.exit` so unittest discovery can import
  `test_employer_patterns`.
- Sample docs cross-references are now working relative links
  (`docs/samples/` → `../SPLIT.md`, `README.md`).
- Launch working docs: removed private-business naming from the
  no-mention rule and private workspace paths from measurement notes.
- README: ledger-verified 55 submissions at launch; unprovable "48
  hours" window dropped.
- GEO layer: unverified rival claims replaced with a verified set;
  launch data-story audited and corrected.
- Cleanliness audit: founder name redacted from launch docs; internal
  docs/credentials quarantined from the public tree via `.gitignore`.

## [0.1.0] — 2026-09-14

First public release of the Keel open-core job-application autopilot.

### Added
- Discovery/scoring engine: search playbooks, sweep prompts, and the
  100-point fit model with a generic scorer template.
- Truthful resume tailoring: templates driven by the operator's applicant
  profile, with truthfulness gates.
- Sanitized answer bank: canonical answers plus banded-question rules and
  hard gates (ships with example data only — real profiles are never
  committed).
- Prescreen gates: automated checks that park leads needing operator
  input instead of inventing answers.
- ATS detection and capability radar: platform identification plus
  HTTP-level probes of which submission paths are viable (detection
  only — no submission behavior).
- Launch-packet builder: the public application loop prepares complete
  launch packets and stops there, per the open-core boundary in
  SPLIT.md.
- `verify_retry`: verification worker that re-checks parked leads over
  plain HTTP and promotes live postings or marks dead ones.
- Feeder watchdog: watches the ready queue and verification pool so a
  dry queue surfaces instead of going silent.
- Telemetry framework: append-only, additive-only event logging.
- Outcome analytics: ledger + telemetry analysis with fail-closed
  reporting.
- Pluggable inbox listener: template for wiring operator responses
  (employer replies, interview invites) back into the system.
- HTML dashboard builder: self-contained dashboard generated from the
  ledger and queues.
- Setup wizard and stock config: guided first run with sane defaults and
  example data.

### Notes
- Licensed under Apache-2.0. See `LICENSE`.
- Public/private boundary documented in `SPLIT.md`.
- Standard library only — no third-party dependencies to install.
