# Make it mine — the operator guide

Keel is an open job-application pipeline you run on your own machine. It
refuses to lie on your behalf: it only ever claims what you tell it is
true, and anything it can't answer truthfully parks for your judgment.
This guide takes you from a fresh clone to your first launch packet.

## 1. Run the setup wizard

```bash
git clone <keel-repo-url> keel && cd keel
./setup.sh
```

The wizard copies the example templates into `data/` — your private working
copies (answer bank, applicant profile, policy, blocklist, queues) — and
asks for your name, email, phone, location, LinkedIn, and timezone. Example
files stay untouched; `data/` is gitignored and never committable.

## 2. Tell the truth, completely

Everything the pipeline may claim about you lives in two files. Fill them
with verified fact only — year-only dates unless you document exact ones:

- **`data/applicant_profile.json`** — your real history, credentials, and
  education. The resume tailor draws only from here.
- **`data/answer_bank.json`** — your canonical form answers (work auth,
  tenure figures, education, availability) plus banded-question rules and
  hard gates. Every answer should carry a provenance receipt: who said it,
  when, and how it was banked.

If a role demands something your profile can't support, the tailor reports
the gap — it never invents a bridge.

## 3. Set your boundaries

**`data/policy.json`** holds your travel/office/relocation caps and the
attestation classes you refuse (no-AI/unaided-work pledges, essays,
recording consent, facts only you could know). Two notes on wiring:

- The open-repo prescreen parks travel/office/relocation commitments for
  your explicit answer — it never invents a % cap or day count. Your
  policy file keeps your answers consistent and truthful.
- Standard legal attestations you genuinely agree to (arbitration,
  background-check consent, at-will, truthfulness, data-privacy) are
  pre-authorized only via `attestation_scope.preauthorized_attestation_keys`
  in your answer bank. Nothing is pre-authorized out of the box.

Your store of job-site accounts goes in `credentials/` (one file per
account, `chmod 600` — see `docs/CREDENTIALS.md`).

## 4. Configure lanes and blocklist

- `keel.config.json` — your metro area, target lanes, timezone, daily cap.
- `data/employer-blocklist.md` — employers you will never apply to.

## 5. Discover and score

Fill the placeholders in `engines/discovery_queries.md`, run your discovery
sweeps, then score what you find:

```bash
python3 engines/score_roles.py --in discovered.json --out data/scored.json \
    --profile data/applicant_profile.json
```

(The rubric is `engines/fit-scoring-model.md`.)

## 6. Verify, then build a launch packet

```bash
KEEL_HOME=$PWD python3 engines/verify_retry.py   # re-check parked leads over HTTP
KEEL_HOME=$PWD python3 engines/apply_loop.py     # build a launch packet
```

`apply_loop` stops at the **launch packet** (`data/launch-packets/`) — it
documents what to submit, not how. The packet's EXECUTOR CONTRACT describes
the submission step your own layer performs. This is the open/private split
(see `SPLIT.md`): detection and honest form values are public; submission
technique stays private.

Prescreen parks anything dubious — essays, attestations, unmappable
questions — in `data/queues/needs_input-queue.json`. Answer it there and
the lane resumes.

## 7. Watch it work

```bash
KEEL_HOME=$PWD python3 engines/build_dashboard.py
./start.sh
```

Open `dashboard/dashboard.html`. Schedule the dashboard build and status
ping (e.g. cron every 30 minutes) if your platform supports it.

## What never leaves your machine

`data/`, `credentials/`, and your personalized `keel.config.json` are
ignored by git. `./package.sh` builds the distributable zip with a
pre-flight scan that blocks the build if personal data snuck into the
payload.

## The contract, in writing

- `docs/OPERATING-CONSTRAINTS.md` — the safety rails as numbered rules
  (C-01…C-20): what may park, what may never be claimed, how counts are
  validated. Extend them, never weaken them.
- `SPLIT.md` — what is public in this repo and what stays private.
- `DOCTRINE.md` — why this exists: automated applications that refuse to lie.
