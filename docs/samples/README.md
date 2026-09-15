# Samples

## Synthetic demo data only

Everything under `docs/samples/` is **invented**. Employers, URLs,
applicant profiles, numbers, questions, answers, and verdicts are
fictional placeholders — no real postings were verified, no real
applications were submitted, and no real personal data appears here.

## What's here

- `launch-packet-sample.md` — a fully synthetic launch packet showing the
  exact structure the pipeline produces in production:
  role header, mocked pre-launch screen, synthetic ATS intel, materials
  placeholders, screening Q&A against a fictional answer bank, a mocked
  prescreen gate log, and the executor contract.

## Packet structure, section by section

| Section | Purpose |
|---|---|
| Header (`role_id`, fit score, posting URL, ATS) | One-line identity of the lead; everything downstream keys off `role_id`. |
| Pre-launch screen | What `prescreen.py` checked before any browser time was spent: live-ness, blocklist, dedupe, and hard gates. The sample's verdict is PARKED to show the fail-closed path. |
| ATS intel | Read-only detection result (platform, question count, markers). Detection is public; submission behavior is not. |
| Materials | Resume / cover letter / brief paths. In the sample these are placeholders — real ones are generated per lead and never committed. |
| Screening Q&A | Required form questions with answers from the answer bank. Q4 is deliberately unmappable to show how the pipeline refuses to invent answers. |
| Prescreen gate log | Tabular gate results — PASS/PARK per gate. |
| Executor contract | The public boundary: what any executor must honor to consume the packet. |

## Why the real pipeline stops at the packet

Per `SPLIT.md`, Keel is open-core. The public half ends at the launch
packet: verified form values, banded rules, hard gates, and the per-field
verification protocol. Everything needed to submit honestly — and nothing
about *how* the commit sequence is executed — is in the packet. The
execution layer (form-event sequencing, commit techniques per ATS,
CAPTCHA handling, credential flows, live submission transport) stays
private because publishing it would teach ATS vendors exactly what to
detect and block.

A PARKED packet like this sample is a normal, healthy outcome: it means
the gates caught something the applicant must resolve in their own words
rather than the system guessing. Parked leads are never silently retried.
