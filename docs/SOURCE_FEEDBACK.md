# Measured source feedback

`engines/source_feedback.py` proposes a bounded allocation of **recorded human minutes** across discovery sources. It reads local observations, respects source caps and cooldowns, and returns JSON for human review. It does not run discovery, change schedules or queues, publish messages, make network calls, or incur service charges.

```bash
python engines/source_yield_proposals.py \
  --feedback-input source-feedback.json --budget-minutes 30
```

Use `--now 2026-09-24T12:00:00Z` for deterministic replay at a declared analysis time. Feedback mode is separate from the older telemetry heuristic; it does not read the legacy roster or telemetry. `--publish` and `--check-open` cannot be combined with feedback mode. Exit code 0 means an allocation proposal exists; 2 means HOLD or invalid/unavailable input. An allocation is never execution permission.

## Input contract

The top-level schema is `keel.source_feedback.v1`. Unknown fields are rejected. The input is limited to 8 MiB, 128 sources, 10,000 observations, and 10,000 applications. Budgets and individual source caps are whole minutes from 0 to 240.

| Field | Meaning |
| --- | --- |
| `observed_at` | Time the measurement export was checked complete; timezone required. Older than 24 hours holds every source. |
| `window_start`, `window_end` | Common observation window, inclusive start and exclusive end, ending no later than `observed_at`. |
| `telemetry_complete` | Explicit Boolean. False holds every source. Missing time or rows are never treated as zero. |
| `objective` | `qualified_leads` or `interviews`. No predicted hiring outcomes are manufactured. |
| `horizon_days` | Outcome follow-up horizon, integer 1–180. Used only for the interview objective. |
| `sources` | Explicit source IDs, permission/completeness flags, caps, cooldown state, and evidence references. |
| `observations` | Attributed opportunities, observation timestamps, qualification observations, recorded human minutes, and optional exact application IDs. |
| `applications` | Empty for qualified-lead ranking. For interviews, the existing `keel_flow.outcomes.cohorts` input records. |

Each source requires `source_id`, `permitted`, `measurement_complete`, `cap_minutes`, `cooldown_until`, `cooldown_verified`, and `evidence_ref`. Flags can be true, false, or null; only explicit true clears the corresponding gate. A null `cooldown_until` means no known active cooldown **only when** `cooldown_verified` is true. An active cooldown receives zero minutes. Unknown permission, cooldown, completeness, or effort produces a visible HOLD.

Each observation requires `observation_id`, `source_id`, `opportunity_id`, `observed_at`, `qualified`, `human_minutes`, `application_id`, and `evidence_ref`.

- Source and opportunity IDs must come from the collector's explicit records. Filenames, role prefixes, company names, and logger names are not source attribution.
- `qualified` records an observed qualification decision under the current fit/liveness policy; it does not derive one. Null holds that source.
- `human_minutes` is measured operator effort for this observation. Zero is permitted, but a source with no positive measured effort is held. Null holds that source. Wall-clock run time, model latency, and an absent timer are not human effort.
- Replayed identical observation IDs are counted once. Changed payloads for an existing ID fail. Renaming an otherwise identical source/opportunity/time/evidence occurrence also counts once; conflicting measurements for that occurrence fail.
- Rechecking an opportunity consumes additional effort when represented by a distinct measured occurrence, but yields only one unique opportunity. Its latest observation supplies current qualification. Conflicting quality at the same time holds the source.
- An opportunity assigned to multiple sources holds all those sources until its ownership is reconciled. There is no guessed first-touch attribution.

The caller must establish observation truth, measured effort, and provenance. A syntactically valid evidence reference is not authentication.

## Allocation and outcome handling

Eligible sources are ordered by `Wilson(successes, trials).lower * trials / recorded_human_minutes`, with source ID breaking ties. Minutes are assigned in that order up to each source cap and the total budget. No diminishing-return curve is assumed. Zero observed successes never earns budget, including when floating-point rounding yields a tiny positive Wilson endpoint. Nonfinite derived rates are rejected. Unused budget remains explicitly unallocated.

For `qualified_leads`, trials are unique attributed opportunities and successes are their currently observed qualifications. For `interviews`, trials and successes come from confirmed, mature, completely observed application cohorts. Every supplied application must bind to one attributed opportunity and the same source. Submission dates must fall within the common window. Qualified opportunities lacking application attribution, missing application outcomes, reused receipts, unconfirmed submissions, immature cohorts, or incomplete follow-up hold the affected source. They are not silently recorded as failures. Sources with no mature cohort stay on HOLD.

For the interview objective, each application uses the existing reducer fields:

```json
{
  "application_id": "application-1",
  "source_id": "source-a",
  "fit_score": 85,
  "submission_confirmed_by_adapter": true,
  "submission_ref": "receipt:application-1",
  "adapter_revision": "reviewed-adapter-v1",
  "submitted_at": "2026-09-03T12:00:00Z",
  "followup_observed_through": "2026-09-24T12:00:00Z",
  "interview_at": "2026-09-06T12:00:00Z",
  "interview_evidence_ref": "local:reviewed-interview-record-1",
  "offer_at": null,
  "rejection_at": null
}
```

Set the observation window to contain the submission, and set `observed_at` to the actual recent export check. Any non-null interview, offer, or rejection timestamp requires its corresponding evidence reference. Adapter confirmation remains a caller assertion in this reducer; do not set it from a local submission claim or a quoted confirmation string alone.

The result includes per-source holds, cost, yield, source caps, proposed minutes, total allocated/unallocated minutes, an input digest, and the outcome reducer's exclusions. It always reports `execution_authorized: false`, `human_review_required: true`, `schedule_writes: 0`, `causal_improvement_established: false`, and `off_policy_estimate: false`. The Wilson formula is a descriptive ranking rule here. Independence, randomized assignment, authenticated propensities, causal superiority, or future performance are not established.

## Runnable synthetic example

Save this example as `source-feedback.json`, then run the CLI with `--budget-minutes 10 --now 2026-09-24T12:00:00Z`. Its two synthetic observations yield a five-minute proposal due to the source cap, leaving five minutes unallocated. Replace every example observation with measured records before making operational decisions.

```json
{
  "schema": "keel.source_feedback.v1",
  "observed_at": "2026-09-24T12:00:00Z",
  "window_start": "2026-09-23T12:00:00Z",
  "window_end": "2026-09-24T12:00:00Z",
  "telemetry_complete": true,
  "objective": "qualified_leads",
  "horizon_days": 14,
  "sources": [
    {
      "source_id": "demo-source-a",
      "permitted": true,
      "measurement_complete": true,
      "cap_minutes": 5,
      "cooldown_until": null,
      "cooldown_verified": true,
      "evidence_ref": "demo:source-policy-a"
    }
  ],
  "observations": [
    {
      "observation_id": "demo-observation-1",
      "source_id": "demo-source-a",
      "opportunity_id": "demo-opportunity-1",
      "observed_at": "2026-09-24T10:00:00Z",
      "qualified": true,
      "human_minutes": 2,
      "application_id": null,
      "evidence_ref": "demo:observed-decision-1"
    },
    {
      "observation_id": "demo-observation-2",
      "source_id": "demo-source-a",
      "opportunity_id": "demo-opportunity-2",
      "observed_at": "2026-09-24T10:05:00Z",
      "qualified": false,
      "human_minutes": 1,
      "application_id": null,
      "evidence_ref": "demo:observed-decision-2"
    }
  ],
  "applications": []
}
```

## Migrating legacy telemetry honestly

The current `staging_ingest.py` producer emits the logger identity `source="staging-ingest"`, `batch`, and `staging_file`; it does not emit a canonical discovery `source_id` or measured human effort. Some discovery entries carry platform/slug/job IDs. Those are useful evidence for a reviewed mapping but cannot establish source ownership or effort automatically. Existing historical telemetry lacking attribution is therefore HOLD, not retroactively relabeled.

For future records, the collector must retain a reviewed source ID when the opportunity is discovered, carry it unchanged through staging and the exact application identity, and measure human effort separately. A future attributed staging event should include this explicit field:

```json
{
  "event_id": "observed-event-1",
  "ts": "2026-09-24T10:00:00Z",
  "event_type": "staged_ingested",
  "role_id": "role-1",
  "source": "staging-ingest",
  "details": {
    "source_id": "reviewed-source-a",
    "batch": "batch-2026-09-24-1000",
    "fit_score": 85
  }
}
```

The source ID may instead appear at the event's top level; if both locations appear they must agree. Do not replace the logger's `source` with the discovery source. Mixed-source aggregate rejections require per-candidate source attribution upstream; the legacy parser must not assign the whole batch from a filename.

A safe manual migration is to review source ownership for each opportunity, record actual qualification and human effort with evidence references in the feedback input contract above, set completeness only for a fully checked window, run the proposal CLI, and review its holds and allocation. Unknown historical time remains null. Neither the legacy parser nor the new allocator backfills or writes any source records.

Legacy repairs also distinguish explicit duplicate reasons from other rejected candidates; validate aggregate counts; deduplicate event IDs and candidate checks; reject corrupt, missing, naive-time, and future telemetry; prevent roster sections from borrowing another section's cadence; parse BIWEEKLY as 14 days; require complete fit coverage for promotion; and require an observed gap below known cadence for a re-sweep claim. An unattributed submission observation protects every source against demotion until reconciled. Empty measured windows remain HOLD.
