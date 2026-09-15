# Keel

<!-- Repo is live at KeelDev-tech/keel. -->
[![CI](https://github.com/KeelDev-tech/keel/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-informational.svg)](CHANGELOG.md)

![Keel wordmark](docs/assets/keel-wordmark.svg)

An open-core job-application pipeline. Discovery → scoring → materials →
verification → launch packets — with honest automation as the product: it only
ever claims what you tell it is true.

Keel is the public half of a real production pipeline that verified
**55 submitted applications** using this exact discipline: fit
scoring, truthfulness gates, clean-form checks, and fail-closed handling.
The execution layer (how applications are actually submitted) stays private by
design — publishing submission fingerprints would get the pipeline blocked by
ATS vendors. See [SPLIT.md](SPLIT.md).

## What it does

- **Discovery** — search query playbooks and sweep prompts for finding real
  postings in your target lanes (`engines/discovery_queries.md`,
  `engines/sweep_worker_prompt.md`).
- **Scoring** — a 100-point fit model with a banded action policy
  (`engines/fit-scoring-model.md`, `engines/score_roles.py`).
- **Materials** — truthful resume tailoring from your verified profile
  (`engines/resume_tailor.py`, `engines/cover_letter_generator.md`).
- **Answer bank** — your canonical form answers + banded-question rules +
  hard gates. The single source of truth; the pipeline never invents what is
  not in it (`engines/answer_bank.example.json`).
- **Prescreen** — pre-launch packet screening: office/relocation/travel
  commitments, essays, attestations, and unmappable required questions get
  PARKED to your input queue, never invented (`engines/prescreen.py`).
- **ATS detection** — platform identification and a capability radar that
  probes whether direct submission is viable (`engines/ats.py`,
  `engines/edge_probe.py`, `engines/api_direct_detect.py`).
- **Apply loop** — builds launch packets for eligible READY leads: verified
  form values, banded rules, hard gates, and the per-field verification
  protocol, with a documented EXECUTOR CONTRACT for your submission layer
  (`engines/apply_loop.py`).
- **Verification retry** — re-verifies parked leads (HTTP liveness, board
  APIs), promotes LIVE ones, buries dead ones (`engines/verify_retry.py`).
- **Telemetry & analytics** — append-only event log, outcome analytics with
  fail-closed reporting rules, employer-response intake via a pluggable mail
  source (`engines/log_event.py`, `engines/outcome_analytics.py`,
  `engines/inbox_listener.py`).
- **Dashboard** — self-contained HTML dashboard from ledger + queues
  (`engines/build_dashboard.py`).

![Dashboard](docs/assets/dashboard-screenshot.png)

## What it does NOT do

- **Keel never submits an application.** The public loop stops at the
  *launch packet*: a verified, prescreened bundle (form values, banded rules,
  hard gates, per-field verification protocol) plus a documented
  EXECUTOR CONTRACT for whatever submission layer you attach. Managed
  execution is the hosted tier — keeping it private also protects it from
  ATS fingerprinting at scale.
- **Keel never invents qualifications.** Anything your profile can't support
  is reported as a gap, never bridged with fiction.
- **Keel makes no submission claims.** The public repo is the discipline and
  the tools. The 53-in-48-hours figure belongs to the private production
  pipeline that proved the discipline works.

## Quick start (~5 minutes, from the downloaded zip)

```bash
./setup.sh          # "Make it mine" — personalizes your working copy
                    # (non-interactive shells skip the prompts; edit data/
                    #  with YOUR truth afterward)
# edit data/applicant_profile.json and data/answer_bank.json with YOUR truth
./start.sh          # status overview
```

Then (run from the workspace root):

```bash
KEEL_HOME=$PWD python3 engines/score_roles.py --in sample_data/discovered_roles.example.json --out data/scored.json
KEEL_HOME=$PWD python3 engines/apply_loop.py          # build one launch packet
KEEL_HOME=$PWD python3 engines/build_dashboard.py    # render the dashboard
python3 -m unittest discover -s tests                 # run the test suite
```

Requirements: Python 3.10+ — stdlib only, no dependencies to install.
CI runs the same suite on 3.10 / 3.11 / 3.12
([ci.yml](.github/workflows/ci.yml)).

## The honest-automation contract

1. **Truthfulness gates** — hard requirements the profile can't support are
   reported as gaps, never bridged with fiction.
2. **Explicit confirmation** — a submission counts only on explicit
   confirmation evidence. Nothing else.
3. **Fail closed** — unverifiable postings, unmappable required questions,
   missing attestations: park, never proceed.
4. **No fingerprinting surface** — nothing in this repo helps ATS vendors
   identify or block automated applications (see SPLIT.md).

## Architecture

Keel is a flat `engines/` package of small, single-purpose modules —
discovery, scoring, materials, prescreen, ATS detection, the apply loop,
verification retry, telemetry, and the dashboard builder — wired together by
`keel_paths.py` (home-directory resolution) and guarded by the
honest-automation contract above. Two files define the project's shape:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the full system picture:
  module map, data flow, queue/ledger conventions, extension points.
- [SPLIT.md](SPLIT.md) — the open-core boundary: exactly what is public,
  what stays private, and why.

Start with [docs/PERSONALIZE.md](docs/PERSONALIZE.md) to make a copy yours.

## Project layout

```
engines/        all pipeline modules (flat package)
tests/          acceptance tests
docs/           architecture, personalization, contributing
docs/assets/    wordmark, social preview, dashboard screenshot
sample_data/    sanitized examples (never real applications)
launch/         launch drafts (Show HN, thread, talking points)
dist/           built zips (from ./package.sh)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor guide
(the technical ground rules also live in [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)). Bug reports and feature
requests live under [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/);
security reports go through GitHub Security Advisories —
see [SECURITY.md](SECURITY.md). Changes are tracked in
[CHANGELOG.md](CHANGELOG.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
