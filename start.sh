#!/usr/bin/env bash
# Inspect local state only; no initialization, HTTP reads, or queue promotions.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$#" -gt 1 ]; then
  echo "usage: $0 [WORKSPACE] (or set KEEL_HOME)" >&2
  exit 2
fi
KEEL_WORKSPACE="${1:-${KEEL_HOME:-$PWD}}"
KEEL_PYTHON="${KEEL_PYTHON:-python3}"

# Missing applicant assertions are an expected first-run result. Still print
# the disjoint supply report, while returning the doctor's ACTION_REQUIRED code.
if "$KEEL_PYTHON" keel.py --home "$KEEL_WORKSPACE" doctor --capabilities; then
  KEEL_DOCTOR_STATUS=0
else
  KEEL_DOCTOR_STATUS=$?
fi
if [ "$KEEL_DOCTOR_STATUS" -gt 1 ]; then
  exit "$KEEL_DOCTOR_STATUS"
fi
"$KEEL_PYTHON" keel.py --home "$KEEL_WORKSPACE" supply
"$KEEL_PYTHON" keel.py --home "$KEEL_WORKSPACE" pipeline-doctor
exit "$KEEL_DOCTOR_STATUS"
