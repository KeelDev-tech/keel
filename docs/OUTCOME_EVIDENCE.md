# Outcome evidence and receipt intake

Keel now separates **observed mail**, **manual attestation**, and **provider-verified acceptance**. These are different evidence grades. A local import, message header, hash, quoted confirmation, or caller `verified` flag never authenticates a provider.

## Offline intake (no new service)

The local listener reads `.eml` files in the configured `maildir`, or `data/mail`. A dry run creates a review report:

```sh
KEEL_HOME="$PWD" python3 engines/inbox_listener.py --lookback-days 30 --out data/outcome-review
```

Maildir accepts regular `.eml` files directly inside its directory, up to 2 MiB each; symbolic links and path traversal are refused. Receipt times must include a timezone and fall within the requested lookback. Malformed source files stop the scan visibly rather than silently implying complete coverage. No mail is sent. LinkedIn/Indeed notifications remain observations of notification mail, not access to private inbox content.

To link a message, an adapter supplies structured `application_id`, `role_id`, `attempt_id`, `receipt_ref`, `posting_url`, or `ats_job_id`. All supplied identities must agree. The Maildir adapter accepts `X-Keel-Application-ID`, `X-Keel-Role-ID`, `X-Keel-Attempt-ID`, and `X-Keel-Receipt-Ref` annotations on a **local working copy**. Those headers are untrusted correlation hints, never provider authentication. Preserve the original message separately if provenance of the original bytes matters. Its imported content hash describes the actual imported bytes, including annotations.

A company name alone cannot select a role. Missing, conflicting, ambiguous, future, or unmatched identifiers remain visible in the review report and are retried after correction. Scores, acknowledgment coverage and lane counters come from the unique application row. They never credit every application at a company.

To record reviewed observations in local telemetry:

```sh
KEEL_HOME="$PWD" python3 engines/inbox_listener.py --live --lookback-days 30 --out data/outcome-review
```

Here `--live` means **write local observation records**. It does not submit applications, send mail, update queue status, or certify acceptance. Receipt records are saved in `data/outcome-review/receipt-observations.json`. Telemetry uses a stable event ID; a retry after a crash between event append and checkpoint does not create a second event. Failed writes are raised and do not mark the message consumed. Conflicting reuses of a receipt ID are persistently held; changing the ID is not a resolution procedure.

Manual observations use the same intake. Save a JSON object following this synthetic shape, substituting the actual evidence and times:

```json
{
  "kind": "manual_attestation",
  "source": "operator-review",
  "receipt_id": "review-session-unique-reference",
  "role_id": "exact-role-id",
  "attempt_id": "exact-attempt-id",
  "application_id": "exact-application-id",
  "received_at": "2026-09-23T23:00:00+00:00",
  "recorded_at": "2026-09-24T01:00:00+00:00",
  "content_sha256": "replace-with-the-64-character-sha256-of-your-evidence",
  "outcome": "AUTO_ACK"
}
```

`application_id` can be empty only when the canonical claim also has none. `recorded_at` cannot precede `received_at` or be in the future. Valid outcomes are `AUTO_ACK`, `REJECTION`, `INTERVIEW_INVITE`, `ASSESSMENT`, `INFO_REQUEST`, `OFFER`, and `OTHER`.

```sh
python3 engines/outcome_tracking/receipt_intake.py --store data/outcome-review/receipt-observations.json --input local-receipt.json
python3 engines/outcome_tracking/evidence_gate.py --ledger data/application-ledger.json --events data/telemetry/events.jsonl --receipts data/outcome-review/receipt-observations.json
```

The evidence gate publishes `observed_receipts`, `manual_attested`, `receipt_claims_held`, and `receipts_complete` separately from `verified`. These counts are claim counts, not message counts. Manual/observed evidence is useful for review but cannot automatically promote canonical state. The command-line gate remains non-passing for certification: it has no provider credentials or implicit trust switch.

## Trusted host integration

`coverage_report(..., receipts_path=..., provider_validators={source: validator})` accepts **explicitly injected trusted host code**. A validator receives a copy of the normalized receipt and the complete canonical claim. It must independently authenticate the actual provider evidence, correct account/recipient, acceptance result, and exact application/attempt binding. It returns a `ProviderValidation` containing:

- SHA-256 digests of both complete normalized inputs, using `safe_io.digest`;
- a provider identifier and timezone-aware `checked_at` no earlier than receipt time and no later than the report time;
- `accepted=True` only after those checks succeed.

The intake accepts `kind="provider_receipt"` as a provenance claim, but that kind alone grants no trust. A configured host validator is called only for an exactly matched `AUTO_ACK` receipt with an explicit attempt ID. Imported mail and manual attestations never invoke it. Returned dictionaries, stale bindings, future validations, mismatches, exceptions and conflicts cannot increase verified coverage. Acceptance is not cached in the local JSON store: the host check runs again on each report. Validators must define their own freshness/revocation checks for the provider's evidence.

The repository supplies the interface and synthetic tests, **not a live authenticated provider connector**. Validating real provider acceptance remains a host integration task. Local receipt files are atomic and locked but are not an adversarial tamper-proof ledger; the OS account can edit them. Hashes bind content, not authenticity.

## Analytics and remaining boundaries

Outcome analytics version `outcome-analytics/2` compares all supplied identities, holds cross-role/cross-attempt conflicts and duplicate candidates, and preserves URL path case. Loading malformed/conflicting telemetry fails rather than silently discarding bad rows. Legacy company-only single-candidate analytics remain explicitly labeled tier 4 inference; the new inbox intake disables that fallback. Existing historical events are not rewritten, and existing imported observations must not be presented as provider-verified conversions.

Mature outcome completeness, ongoing inbox observation windows, effort accounting, and causal source-selection propensities are not inferred from the absence of replies. A missing response can mean an unobserved channel or incomplete collection. Scheduling decisions should require explicit completeness/effort records from the host and keep unobserved outcomes unknown.

Run the offline regression suite with `python3 -m pytest -q tests/test_receipt_intake_evolution.py`. It covers future/corrupt receipts, untrusted flags, validator binding, contradictory IDs, duplicate receipt holds, mail path containment, company ambiguity, and recovery after append/checkpoint failure. All fixtures are synthetic.

### Replay and projection consistency

The receipt store includes an immutable first-event outbox. It freezes telemetry metadata before append, so a later fit-score/company-label change cannot alter the payload associated with the same event ID. Receipt conflicts are checked across the complete batch **before** acknowledgment and lane aggregation; all conflicting observations are held, including earlier counterparts in the same batch.

Raw observed IDs remain separate from ledger-resolved IDs. For example, a message containing only a unique role ID can correlate to a ledger row for review, but its receipt retains an empty observed attempt ID and a separately labeled `resolved_identity`. That inferred attempt cannot satisfy the evidence gate's requirement for an observed, exact attempt binding.

New intake telemetry carries a `receipt_key`. Analytics rechecks the receipt store when loading those events; a later conflict retracts the event from current analytical credit without rewriting historical telemetry. Missing receipt state also holds the new event. The default store is under `hidden_files/outcome-tracking`; when intake uses a custom output directory, pass its store explicitly:

```sh
KEEL_HOME="$PWD" python3 engines/outcome_analytics.py --receipts data/outcome-review/receipt-observations.json --out data/outcome-review
```

Historical reports remain immutable snapshots; regenerate reports to project current conflict status. Legacy telemetry without receipt keys retains its explicitly unverified/inferred status.
