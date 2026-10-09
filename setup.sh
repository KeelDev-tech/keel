#!/usr/bin/env bash
# Initialize a local workspace with the same fail-closed templates as the CLI.
set -euo pipefail
KEEL_CALLER_DIR="$PWD"
cd "$(dirname "$0")"
if [ "$#" -gt 1 ]; then
  echo "usage: $0 [WORKSPACE] (or set KEEL_HOME)" >&2
  exit 2
fi
KEEL_WORKSPACE="${1:-${KEEL_HOME:-$PWD}}"
case "$KEEL_WORKSPACE" in
  /*) ;;
  *) KEEL_WORKSPACE="$KEEL_CALLER_DIR/$KEEL_WORKSPACE" ;;
esac
KEEL_PYTHON="${KEEL_PYTHON:-python3}"

"$KEEL_PYTHON" keel.py --home "$KEEL_WORKSPACE" init

cat <<'INSTRUCTIONS'

Initialization preserves existing workspace files. New applicant answers and
personal policy commitments remain unknown; no consent is pre-authorized.

Record your own answers with provenance (omit --value to read from stdin):
  python3 keel.py --home /path/to/workspace confirm-answer --key first_name --source "applicant assertion"
  python3 keel.py --home /path/to/workspace confirm-answer --key last_name --source "applicant assertion"
  python3 keel.py --home /path/to/workspace confirm-answer --key email --source "applicant assertion"

Fill data/applicant_profile.json and data/policy.json with your own facts and
boundaries. Copy your actual resume into the workspace's data/resumes/.
Inspect with ./start.sh /path/to/workspace. Read docs/PIPELINE_RECOVERY.md.
For an isolated offline walkthrough use a new, nonexistent directory:
  python3 keel.py --home /path/to/new-synthetic-demo demo
INSTRUCTIONS
