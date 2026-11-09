# Contributing — technical ground rules

> Full contributor onboarding lives in the root [CONTRIBUTING.md](../CONTRIBUTING.md).
> This file is the technical checklist.

## What belongs in the public repo

Keel is open-core. Before contributing, read [SPLIT.md](../SPLIT.md):
anything that would help an ATS vendor fingerprint or block automated
applications does not belong here — detection is public, submission behavior
is private.

## Ground rules

1. **Honest automation only.** No feature may invent applicant data,
   fabricate credentials, bypass explicit confirmation, or claim a
   submission without evidence. Fail closed.
2. **No personal data.** Never commit real names, emails, phones,
   addresses, employer records, or application history. Examples use
   `example.com` placeholders. `./package.sh` scans the payload and blocks
   the build on hits.
3. **Append-only telemetry.** Corrections are new events, never rewrites.
   One lead lives in one queue.
4. **Evidence-only counts.** Dashboard numbers come from the ledger and
   explicit confirmation rows — never narrated ahead of evidence.

## Development

Unittest discovery covers only the unittest portion of the tests. It does not
execute function-style pytest tests, including the evidence-based scoring
contracts. A syntax check is not a test-suite pass.

For configured validation, follow the [root development setup](../CONTRIBUTING.md#dev-setup)
and the exact commands and environment settings in the [CI workflow](../.github/workflows/ci.yml)
and [recovery workflow](../.github/workflows/recovery-profile.yml). Use the pinned
validation environment and temporary `KEEL_HOME` and `RUNNER_TEMP` directories,
not a live applicant workspace. Report unavailable suites and skips separately.

For suites in `tools/run_tests.py` that request the separately supplied audit
guard, preserve the missing-guard refusal. The `local_profile` suite does not
request that guard. Direct workflow commands do not establish verified
audit-hook isolation.

Keep modules import-safe (no side effects on import), config-driven via
`KEEL_HOME`, and personal-data-free. New gate vocabulary goes in
`log_event.py`'s centralized set; new ATS patterns go in `ats.py`'s
additive pattern table.
