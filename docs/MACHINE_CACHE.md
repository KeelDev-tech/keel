# Persistent pure-computation cache

`keel_machine.cache.ComputationCache` avoids recomputing identical local analysis,
preparation, and test results. It uses a private SQLite database and the standard
library. It executes no callbacks, makes no network requests, and grants no
application permission.

Purity is established by the trusted host's reviewed operation registry. A label
such as `analysis` cannot prove that arbitrary external code is pure. The cache
stores inert JSON only; it cannot be used as an authority store. Current consent,
operator identity, provider receipt validity, approvals, revocations, policy
checks, execution gates, and other live conditions must be checked outside the
cache. Cached text or booleans claiming approval never establish permission.

## API

```python
from keel_machine.cache import ComputationCache
from keel_machine.common import digest

# Parent directory must already exist, owned by the host, with mode 0700.
cache = ComputationCache("/private/keel/computations.sqlite")
key = {
    "schema": "keel.machine.cache-key.v1",
    "account_id": "account-A",
    "scope": "workspace-A",
    "purpose": "analysis",
    "operation": "reviewed-local-score",
    "implementation_sha256": implementation_digest,
    "config_sha256": digest(configuration),
    "inputs_sha256": digest(normalized_inputs),
    "dependencies": {"profile": profile_revision_digest},
    "contract_sha256": result_contract_digest,
}
result = cache.get(key)
if result["status"] == "MISS":
    # This function is registered as pure by trusted host code.
    value = reviewed_pure_function(normalized_inputs, configuration)
    result = cache.put(key, value, ttl_seconds=3600)
```

The caller must handle `HELD` explicitly. It must not assume an input revision,
dependency, or implementation digest from a name or filename. Hash the relevant
bytes/normalized values and include every dependency affecting the result.
Changing an input without changing its declared digest is a host integration
error this cache cannot discover.

Constructor:

```python
ComputationCache(path, clock=time.time, max_entries=1024, max_bytes=16777216)
```

- `get(key)` returns `HIT`, `MISS`, or `HELD`, plus `key_sha256` and
  `output_sha256`. Only `HIT` includes a fresh JSON copy in `value`.
- `put(key, value, ttl_seconds=3600)` returns `STORED`, `REFRESHED`, or `HELD`.
  Successful results include the expiry and number of evicted entries.
- `stats()` reports entry counts, held/ready counts, canonical byte usage,
  configured limits, observation time, and `execution_authorized=false`.
- `MachineError` signals malformed input, invalid clocks, changed configuration,
  or unavailable/corrupt storage metadata. No invalid-input failure is a HIT.

## Exact cache-key contract

Every field shown above is required, and additional fields are rejected.
`purpose` must be `analysis`, `preparation`, or `test`. Account, scope, operation,
and dependency IDs match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. Revision values are
lowercase 64-character SHA-256 digests. At most 128 dependency entries are allowed.

Canonical JSON uses sorted keys, compact UTF-8 encoding, and finite numbers.
Dictionary ordering does not change identity. Account, scope, purpose, operation,
implementation, configuration, inputs, every dependency, and output contract all
participate in the key digest. They are not optional hints or fuzzy comparisons.

Keys are limited to 65,536 canonical bytes. Results are limited to 262,144 bytes
and the shared JSON bounds: 32 nesting levels, 65,536 visited values, bounded
collections, and signed 63-bit integer magnitude. Sets, custom objects, cycles,
nonfinite values, and unsupported types are rejected. No pickle, arbitrary
imports, or executable serialized objects are accepted.

## Persistence, expiry, and capacity

The database records its schema and configured capacity. Reopening an existing
cache with different `max_entries` or `max_bytes` fails explicitly. Use a new
cache database when intentionally changing the configuration. Account/scope
separation is part of keys; this is a trusted local utility, not a remote
multi-tenant authentication endpoint.

TTL must be positive, finite, and at most 30 days. At the expiry boundary a ready
entry is removed and lookup returns MISS. An identical recomputation refreshes
the TTL. Reads do not extend TTL. Successful reads and writes advance a durable
access counter, so least-recently-used eviction remains deterministic even when
wall-clock values are equal. Expired entries are removed before admission.

`max_bytes` measures **live canonical key plus result bytes**, not SQLite file
pages, indexes, filesystem overhead, or high-water disk allocation. The cache
also bounds entry count; limits may be configured up to 100,000 entries and
256 MiB of canonical bytes. A single result exceeding the configured byte budget
is rejected before modifying the cache. Eviction removes only ready entries.

Time is observed after the SQLite write lock is acquired and again before
commit. A backward or malformed clock fails closed and rolls back the operation.
The last observed time persists across restarts. Hosts must repair their clock
or intentionally create a separate cache after a real wall-clock rollback;
there is no silent time reset. TTL checks use the lookup transaction's start time.

## Corruption and nondeterminism

On each HIT, the cache verifies the exact stored canonical key, result digest,
result encoding, relevant metadata, and JSON bounds. Invalid entries become
`HELD`; their payload is discarded and no value is returned. This is a
reproducible computation cache, not a forensic evidence archive. The quarantine
retains the key, reason, and original digest when well formed.

A second computation with the same current key and a different output digest
creates `NONDETERMINISTIC_OUTPUT`. It never overwrites the first result or
selects whichever worker finished last. The hold survives restart and TTL;
recomputing the original output does not clear it.

Held keys count toward bounded capacity and are not LRU-evicted. If held keys
leave no capacity, a new put returns `HELD` with `CAPACITY_HELD`, without creating
unbounded tombstones or destroying unrelated successful entries. Its next lookup
remains MISS because that rejected admission was not stored. A trusted host must
review the defective computation/key design and use an appropriately revised
key or a deliberately new cache. There is no automatic hold-clear API.

This cache is not a permanent nondeterminism ledger: after a successful entry
expires or is LRU-evicted, its old result is no longer available for comparison.
Use durable reliability observations for long-term reproducibility evidence.

## Storage and tested limits

Shared `PrivateDB` creates a mode-0600 SQLite file exclusively inside an existing
mode-0700 parent. It rejects symlink/hardlink and replacement hazards, checks
ancestor ownership and writable-directory conditions, and uses immediate
transactions with full synchronization. Competing writers coordinate through
SQLite; concurrent identical writes produce one entry, and conflicting outputs
produce a hold.

The regression suite covers account and revision isolation, canonical JSON,
expiry and refresh, deterministic LRU, byte and entry bounds, held capacity,
corrupt payloads and metadata, clock rollback, reopening, replacement detection,
and concurrent writes. It demonstrates local behavior with synthetic data, not
improved production outcomes. The host OS administrator and same-UID processes
remain trusted; the cache is not an OS sandbox or a tamper-proof ledger. Linux
storage behavior is tested; native Windows ACL behavior is not qualified.
