# SPLIT.md — Open-core boundary

Keel is **open-core**: this repository is the public half. A private
execution layer exists alongside it and is deliberately not published.

## Why the split exists

The private half contains submission-behavior methods that ATS (applicant
tracking system) vendors could fingerprint: form-event sequencing, commit
techniques per platform, CAPTCHA-handling specifics, credential and
verification-code flows. Publishing them would teach vendors exactly what to
detect and block — degrading the pipeline for everyone who runs it. The open
half is everything that does not carry that risk.

## Public (this repo)

- Discovery and scoring: search playbooks, sweep prompts, the 100-point
  fit model, the generic scorer template.
- Generic resume/material tailoring: templates driven by YOUR applicant
  profile, with truthfulness gates.
- Sanitized answer bank: canonical answers + banded-question rules + hard
  gates (example file; yours is personal data, never committed).
- Telemetry/event logging: append-only, additive-only event logging.
- Outcome analytics: ledger + telemetry analysis with fail-closed reporting.
- Generic dashboard: self-contained HTML from your ledger and queues.
- ATS detection / capability radar: identification of platforms and
  HTTP-level probes of what submission paths are viable. Detection only —
  no submission behavior.
- Documentation and personalization: setup wizard, config, guides.

## Private (not in this repo)

- The technique library: per-ATS form-commit/event-sequencing methods.
- Brief-builder internals that inject those techniques into launch briefs.
- CAPTCHA-handling specifics.
- Credential and verification-code flows.
- The API-direct submission transport (live HTTP submission code).
- Any submission behavior an ATS vendor could fingerprint.

The public `apply_loop.py` stops at the **launch packet** and documents an
EXECUTOR CONTRACT that the private layer (or your own implementation)
fulfills. The packet contains verified form values, banded rules, hard
gates, and the per-field verification protocol — everything needed to
submit honestly, nothing about how the commit sequence is executed.

## What is inseparable

The generic brief's per-field verification protocol (verify after every
field, blur test, dropdown re-open check) is public because it is a
correctness practice, not a fingerprintable technique. The specific
DOM-event sequences that make a given ATS accept a field are private.

The ATS *detection* rules (URL patterns, board APIs) are public because
they are read-only identification. The *submission* paths discovered by
those probes are exercised only in the private layer.

## Rule of thumb

If a method's publication would help a vendor block automated
applications, it is private. If it helps an applicant run an honest,
verifiable, fail-closed pipeline, it is public.
