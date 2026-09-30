# Make it mine — the operator guide

Use one canonical workspace for your private data and keep it separate from
fresh source checkouts. The source tree contains examples and engines; the
workspace contains the facts you asserted, queues, materials, and observations.

## Initialize without claiming example facts

From the source directory, with Python 3.11+ on Linux or WSL:

```bash
export KEEL_HOME="$HOME/keel-workspace"
./setup.sh
```

You can also pass the workspace explicitly: `./setup.sh /path/to/workspace`.
The wrapper uses `keel.py init`. It creates only missing files and validates
existing JSON instead of overwriting it. Applicant answers, personal policy
commitments, experience, and consent start unknown. Setup does not connect an
account or start a network scan.

## Record your own facts

Record required identity values using provenance receipts:

```bash
python3 keel.py --home "$KEEL_HOME" confirm-answer --key first_name --source "applicant assertion"
python3 keel.py --home "$KEEL_HOME" confirm-answer --key last_name --source "applicant assertion"
python3 keel.py --home "$KEEL_HOME" confirm-answer --key email --source "applicant assertion"
```

Omitting `--value` reads from stdin and avoids placing personal values in your
shell history. This records what the applicant asserted; it does not prove the
assertion's truth. Use `--scope ROLE_ID` for role-specific answers and `--expires`
where the value can become stale. Read `confirm-answer --help` first.

Fill `data/applicant_profile.json` with your actual employment, credentials,
education, and verified capabilities. Supply real materials under the
workspace's `data/resumes/`. The profile and answer bank are different inputs:
changing the profile does not create answer provenance receipts.

## Set your boundaries

`data/policy.json` holds office, travel, relocation, and attestation boundaries.
Unset commitments stay unknown. Questions about essays, no-AI or unaided-work
pledges, recording consent, and facts only you know remain for your explicit
judgment. No attestation consent is pre-authorized by initialization.

Keep any independent executor credentials outside source candidates. The public
CLI does not provide a signed-in submission connector. Never put credentials,
private applicant files, or personalized configuration in a release allowlist.

## Inspect the conversion process

```bash
./start.sh "$KEEL_HOME"
python3 keel.py --home "$KEEL_HOME" pipeline-doctor
python3 keel.py --home "$KEEL_HOME" dashboard
```

`start.sh` runs the offline doctor, supply, and conversion reports. A new workspace returns 1
until required assertions are recorded. Passing doctor checks means local
inputs are readable and sufficient for that check; it does not establish that
a real role is READY or authorize an application.

Use [PIPELINE_RECOVERY.md](PIPELINE_RECOVERY.md) for bounded source registration,
discovery, posting verification, readiness gates, and Muse integration limits.
Do not edit a parked row's status to READY to manufacture supply. Do not assume
an answer-bank edit automatically resolves a role's current form questions.

## Test a separate synthetic workspace

```bash
KEEL_DEMO_PARENT="$(mktemp -d)"
python3 -S keel.py --home "$KEEL_DEMO_PARENT/demo" demo
```

The demo path must not exist. It uses only synthetic people, postings, answers,
and transport fixtures. It exercises the shipped pipeline with zero external
network requests and gives no submission authority. Keep its outputs separate
from real applicant data.
