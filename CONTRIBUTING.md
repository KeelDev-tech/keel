# Contributing to Keel

## What Keel is

Keel is an open-core job-application autopilot with a truthfulness contract:
it only ever claims what you tell it is true. Discovery, fit scoring, truthful
resume tailoring, a canonical answer bank, prescreen gates, ATS detection, and
an apply loop that stops at a launch packet — everything before submission is
public. The pipeline never invents qualifications, never counts a submission
without explicit confirmation evidence, and fails closed on anything it can't
verify. That discipline is the product. Everything you contribute here serves
it or doesn't belong.

## The open-core boundary

Read [SPLIT.md](SPLIT.md) before you write a line. This repo is the public
half. Anything that would help an ATS vendor fingerprint or block automated
applications stays private: form-event sequencing, per-platform commit
techniques, CAPTCHA-handling specifics, credential and verification-code
flows, the live API-direct transport. Detection is public; submission behavior
is private.

The rule of thumb: if publishing your change helps a vendor block automated
applications, it doesn't belong here. If it helps an applicant run an honest,
verifiable, fail-closed pipeline, it does. When in doubt, open an issue and
ask before building — we'd rather talk you out of a wasted week than review
a PR we have to reject.

Do not submit PRs probing, requesting, or reverse-engineering the private
side. They will be closed.

## Dev setup

Python 3.10+, stdlib only — no dependencies to install, no virtualenv to
build. CI runs the same suite on 3.10 / 3.11 / 3.12.

```bash
./setup.sh          # "Make it mine" — copies examples into data/, walks you
                    # through identity fields. Non-interactive shells skip
                    # the prompts; edit data/ with YOUR truth afterward.
./start.sh          # status overview: answer bank, queues, ledger, dashboard
```

Then, from the repo root:

```bash
KEEL_HOME=$PWD python3 engines/score_roles.py --in sample_data/discovered_roles.example.json --out data/scored.json
KEEL_HOME=$PWD python3 engines/apply_loop.py          # build one launch packet
KEEL_HOME=$PWD python3 engines/build_dashboard.py    # render the dashboard
python3 -m unittest discover -s tests                 # full test suite
python3 -m py_compile engines/*.py                    # syntax check
```

Conventions that matter:

- **Config-driven via `KEEL_HOME`.** Engines resolve paths through
  `engines/keel_paths.py`. Never hardcode a home directory.
- **Import-safe modules.** No side effects on import.
- **Sanitized everything.** Never commit real names, emails, phones,
  addresses, social profile URLs, real employer names, or application
  history. Examples use `example.com` placeholders.
  `./package.sh` scans the payload and blocks the build on hits.
- **Append-only telemetry.** Corrections are new events, never rewrites.
  One lead lives in one queue.
- **Gate vocabulary is centralized.** New gate types go in the `GATE_TYPES`
  set in `engines/log_event.py`. New ATS patterns go in the additive
  pattern table in `engines/ats.py`.

## How to contribute

**Issues first for anything non-trivial.** Bug reports and feature requests
live under `.github/ISSUE_TEMPLATE/`. Security issues do *not* go in public
issues — see [SECURITY.md](SECURITY.md) and use GitHub Security Advisories.

**PRs.** The [PR template](.github/PULL_REQUEST_TEMPLATE.md) asks for four
things: what changed, why, the tests you ran, and a checklist. The checklist
is not ceremony:

- No personal data anywhere in the diff.
- Tests green — `python3 -m unittest discover -s tests`, and new tests for
  new behavior. Name them and what they cover.
- Docs updated if behavior changed (README / docs / a CHANGELOG entry under
  `[Unreleased]`).
- The change reviewed against the honest-automation contract: truthfulness
  gates intact, fail-closed on unverifiable input, respects the open-core
  boundary in SPLIT.md.

**The claim-gating ethos — read this twice.** Every claim in this project's
code, docs, and comments must be verifiable. No invented numbers. No
"thousands of users", no performance figures without a ledger row and an
explicit confirmation behind them, no "48 hours" windows you can't prove from
timestamps. Dashboard numbers come from the ledger and explicit confirmation
rows — never narrated ahead of evidence. When a rate has too few data points,
the project says "insufficient data", not a guess. This is the project's
soul: an honest-automation system that tolerates unverifiable claims in its
own repo would be a fraud. Hold the line.

## Good first issues

We tag starter tasks `good-first-issue`. A good one in *this* repo has three
properties: it's bounded (one module, one behavior), it's verifiable (you can
write a test that proves it), and it can't leak across the open-core boundary.

Strong starters:

- **New ATS detection patterns** — a new platform in `engines/ats.py`'s
  additive pattern table, read-only identification only, with tests.
- **New gate vocabulary** — a new gate type in `log_event.py`'s `GATE_TYPES`
  set with a documented meaning, wired into one engine.
- **Prescreen rules** — a new form-intel check in `engines/prescreen.py`
  (essays, attestations, commitments) with a failing test first.
- **Dashboard and docs** — rendering fixes in `build_dashboard.py`, broken
  links, unclear setup steps, better `sample_data/` examples.
- **Tests for uncovered engines** — `tests/` currently covers employer
  patterns and prescreen. Pick an untested engine, write the suite.

Weak starters: anything touching submission behavior, anything needing real
applicant data, anything that "just" refactors the architecture. Ask in the
issue thread if you're unsure where a task lands.

## Code of conduct

Honesty is the price of admission. No spam PRs, no credential-harvesting
"integrations", no fabricated benchmarks, no invented testimonials, no
attempts to extract the private execution layer. Contributions that invent,
mislead, or expose other people's data are rejected and their authors are not
welcome back. Treat other contributors' time as expensive — come with
evidence, keep it tight, and leave the repo more truthful than you found it.
