# Start this Keel source candidate

Use Python 3.11+ on Linux or WSL. The local core uses only the Python standard
library. Bash is required for `setup.sh` and `start.sh`. Browser and materials
tools have separate optional dependencies.

From this extracted source directory:

```bash
export KEEL_HOME="$HOME/keel-workspace"
./setup.sh "$KEEL_HOME"
./start.sh "$KEEL_HOME"
```

A fresh `start.sh` returns 1 because applicant assertions are unknown. Record
your own required values, then inspect again. Omit `--value` to read from stdin:

```bash
python3 keel.py --home "$KEEL_HOME" confirm-answer --key first_name --source "applicant assertion"
python3 keel.py --home "$KEEL_HOME" confirm-answer --key last_name --source "applicant assertion"
python3 keel.py --home "$KEEL_HOME" confirm-answer --key email --source "applicant assertion"
python3 keel.py --home "$KEEL_HOME" doctor --capabilities
python3 keel.py --home "$KEEL_HOME" pipeline-doctor
```

Fill the workspace's applicant profile and policy with your own facts and
commitments. Supply real materials in `data/resumes/`. Passing local checks does
not prove posting presence, rendered form completeness, READY, or authorization
to submit. Verification never promotes READY by itself.

For a separate offline synthetic walkthrough:

```bash
KEEL_DEMO_PARENT="$(mktemp -d)"
python3 -S keel.py --home "$KEEL_DEMO_PARENT/demo" demo
```

The demo path must not exist. It makes zero external network requests and
creates only synthetic evidence and an unauthorized review packet.

Read [docs/PIPELINE_RECOVERY.md](docs/PIPELINE_RECOVERY.md) before inspecting or
integrating a live workspace. Read [docs/RELEASE_PROFILE.md](docs/RELEASE_PROFILE.md)
for capability limits and [the current validation report](docs/VERIFICATION_READY_VALIDATION.md)
for measured results and retained failures. This candidate includes source and per-file hashes;
it contains no live Muse state, credential environment, or production deployment.

The candidate includes pinned free development tools and the eleven regression
modules selected by `.github/workflows/recovery-profile.yml`, with their source
dependencies. The recovery guide gives the exact local test command. CI is
configured for Python 3.11 and 3.12; configuration alone is not a passing result.
