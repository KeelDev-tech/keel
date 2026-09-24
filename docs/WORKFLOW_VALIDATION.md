# Keel 0.6 local validation

Version: 0.6.0-review.1. Evidence captured September 18, 2026.
Status: local regression and synthetic workflow PASS; live integration pending.

| Check | Observed result |
|---|---|
| Main, flow, assurance, trust and workflow suite | 893 passed, 0 failed/errors/skipped |
| Preserved maintenance suite | 131 passed, 0 failed/errors/skipped |
| Total | 1,024 passed |
| New workflow tests within the main suite | 140 passed |
| Actual local workflow exercise | PASS; 6 expected checks passed |
| SQLite snapshot/restore | 6 of 6 objects matched; integrity and reopened API behavior passed |
| Healthy empty restore | NOT_READY; all six required objects missing |
| External effects | 0 model calls, HTTP requests, browser actions, messages or submissions |

The full suites ran through their existing separate guards. Review concurrency,
premature disclosure, immutable commits, stale/changed context, missing evidence,
attachment edits, uncertain attempts, endpoint/account changes, idempotency,
receipt tampering, private file modes and restore failures are tested.
The CLI lifecycle also exercises all three simulator outcomes and exit codes.

The synthetic end-to-end exercise runs real local code, SQLite transactions,
backup/restore and rejection checks. Its reviewer verdicts and human approval
are explicitly test fixtures, not observations from models or people.

Source inventory digest for the tested rehearsal:
`6599c9638b7eb3e4940617d122ee27174bfabaf4c27466a4894ea257087429da`

Exact counts/logs are in `audit/workflow-validation.json`,
`audit/workflow-main-junit.xml`, `audit/workflow-main-pytest.log`, and the separate
maintenance files. Rehearsal measurements and their evidence digests are in
`audit/workflow-rehearsal.json` and `audit/workflow-demo/`. The release builder
rejects a changed source inventory after rehearsal. The TXT extractor verifies
every source file before writing a new isolated directory.

An independent code-review pass identified six issues that were corrected and
rechecked: candidate attachment-byte validation, timestamp compatibility,
durable private database paths, cross-destination retry bypass, incomplete
simulation exit status, and private CLI output permissions. See
`audit/workflow-review.md` for scope. This is not third-party certification.

No live repository, authenticated reviewer adapter, canonical production event
bridge, provider receipt verifier or real submission adapter was available.
No production deployment, live application, independent reasoning improvement,
throughput gain or state-of-the-art benchmark is claimed. The 90-second export
freshness limit is preserved and can constrain eventual model review latency.
