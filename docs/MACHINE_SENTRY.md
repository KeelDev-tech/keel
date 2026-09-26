# Contract and failure-rate sentries

These standard-library modules add local gates around machine computation. They
make no model calls, change no route, spend no money, and create no approvals.
Their output is deliberately narrower than a security or provider attestation.

## Closed input/output contracts

`keel_machine.contracts.validate_contract(spec)` returns an independently cloned,
normalized contract or raises `MachineError` with a bounded diagnostic code.
`check_contract(spec, value)` returns exactly:

```json
{"status":"PASS","violations":[]}
```

or HOLD with bounded, content-free violation codes. Invalid contracts also HOLD.
The machine graph checks inputs and outputs, including cached values, through
this API; a cached hash alone does not establish a valid result.

Example:

```python
from keel_machine.contracts import check_contract

contract = {
    "type": "object",
    "properties": {
        "posting_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "qualified": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
    },
    "required": ["posting_id", "qualified", "score"],
    "additionalProperties": False,
}
result = check_contract(contract, {
    "posting_id": "posting-123", "qualified": True, "score": 87,
})
```

This is an intentionally bounded subset with JSON Schema-like keywords, **not a
complete JSON Schema implementation**:

| Type | Supported keywords and limits |
|---|---|
| `object` | Explicit `properties`, `required`, and `additionalProperties: false`; optional `minProperties`/`maxProperties`; at most 128 declared properties |
| `array` | Required `items` contract; `minItems` defaults to 0, `maxItems` to 256; maximum 10,000 |
| `string` | `minLength` defaults to 0, `maxLength` to 4,096; maximum 65,536 characters; optional scalar `enum` |
| `integer` / `number` | Finite `minimum`/`maximum`, default −10^15/+10^15; optional scalar `enum` |
| `boolean` | Optional scalar `enum`; integer 0/1 are not booleans |
| `null` | Only JSON null |

Enums contain 1–64 unique values, with canonical JSON equality; `1` and `1.0`
therefore remain distinct enum representations. Integer contracts reject bools
and floating-point representations. Number contracts accept finite integers and
floats but reject bools. Unknown schema keywords, type unions, remote references,
regex patterns, executable callbacks, implicit properties, and permissive extra
properties are rejected. Optional properties can be omitted; explicit null uses
a null contract rather than an implicit union.

The normalized schema is limited to 512 schema nodes and 12 schema levels.
Values are limited to 20,000 traversed nodes, 24 levels, 262,144 encoded bytes,
and the common JSON integer range of ±(2^63−1). Object keys are bounded strings.
Oversized structures, cycles, unsupported Python objects, invalid Unicode, NaN,
and infinity hold before processing. No error message includes user data or
object paths. Bounds are resource controls and shape checks; passing a contract
does not prove that text is truthful, a receipt authentic, or an action approved.

## Durable failure-rate monitoring

`keel_machine.drift.FailureMonitor` monitors one predeclared binary outcome under
one frozen source/configuration/model/calibration binding. It stores an immutable
baseline and an append-only cumulative sequence in the existing `PrivateDB`
wrapper. Create its directory with mode 0700 before constructing it; the SQLite
file and sidecars must remain private, regular, owner-controlled files. The
storage layer rejects replacement, symlinks, unsafe ancestors, and hardlinks.
Other same-account processes and the host administrator remain trusted.

```python
from keel_machine.drift import FailureMonitor

baseline = {
    "schema": "keel.machine.failure-baseline.v1",
    "baseline_id": "preparation-v1",
    "bindings": {
        "source_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "model_sha256": "c" * 64,
        "calibration_sha256": "d" * 64,
    },
    "sample_count": 1000,
    "failures": 10,
    "tolerance": 0.05,
    "delta": 0.05,
    "min_samples": 30,
    "max_samples": 100000,
    "evidence_sha256": "e" * 64,
}
# These example hashes/counts are placeholders, not measured baseline evidence.
monitor = FailureMonitor("/private/keel/failure-sentry.sqlite", baseline=baseline)
# Call only from trusted instrumentation after a real runner outcome is known.
result = monitor.record(
    "actual-attempt-id", failed=True,
    current_bindings=baseline["bindings"],
    provenance_sha256="f" * 64,  # replace with the exact observed event digest
)
report = monitor.report(current_bindings=baseline["bindings"])
```

The host must supply genuine baseline evidence, a fixed outcome definition and
workload, and actual runner measurements. Do not expose this object to a worker
or treat worker-supplied JSON as host instrumentation. `provenance_sha256` binds
a measurement but does not authenticate it. The monitor deliberately reports
`provenance_authenticated_here: false` and cannot manufacture backend identity.
There is no network importer or automatically installed permissive verifier.

The same sample ID and exact outcome/provenance replay idempotently. A changed
payload for an existing ID rejects the transaction. Renaming an observation
while keeping its provenance hash is rejected too. Distinct hashes cannot prove
distinct real units; the host still owns correct measurement and identity.
Records are serialized into a durable `window_sequence`, with no pruning,
rolling reset, or implicit baseline update. Changed current bindings yield HOLD
without mixing new-version measurements into the original series. Invalid bools,
future-invented predictions, or missing provenance are not inferred as successes.
The API accepts only actual failure booleans; truth of the observation remains
part of the host contract. Clock observations occur after the transaction lock
and again before commit. Reports, exact replays, and binding/capacity holds all
advance the durable last-seen time. A regressed clock rejects ingestion and holds
reports without rolling the stored timestamp backward.

## Statistical method and scope

Let the frozen baseline contain N independent Bernoulli outcomes from the
predeclared reference workload, with empirical failure rate b. Let the live
stream have n bounded binary outcomes with empirical rate f_n. The implementation
uses these one-sided endpoints:

    baseline_upper = min(1, b + sqrt(log(2/delta)/(2*N)))
    live_lower(n)  = max(0, f_n - sqrt(log(2*n*(n+1)/delta)/(2*n)))
    increase_lower(n) = live_lower(n) - baseline_upper

The baseline upper spends delta/2 once. Look n spends delta/[2*n*(n+1)] for the
live lower. Since sum_n 1/[n(n+1)] = 1, the union of the baseline error and all
live-look errors is bounded by delta under the assumptions. Live conditional
Hoeffding bounds cover the running average of conditional failure probabilities,
allowing adapted binary outcomes. A constant underlying probability gives the
usual fixed-rate interpretation. This is a conservative Hoeffding/time-union
construction, not a mixture-optimal, empirical-Bernstein, or change-point
localization algorithm. Floating endpoints are rounded outward.

A confirmed increase requires `n >= min_samples` and the **lower** bound on the
increase to exceed the preregistered tolerance. Baseline uncertainty is retained;
a tiny baseline cannot create false precision. In particular a one-sample
baseline may prevent detection despite many later measurements. Looking at the
report repeatedly or stopping on a signal does not consume an untracked extra
per-look alpha budget. Choosing the baseline, tolerance or monitored objective
after seeing adverse outcomes, or repeatedly opening new monitors until one
alarms, invalidates a claimed family-wide guarantee. Multiple monitors need an
external host-wide alpha allocation; this module controls one frozen monitor.

No overlap between baseline and live samples, honest unit definitions, stable
outcome semantics, representative baseline selection, and baseline independence
must be established outside this module. Version pins detect declared changes;
they cannot prove that backend weights match a supplied hash. The live bound
concerns its observed conditional-risk history and does not predict future
failures or establish the absence of arbitrary distribution shift.

| Status | Interpretation |
|---|---|
| `INSUFFICIENT_DATA` | Fewer than the predeclared minimum live observations; no no-drift claim |
| `NO_ALARM` | No increase has been established under this test; **not** proof that drift is absent |
| `HOLD` | Confirmed increase, changed bindings, clock regression, or exhausted observation capacity |

A confirmed alarm is sticky across later successes and restarts. A new host
release/baseline decision is required to qualify a different deployment; no
human review workflow is automatically created here. Capacity exhaustion also
holds rather than silently resetting the statistical sequence. Every report
keeps `no_drift_proven: false`, `execution_authorized: false`, and `route_writes: 0`.
Do not automatically translate NO_ALARM into the improvement controller's
`no_detected_distribution_drift` attestation: that controller may evaluate a
different outcome, population, time window, or configuration. This sentry is
only scoped supporting evidence for a separately validated integration.

## Validation

Contract tests cover strict nested shapes, missing/extra properties, enum and
bound violations, bool/int separation, unsupported executable schema features,
nonfinite values, cycles, Unicode failures, resource bounds, and normalization.
Sentry tests cover known analytical endpoints, insufficient/no-alarm behavior,
confirmed and sticky increases, restart persistence, exact replay, conflicting
samples, provenance reuse, frozen baseline replacement, binding drift, capacity,
clock regression, and malformed inputs. A seeded repeated-look null simulation
serves as a regression check; the stated Hoeffding/union-bound argument supplies
the mathematical guarantee, rather than the simulation itself.
