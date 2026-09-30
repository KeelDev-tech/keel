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

Python 3.11+ on Linux; CI tests 3.11 and 3.12. Use a separate virtual
environment with the same complete pinned dependency set as CI:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.lock
.venv/bin/python -m pip check
source .venv/bin/activate
```

Select an installed Python 3.11 interpreter instead if validating that CI leg.
Use the virtual environment explicitly in every new shell; an install script's
PATH changes do not prove that later sessions use it. Recheck the pins and run
the workflow commands after a fresh environment launch. Cloud configuration
must be reviewed/published separately; a successful current-session install
does not establish persistence.

`setup.sh` initializes applicant workspace templates; it does **not** install
test dependencies or prompt for identity. For a synthetic first-run check, use
a separate temporary directory, never a live workspace or the source tree:

```bash
KEEL_CHECK_PARENT="$(mktemp -d)"
KEEL_PYTHON="$PWD/.venv/bin/python" ./setup.sh "$KEEL_CHECK_PARENT/fresh"
KEEL_PYTHON="$PWD/.venv/bin/python" ./start.sh "$KEEL_CHECK_PARENT/fresh"
# Expected exit 1: first_name, last_name and email remain unknown.
.venv/bin/python -S keel.py --home "$KEEL_CHECK_PARENT/demo" demo
.venv/bin/python -m keel_next doctor --home "$KEEL_CHECK_PARENT/advanced"
```

The advanced storage diagnostic is read-only: it reports the production
ancestor guard's result and observed owner/mode metadata without creating a
workspace or opening its databases. `ANCESTORS_ACCEPTED` checks only that chain;
it does not prove private storage permissions, writable storage, or execution
authority. A BLOCKED/UNAVAILABLE result requires a compatible host. Every
ancestor, including `/`, must have an allowed owner. Changing TMPDIR alone
cannot fix an untrusted root owner. Do not relax the guard, change ownership or
permissions, or skip the failing tests to manufacture readiness.

Run the exact commands in `.github/workflows/ci.yml` and
`.github/workflows/recovery-profile.yml` with temporary `KEEL_HOME` and
`RUNNER_TEMP` directories. CI explicitly selects pytest function tests because
unittest discovery omits them, including operational control/runtime and
machine contracts. The broad `tools/run_tests.py` runner requires its separately
supplied audit guard; do not bypass that refusal or describe direct CI commands
as verified audit-hook isolation. Record unavailable suites and skips.

Keep host integration separate from development. Do not inspect existing Muse
state with a writable `Coordinator`: another controller advances the generation
and fences leases. Only `Coordinator.open_readonly`, `inspect_home`, or the
snapshot CLI are passive inspection interfaces. Follow
[the Muse handoff](docs/MUSE_HANDOFF.md) and [host handoff](docs/HOST_HANDOFF.md):
port against the actual host tree, stop on conflicts, and preserve host config.
Offline development grants no live provider or submission authority.

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

## Test standards (CI enforces these)

CI installs the pinned development requirements, runs `unittest` discovery,
the explicit optimization/security pytest suite, and the matched cache
benchmark on Python 3.11 and 3.12. A test that cannot run from a clean
checkout is a defect. Three hard rules:

1. **Keep the local runtime standard-library-only.** Test dependencies must
   be declared in `requirements-dev.txt`. Add pytest function tests to an
   explicit CI pytest step; unittest discovery does not execute them. Optional
   runtime integrations must report unavailable dependencies explicitly and
   must not silently bypass a required check.
2. **Never `sys.path.insert` an absolute machine-local path at import
   time.** It shadows same-named test modules during unittest discovery
   and breaks CI on any machine where the directory exists. Load
   local-only engines lazily by absolute path instead — never via
   `sys.path`.
3. **Tracked tests must not depend on git-ignored files without a skip.**
   If your test loads something from an ignored directory (e.g.
   `monitors/`), raise `unittest.SkipTest` when it's absent. A test that
   errors on a clean checkout is a broken test.

Before opening your PR, run the suite exactly as CI does, from the repo
root using the commands in `.github/workflows/ci.yml`. A local pass on one
Python version does not establish that both CI versions passed.

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
