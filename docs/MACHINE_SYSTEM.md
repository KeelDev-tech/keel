# Machine execution extensions

For the concrete owner-controlled worker, see `MACHINE_RUNTIME.md`: the shipped
`init`, `submit`, `work`, `status`, `result` and `hold` commands now wire these
modules to the existing coordinator and durable result store.

This layer improves local execution without adding human review tasks or a paid
service. It provides incremental pure computation, contract/failure monitoring,
and a bounded HTTP cache. It extends the existing coordinator, admission gates,
HTTP transport and lexical processing. It does not replace their authority.

## Run

```bash
python3 -B -m keel_machine capabilities
python3 -B -m keel_machine demo --home "$HOME/keel-machine-synthetic-demo"
```

The demo directory must be new under an existing owner-controlled parent. The
storage directory is mode 0700 and SQLite files are mode 0600. Writable non-sticky
ancestors, symlinks, hardlinks and substituted database identities are rejected.
Run on the supported Linux/POSIX Python host. The modules use the standard library
and existing Keel code; no model, server subscription or cloud account is required.

The synthetic demo runs two independent lexical preprocessing branches and one
dependent comparison using the existing retrieval tokenizer. Its checks require:

1. First run: three node computations.
2. Unchanged repeat: zero computations and three cache hits.
3. Change one input: two computations and one cache hit.
4. Revoke live host admission: no returned output, including cached output.
5. Supply an incorrect input type: hold the affected node and dependent result.
6. Inject repeated synthetic contract failures: produce a drift hold.

The last check illustrates detector arithmetic under controlled fault injection.
It is not a statistical finding about real production traffic. The demo performs
no network requests, model calls, external actions or production deployment.

## Incremental computation

`GraphRunner` accepts a validated acyclic graph of at most 64 nodes. Functions are
registered by trusted host code using `Operation`; JSON cannot import modules,
select an executable, or register a function. Each function takes one JSON object
with `arguments`, `config` and `dependencies`. Both inputs and outputs must satisfy
explicit closed contracts. These are a bounded subset of JSON Schema, not a full
general-purpose schema implementation.

The computation key binds account, scope, purpose, operation implementation,
input values, configuration, source revisions, contracts, and each upstream
computation key and output digest. Thus a changed branch invalidates its
descendants even when an intermediate value happens to remain equal. Independent
unchanged branches can reuse results across runs. The run ID does not affect the
computation key, but it does bind current host admission.

The cache validates stored bytes and output hashes on every hit. Conflicting
outputs for a retained key create a persistent hold. TTL/LRU enforce logical byte
and entry limits; held entries are not silently evicted. This is bounded result
storage, not an unlimited history of nondeterminism or a disk quota. See
`MACHINE_CACHE.md` for capacity, expiry and corruption behavior.

No model output is inherently deterministic. Register only reviewed pure local
functions, and include every relevant helper source, configuration and external
input revision in the pins. Plain module functions without closures are required;
that syntactic check does not prove purity. Globals, imported native libraries,
environment variables, hardware and external service state are not automatically
attested. Do not cache decisions granting approval or executing effects.

Source-file hashes, Python bytecode, default arguments and contracts are pinned
at operation registration and checked around execution. A changed operation is
held; registering the reviewed new implementation is explicit. These pins can
change across extraction paths or Python versions, producing appropriate misses.
The same-UID host owner remains trusted.

## Live admission and coherent results

Construct `GraphRunner(cache, operations, trusted_guard)`. For node requests, the
guard receives `keel.machine.admission.v1` with account/scope/purpose, run and plan
bindings, node ID, source revisions, cache key, phase, time and a fresh nonce.
Return an actual `GuardDecision` that binds the complete request digest and an
expiry no more than 60 seconds after the supplied time.

The phases `before`, `after`, and `return` check current state before cache use,
after computation/readback and before returning results. A cache hit never
substitutes for fresh admission or output-contract validation.

After per-node checks, a final request with schema
`keel.machine.graph-admission.v1` and phase `graph-return` includes every node's
revisions, cache key and output hash. The host must validate this request against
one coherent current snapshot. A later callback can invalidate an earlier node;
per-node checks alone cannot attest graph-wide freshness. The final request is
the final host callback before return, and a rejection returns no outputs.

A constant-true callback is only appropriate for an explicitly synthetic fixture.
The host owns authentication, snapshot coherence and revocation checks. An
admission observation cannot guarantee future validity after the graph returns;
any later consumer or effect gateway must check its own current authority.

The engine returns no partial outputs if any node is held. Computation counts
include attempted callbacks, including ones that fail. A fixed computation count
and cooperative wall deadline limit accepted work. A stuck Python callback cannot
be forcibly interrupted by this in-process engine. Run untrusted or potentially
unbounded work under Keel's existing isolated worker and resource enforcement.
The graph's byte/structure/count limits do not replace kernel enforcement.

Final reports are bounded to 256 KiB. Oversized combined results return a hold
with no output payload. Callbacks receive copies, and failures retain fixed
machine codes rather than arbitrary exception prose.

## Existing coordinator integration

`make_coordinator_handler(runner, record_result)` in `keel_machine.adapters`
connects a registered graph to the existing Muse coordinator. Task scope and
account must match; `task.dependencies.graph_plan` binds the exact graph.
`record_result` is a trusted durable result writer and must return the canonical
report digest. Graph holds return BLOCKED without a result receipt. Writer
uncertainty follows the coordinator's existing UNKNOWN behavior. See
`MACHINE_COORDINATOR.md` for the complete contract and its host qualification.

The existing coordinator already supplies bounded concurrency, leases, restart
fencing and account fairness. This extension adds computation reuse rather than
another queue or new automatic submission path.

## Contract and failure monitoring

`check_contract` rejects missing/extra fields, wrong types, unsupported schema
keywords and oversized data. Rejections are content-free machine codes. A
`FailureMonitor` tracks host-supplied binary failures against a frozen baseline,
with exact replay, current configuration bindings and a conservative sequential
bound. A supported increase returns HOLD; NO_ALARM does not prove no drift.

The host must record actual machine observations with honest unique identities
and applicable independence assumptions. Hashing an invented event does not
authenticate it. This monitor does not certify the learning controller's broader
`no_detected_distribution_drift` assertion. No route or policy is automatically
rewritten. See `MACHINE_SENTRY.md` for the statistical scope and formula.

## HTTP request reuse

The existing `engines.http_cache` now bounds successful cached content and
coalesces simultaneous cacheable requests to the same URL. Each follower has its
own deadline, response-size limit and acceptable age. Admission and 429 stops
are rechecked for every consumer. Failures wake followers without automatic
retry. Expired entries are removed and LRU eviction bounds retained content.

This is process-local sharing, not distributed exactly-once retrieval. The
leader's request limit governs the shared response; a failed shared request is
not automatically repeated with a larger limit. See `HTTP_CACHE_MACHINE.md`.

## Qualification

Unit and integration tests use private synthetic workspaces and fake network
transports; they do not call employers or providers. The handoff records actual
test counts and unresolved historical/environment checks. Existing browser and
cgroup host qualifications remain separate. No production performance gain,
state-of-the-art superiority, arbitrary-function purity, or universal drift
detection is claimed.
