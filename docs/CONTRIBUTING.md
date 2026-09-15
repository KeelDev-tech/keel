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

```bash
python3 -m unittest discover -s tests   # full suite
python3 -m py_compile engines/*.py      # syntax check
```

Keep modules import-safe (no side effects on import), config-driven via
`KEEL_HOME`, and personal-data-free. New gate vocabulary goes in
`log_event.py`'s centralized set; new ATS patterns go in `ats.py`'s
additive pattern table.
