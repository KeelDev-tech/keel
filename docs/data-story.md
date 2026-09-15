<!--
COUNTING METHODOLOGY (2026-09-15 ~02:15 PT) — read before trusting a number.
Sources: ~/workspace/job-pipeline/ledger/application-ledger.json (list of rows),
         ~/workspace/job-pipeline/telemetry/events.jsonl (append-only log).
- "Verified submissions" = rows with status SUBMITTED. Submission is only
  recorded on explicit page-confirmation evidence (pipeline reporting rule);
  nothing below re-verifies the confirmations, it counts the ledger.
- Launch figure 55: counted at 2026-09-15 00:17 PT during the launch push.
  Current total 60: counted 2026-09-15 ~02:15 PT. The 5 new rows carry
  date_submitted timestamps after launch (Figma, Abridge, Complete,
  Parsley Health, Instructure); the remaining 55 predate it.
- Gate figures = DISTINCT (role_id, gate) pairs in gate_blocked events.
  The raw log has 327 gate_blocked events but many leads trip the same gate
  more than once, and 2026-09-14 backfilled events (details.backfilled=true)
  duplicate live ones — dedupe prevents double-counting. 13 queue-level
  gate_blocked events with no role_id (feeder_empty, new_ats_detected, etc.)
  are excluded from lead counts. Log window: 2026-09-13 01:23 PT →
  2026-09-15 ~02:15 PT. gate_encountered (hit but continued) and
  gate_cleared (resolved) are NOT in these counts; noted separately.
- Ledger outcome statuses counted directly: INTERVIEW_INVITED, REJECTED.
-->
# Keel data story: 55 verified applications, zero lies

**Tired of looking for a job? Let us do it for you — and we'll never lie on
your behalf.**

This page is the linkable asset behind Keel's claims: every number below is
counted directly from the private production pipeline's ledger and
append-only telemetry log, aggregated with no identifying detail. Every
figure carries its count time. The public repo makes no submission claims;
it's the tools, the gates, and the telemetry.

*Counted: 2026-09-15 ~02:15 PT. Sources: `application-ledger.json` +
`telemetry/events.jsonl`.*

---

## The headline numbers

| Figure | Value | What it means |
|---|---|---|
| Verified submissions at launch | **55** | Counted 2026-09-15 00:17 PT; applications count only on explicit page-confirmation evidence |
| Verified submissions now | **60** | Five more since launch, all timestamped in the ledger |
| Fabrication gates tripped | **5** | Distinct leads where the system stopped rather than invent an answer (7 raw gate events) |
| Leads stopped by any gate | **198** | Distinct leads × gates blocked, duplicates removed |
| Telemetry events logged | **1,109** | Append-only; every discovery, gate, and outcome recorded |
| Interview invites | **2** | Ledger rows in INTERVIEW_INVITED status |
| Rejections recorded | **4** | Ledger rows in REJECTED status — losses logged with the same discipline as wins |

## The gates: where the pipeline said no

Honest automation is measured in refusals. Distinct leads stopped per gate
(`gate_blocked` events, deduped by lead × gate, log window 2026-09-13 →
2026-09-15):

| Gate | Leads stopped | Meaning |
|---|---|---|
| Needs human input | 83 | Parked for the applicant's own words — never invented |
| Eligibility | 41 | Failed clean-form/security checks — never pursued |
| Discarded by applicant | 18 | Withdrawn on the applicant's instruction — never submitted |
| Materials missing | 13 | No resume lane on disk — never proceeded |
| Travel commitments | 7 | Required travel attestations — never signed by a bot |
| Fabrication | **5** | **The system refused to invent an answer** |
| Technique blocked | 5 | Submission path fingerprintable — never risked |
| Essays | 4 | Your words, your signature — never ghostwritten |
| Malformed queue entries | 4 | Bad data demoted before it could reach a browser |
| Attestations | 3 | Policy statements — never signed by a bot |
| Lever pre-flight refused | 3 | Platform wall confirmed over HTTP — never forced |
| Account creation | 2 | Registration gates — never bypassed |
| Other gates (1 lead each) | 10 | Location, CAPTCHA, verification codes, relocation, duplicate-of-submitted, spam flags — each stopped once |

Also in the log: 82 gates **cleared** after verification (74 pending-verification
gates resolved, not parked forever), and 8 leads that hit the low-fit gate and
parked rather than spray.

## The contract, in one line

**Self host. Reclaim the time.** Your job-search data never leaves your
machine. No $80/month subscription. No account on someone else's server.
And the machine will park a lead, eat the loss, and move on before it ever
lies on your behalf.

## What we don't claim

- The 55-at-launch figure was counted at 00:17 PT on 2026-09-15. Five more
  submissions landed after it — each timestamped in the ledger, none
  backdated.
- Gate figures count distinct leads stopped, with backfilled and repeat
  events deduped — they are not raw event totals.
- No user counts, growth projections, revenue, or funding status.
- Rival-side figures (Sonara tiers, LazyApply ratings) are third-party
  reviews, cited with sources in our press kit — never our measurements.
- Interview invites and rejections are early outcomes, not a conversion
  claim. The sample is small and we say so.

---

*Keel is the free, open-source (Apache-2.0) job-application autopilot that
refuses to lie: https://github.com/KeelDev-tech/keel*
