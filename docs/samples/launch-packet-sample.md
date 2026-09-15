# Launch Packet (SAMPLE — synthetic demo data) — Meridian & Vale Coffee Roasters | Operations Specialist | Remote

> **DEMO DATA ONLY.** Every employer, URL, number, question, and answer in
> this file is invented for demonstration. No real application was
> submitted, no real posting was verified, and nothing here succeeded.
> See `docs/samples/README.md`.

role_id: MVCR-OPS-SPECIALIST-REMOTE-SAMPLE | fit 68
Posting URL: https://example.com/jobs/ops-specialist-mvcr-0000 (fictional)
ATS: greenhouse-legacy embed (synthetic detection result)

## Pre-launch screen (mocked)

- Posting LIVE as of 2026-09-15 09:00 PDT (simulated). Remote, US time zones.
- Blocklist: clean (fictional employer; blocklist check shown for form's sake).
- Dedupe: no prior application on record.
- Prescreen verdict: **PARKED — needs_input (fail-closed)**. See Screening
  Q&A below: required question Q4 has no mapping in the sample answer bank,
  so the packet stops here. The launch packet is produced so the gate is
  visible; the executor MUST NOT proceed until the applicant supplies the
  missing answer or the lead is dropped.

## Synthetic ATS intel (mocked)

- Platform: Greenhouse legacy embedded board (`boards.greenhouse.io`-style).
- Custom questions detected over HTTP: 4 (see Screening Q&A). No Enterprise
  board markers observed; no CAPTCHA marker observed in the mocked probe.
- Note: detection is read-only identification. Submission behavior is the
  private execution layer's concern (see SPLIT.md).

## Materials (placeholders — not real files)

- Resume: resumes/Demo_Applicant_SweepX_Resume_v1.pdf
- Cover letter: cover-letters/Demo_Applicant_CoverLetter_v1.pdf
- Brief: engines/application-executor/briefs/MVCR-OPS-SPECIALIST-REMOTE-SAMPLE.brief.txt
- Custom answers: see Screening Q&A below

## Screening Q&A (synthetic)

Drawn from a **fictional sample answer bank**. Fictional applicant profile
(used throughout this repo's samples): a hypothetical operator with 7
years running a small consultancy, 5 years in the AI industry, 12 years
total professional experience. All figures below are demo values.

**Q1. "How many years of experience do you have in operations or program
operations?"**
- Field type: required dropdown (0–1 / 1–3 / 3–5 / 5–7 / 7+)
- Sample answer: `5–7` (honest per the fictional profile's 7-year band)
- Verdict: MAPPED ✓

**Q2. "Are you authorized to work in the United States?"**
- Field type: required yes/no
- Sample answer: `Yes`
- Verdict: MAPPED ✓

**Q3. "This role is remote with quarterly on-site summits in Denver, CO.
Can you travel up to 10% of the time?"**
- Field type: required yes/no
- Sample answer: `Yes`
- Verdict: MAPPED ✓ (travel commitment verified against the fictional
  profile's standing travel tolerance — in production, an unstated travel
  requirement would park the lead instead)

**Q4. "Describe your experience scaling a multi-location retail operations
program from scratch, with specific headcount numbers."**
- Field type: required free text
- Sample answer: (none — no truthful mapping exists in the sample answer
  bank for the fictional profile)
- Verdict: **UNMAPPABLE → PARK (fail-closed)**. The pipeline does not
  invent experience, fabricate numbers, or downgrade a required question
  to optional. The lead waits on the applicant's own words or is dropped.

## Prescreen gate log (mocked)

| Gate | Check | Result |
|---|---|---|
| location | JD contains no regular on-site requirement | PASS |
| degree | No degree required | PASS |
| tenure | 3+ yrs required; fictional profile supports 7-yr band in ops | PASS |
| travel | Quarterly summits disclosed; profile tolerates | PASS |
| attestation | No "unaided-work" / no-AI attestation required | PASS |
| required-question mapping | Q4 has no truthful mapping | **PARK** |
| scam/privacy | Fictional employer; no SSN/bank asks in mocked form | PASS |

## Executor contract

The real Keel pipeline stops at this packet (per SPLIT.md). A private
execution layer (or the applicant's own implementation) may take the packet
only if **all** of the following hold:

1. Prescreen verdict is CLEAN — this sample's verdict is **PARKED**, so it
   would be returned to the applicant for Q4, not submitted.
2. Every required field verifies against the packet's answers using the
   per-field verification protocol (verify after each field, blur test,
   dropdown re-open check).
3. Any live-field mismatch with the packet, any new required question, or
   any attestation the applicant has not approved → STOP and report
   needs-input. Never invent.
4. Report the EXACT confirmation text on success; log the outcome to the
   append-only telemetry log.

## What this sample demonstrates

- The packet is a **data handoff**, not a submission script: verified
  values, gates, and a verification protocol, with zero execution
  internals (those are the private half — SPLIT.md).
- Fail-closed behavior is a first-class outcome: Q4 parks the lead rather
  than risking a dishonest answer. A parked lead is never silently
  retried; it waits on the applicant or is dropped.
