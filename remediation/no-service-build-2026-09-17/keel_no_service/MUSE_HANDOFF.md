# MUSE implementation handoff

Trent authorized fixing the limitations without paying for another service.
This package is executable code plus five incremental patches. Continue the
implementation against the existing authoritative Keel repository. Do not ask
Trent to purchase a service to run this package.

## Establish the source before modifying it

1. Read the live `AGENTS.md`, current limitations register, and conflict review.
2. Work in an isolated branch/copy. Preserve newer RP01, PQ-0A, PQ-9, the live F18
   gate, live launch/intent holds, and existing standing directives.
3. Compare `patches/manifest.json` against the current source. It identifies the
   recovered **review snapshot**, not the current live revision.
4. Do not apply either old cumulative transfer patch. Do not restore the rejected
   gateway/kill switch, preparation-only brief, stale verify_retry implementation,
   or separate-SQLite receipt writer. This delivery changes none of those files.

If the five base hashes match, stage an exact isolated copy:

```bash
python3 stage_patches.py --source-executor /absolute/path/to/application-executor --destination /absolute/path/to/new-executor-stage
python3 -S run_tests.py --executor /absolute/path/to/new-executor-stage
```

The source directory is untouched. The destination must be new. When any hash
differs, inspect and port only the behavior described below; do not force an
overwrite. Run the package's engine tests against that isolated integration.

## Integrate in this order

**1. Parking and queue primitives.** Port `parking_schema.py.patch` and
`queue_io.py.patch`. Accept a nonempty list of nonempty note strings, reject
empty/mixed-type notes, preserve `entries`/`items`/`leads` wrappers and metadata,
support `lock_path=` without changing the global lock path, and fsync file plus
directory. `park_entry` updates an existing entry and fails on a missing entry;
it is not an atomic cross-queue migration. Preserve the live queue's canonical
parking status mapping. The patch adds no canonical-counting charter amendment.

**2. Preflight and launch-lock lifecycle.** Port `launch_lock.py.patch`,
`api_fastlane.py.patch`, and the pre-dispatch changes in `api_direct_loop.py.patch`
together. No-packet and browser-route decisions happen before launch acquisition.
Preflight passes `stage=False`; staging occurs only after acquiring an invocation
token. Grant/staging/telemetry failure before dispatch releases that exact token.
Once submission is invoked, timeout/429/error/refusal never triggers release or
transport fallback. UNKNOWN holds in the live attempt store survive lease expiry.

Launch-lock mutation is serialized with a persistent `.mutex` file; all writers
must use the same implementation. Do not remove mutex files, apply mixed lock
versions, clear old locks in bulk, or assume a local lease proves retry safety.
The existing TTL policy is not a substitute for canonical attempt reconciliation.
No submission writer or provider contract is replaced by these patches.

The early return paths preserve per-board statistics. Full dry-run skips lock,
approval staging, handoff writes, telemetry writes and HTTP-meter writes. Existing
preflight can still perform read-only provider requests; rate limiting still applies.

**3. Consume durable preparation requests.** `request_preparation` stamps the
current queue entry with `preparation_request.kind=packet_build|browser_handoff`.
Wire the existing parent controller to consume this field idempotently under its
current canonical claim. Before building/spawning, re-read current queue state,
dependency revisions and application holds. Clear a request only after its
handoff/build is accepted; use timed reconciliation after a missed wake-up.
Do not mark a lead submitted because a request was accepted. This consumer is a
live-repository integration task; the review export has no reliable current hook.

**4. Add local modules as observers first.** Copy `keel_local/` into the current
source layout or add its package root to that application's import path.
Map current engine records into version 1 observations. Supply authoritative
canonical application IDs, approval-verifier output, dependency revisions,
current attempt/hold state, route evidence and timestamps. Missing data stays
unknown. The evaluator must not be fed caller-invented `approval_valid=true`.
Use the same evaluator for preparation, dashboard and pre-claim checks only
after adapter contract tests pass. Recheck at the actual canonical claim boundary.

`application_identity()` uses candidate/provider/employer/posting IDs. Reviewed
cross-provider mirror aliases must come from the live store. Matching job titles
does not establish identity. Reconcile historical attempts before enabling a new
route. Do not retroactively label legacy rows verified.

**5. Resolve answers without dropping scope.** Wire every brief/field consumer
through `resolve_answer` and `brief_answer`. Build question hashes from exact
prompt, field type and ordered choices. Source/verification/authorization refs
must come from the trusted bank/issuer; this module does not authenticate strings.
Populate authorized attestation keys from the live standing authorization record.
Never infer experience from employer names. SMS/WhatsApp consent stays scoped.
No-AI/unaided-work declarations remain user decisions. No true answer is invented.

**6. Bind outbound bytes and accounts.** Use `freeze_asset` at review time and
`approved_bytes` immediately before uploading. Approvals must bind the returned
bundle hash, form/policy revisions, candidate/target and assets. Bind publishing
to connector ID, immutable account ID and configuration revision; verify those
again at dispatch. Rights and inspection references are mandatory but must be
validated by the real issuer. Hashing alone is not approval authentication.

**7. Wire the durable preparation outbox and diagnostics.** `Journal` is a new,
optional **diagnostic/preparation sidecar**, never a second application authority.
Do not point it at the live workflow DB. It atomically accepts an event and its
work request, deduplicates event IDs, fences leases, backs off retries and exposes
dead letters. Browser handoff events are idempotent proposals; the consumer still
needs the canonical browser intent before any submit click. Parent orchestration
should emit browser lifecycle and actual HTTP/tap meters, including failures.
An outbox in this separate diagnostic store cannot make live queue/ledger writes
atomic. A-B6/F27 stay open until the actual shared state boundary is implemented.

**8. Supply and health.** Configure the user's existing public board tokens and
run bounded discovery using the existing scheduler. New postings are STAGED,
deduplicated before verification and inspected as posting text, not only forms.
Regex signals request policy review; a missed signal never proves eligibility.
Honor provider 429s and retry guidance; do not buy proxies or bypass access controls.
Run the fetcher under an existing process deadline (its OS DNS lookup is not
bounded by the socket timeout). Feed measured non-starved capacity and P95 prep
time to `refill_plan`, subtract WIP, and surface infeasible yield/budget gaps.
Read scheduler exports for `scheduler_drift`; a failed scheduler read is UNVERIFIED.
Buffer plans are read-only: implement moves inside the current canonical lock,
rechecking state/dependencies before acting. Retained IN-FLIGHT packets cannot spawn.

## Verification and reporting

- Run `python3 -S run_tests.py --executor <isolated-current-executor>` and current
  repository gates. Do not reinterpret the old 344 tests as the live release gate.
- Keep APIs/browser submits disabled in test fixtures; the runner blocks network
  and subprocess execution inside tests. Do not disable that guard for a test.
- Capture shadow nominal vs executable READY, loss reasons, by-route inventory,
  runway, bounded refill proposals and missing measurement coverage.
- Only claim an ID cleared when its specific acceptance evidence is added to
  the authoritative register. Code, adapter wiring, live proof and independent
  validation are separate stages.
- Report exact files/revisions changed, tests actually executed, source conflicts,
  remaining provider/user dependencies and whether anything was deployed.

No new paid service is required for these steps. A production perimeter,
separate signer identity, provider validation and independent-user evaluation
still require implementation/operational evidence; this package does not claim
those tasks complete or silently authorize broader deployment changes.
