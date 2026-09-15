# Changelog

All notable changes to Keel are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
