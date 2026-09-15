# Keel

An open-core job-application pipeline. Discovery → scoring → materials →
verification → launch packets — with honest automation as the product: it only
ever claims what you tell it is true.

Keel is the public half of a real production pipeline that verified
**53 submitted applications in ~48 hours** using this exact discipline: fit
scoring, truthfulness gates, clean-form checks, and fail-closed handling. The
execution layer (how applications are actually submitted) stays private by
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

## Quick start

```bash
./setup.sh          # "Make it mine" — personalizes your working copy
# edit data/applicant_profile.json and data/answer_bank.json with YOUR truth
./start.sh          # status overview
```

Then:

```bash
KEEL_HOME=$PWD python3 engines/score_roles.py --in roles.json --out data/scored.json
KEEL_HOME=$PWD python3 engines/apply_loop.py          # build one launch packet
KEEL_HOME=$PWD python3 engines/build_dashboard.py    # render the dashboard
python3 -m pytest tests/                                    # run the test suite
```

## The honest-automation contract

1. **Truthfulness gates** — hard requirements the profile can't support are
   reported as gaps, never bridged with fiction.
2. **Explicit confirmation** — a submission counts only on explicit
   confirmation evidence. Nothing else.
3. **Fail closed** — unverifiable postings, unmappable required questions,
   missing attestations: park, never proceed.
4. **No fingerprinting surface** — nothing in this repo helps ATS vendors
   identify or block automated applications (see SPLIT.md).

## Project layout

```
engines/        all pipeline modules (flat package)
tests/          acceptance tests
docs/           architecture, personalization, contributing
sample_data/    sanitized examples (never real applications)
dist/           built zips (from ./package.sh)
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system picture
and [docs/PERSONALIZE.md](docs/PERSONALIZE.md) for the friend guide.

## License

Apache-2.0 — see [LICENSE](LICENSE).
