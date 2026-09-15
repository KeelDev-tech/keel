# Personalize — the friend guide

This is the "Make it mine" guide. Your copy of Keel is yours: the
example files are templates, and `./setup.sh` created your personal working
copies under `data/`. Nothing personal ever goes back into the shared repo.

## 1. Run the wizard

```bash
./setup.sh
```

It copies the examples into `data/` (your answer bank, applicant profile,
employer blocklist, queues) and asks for your name, email, phone, location,
LinkedIn, and timezone. Your identity flows into the answer bank and profile.

## 2. Tell the truth, completely

Two files carry everything the pipeline is allowed to claim about you:

- **`data/applicant_profile.json`** — your real history, real credentials,
  real education. Every bullet must be something you actually did. Year-only
  dates unless you document exact ones.
- **`data/answer_bank.json`** — your canonical form answers (work auth,
  tenure figures, education, availability) plus the banded-question rules and
  hard gates.

The pipeline's core rule: **it only ever claims what you tell it is true.**
If a role demands something your profile can't support, the tailor reports
the gap — it never invents a bridge. Applications you wouldn't defend in an
interview are applications you don't file.

## 3. Set your lanes and blocklist

- `keel.config.json` — your metro area, target lanes, timezone,
  daily application cap.
- `data/employer-blocklist.md` — employers you will never apply to. Check
  it before every new lane, and keep adding to it.

## 4. Discover and score

Fill the placeholders in `engines/discovery_queries.md` with your geography
and industries, run your discovery sweeps (see
`engines/sweep_worker_prompt.md`), then score what you find:

```bash
python3 engines/score_roles.py --in discovered.json --out data/scored.json --profile data/applicant_profile.json
```

The rubric is in `engines/fit-scoring-model.md`. Adapt the lane weights to
your search — the template's hard-requirement classifier is wired; the
judgment-heavy components are yours to fill (or judge by hand).

## 5. Prepare materials

```bash
python3 engines/resume_tailor.py --profile data/applicant_profile.json \
    --role role_brief.json --out data/resumes/my_resume.pdf
```

Cover letters: use `engines/cover_letter_generator.md` with the
placeholders filled.

## 6. Run the loop

```bash
KEEL_HOME=$PWD python3 engines/apply_loop.py
```

Builds a launch packet for your best eligible READY lead. The packet's
EXECUTOR CONTRACT describes what your submission layer must do. Items the
prescreen flags (essays, attestations, unmappable questions) park in your
input queue — handle them yourself, truthfully, or skip the role.

## 7. Track and learn

```bash
KEEL_HOME=$PWD python3 engines/verify_retry.py          # dry-run re-verification
KEEL_HOME=$PWD python3 engines/build_dashboard.py      # regenerate dashboard
./start.sh                                                    # status overview
```

Open `dashboard/dashboard.html` in a browser. Set a recurring schedule for
the dashboard build and the status ping if your platform supports it
(e.g. cron every 30 minutes).

## What never leaves your machine

`data/` — your profile, answer bank, queues, ledger, mail, launch packets —
is your working copy. `./package.sh` excludes it from the distributable zip
by construction (and a pre-flight scan blocks the build if personal data
snuck into the payload).

## Getting better over time

- Log everything: `engines/log_event.py` and `record_outcome.py` feed
  `telemetry/events.jsonl`.
- Review outcomes weekly: `engines/outcome_analytics.py` shows which lanes
  convert — with fail-closed rules on thin data.
- Feed the radar: `engines/edge_probe.py` probes ATS platforms monthly so
  you learn which submission paths actually work.
