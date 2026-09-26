# Bounded concurrent public-read caching

`engines/http_cache.py` now shares simultaneous public HTTPS GETs for the same
canonical URL and bounds its local memory use. It is part of the existing reader
path; no new scheduler, daemon, service, subscription or model is required.

Existing APIs remain available:

```python
from engines import http_cache

body, final_url, reused = http_cache.fetch(url, timeout=10, ttl=60, limit=1048576)
text, final_url, reused = http_cache.fetch_text(url, timeout=10, ttl=60)
value, final_url, reused = http_cache.fetch_json(url, timeout=10, ttl=60)
```

These examples make real GET requests when used with real URLs. The tests below
use only synthetic transports and never query a live provider.

## Limits and reuse

| Resource | Default bound |
|---|---:|
| Cached responses | 256 entries |
| Cached response bodies | 16 MiB total |
| A single transport response | Existing 4 MiB hard limit |
| Active requests plus completed flights retained by followers | 16 slots |
| Followers per shared request | 32 |
| Response retention / maximum acceptable caller age | 600 seconds |
| Per-call timeout | Greater than zero, at most 60 seconds |

The least recently used entry is evicted when either cache capacity is reached.
Expired entries are removed on fetch, statistics reads and `cache_size()`. No
background cleanup thread runs. A caller requesting a stricter age than an
otherwise valid entry causes a miss without deleting data another caller can
still use. A successful response larger than the configured cache-byte capacity
may be returned directly but is not retained.

Body memory retained by flights is also bounded: a completed flight continues to
occupy its slot until its last waiting follower finishes. Each slot can retain at
most one bounded response body. Cache and flight references can share the same
immutable bytes. The bounds do not cover objects retained by calling application
code after return, Python interpreter overhead, or a malicious replacement
transport that violates the existing transport contract.

Only one leader performs a GET for an active shared URL. Followers wait on its
completion without holding the global cache lock. Different URLs can progress
concurrently. `reused` is `True` for a valid cache hit or a successfully shared
result, and `False` for the caller that actually performed the transport request.

Each caller retains its own byte limit, age limit and deadline. The leader uses
its exact requested byte limit in the transport; it is never silently expanded
to accommodate followers. If a leader's limit is too small, the flight fails for
all its current followers. There is no automatic fallback or retry. A subsequent
independent request is a new explicit call. A smaller follower limit rejects a
larger shared result rather than truncating it.

When request or follower capacity is exhausted, the call raises
`safe_http.NetworkPolicyError` without starting another transport or allocating
an unbounded waiting queue. Existing fresh cache hits can still succeed.

`ttl=0` bypasses cache reads, writes **and sharing**. `disable()` also preserves
independent network requests for measurement; capacity and policy limits still
apply. `clear()` and `disable()` advance a generation fence. Older requests
cannot repopulate the cache or deliver shared results after that fence. An
already-running leader can still receive its own currently admitted network
result. These operations do not cancel transports or clear any rate-limit hold.
`enable()` resumes reuse of any still-fresh retained entries.

## Admission and deadlines

Every consumer checks both the shared `safe_http` hold store and the legacy
`host_cooldowns` store. Original and redirected final hosts are rechecked before
cache/shared delivery. Leaders also recheck holds after obtaining a response and
before publication. A 429 observed by another worker during a read can therefore
block the returned response and its cache publication. The existing checked-IP
TLS transport, per-hop redirect admission, URL policy and hard response bound
remain authoritative.

Failures, non-2xx responses, over-limit bodies and late results are never cached.
Normal exceptions and `BaseException` unwinding release followers and slots.
Followers receive a fixed failure description rather than arbitrary exception
prose. A 429 is not retried or converted into success. A killed process loses its
whole process-local cache; this module makes no cross-process sharing claim.

Admission, waiting, transport and final validation consume one monotonic caller
deadline. A follower can time out without cancelling the leader or other
followers. The existing `host_cooldowns.check_host` accepts an optional keyword
`timeout` (default remains five seconds) so its file-lock wait can consume the
remaining budget. Redirect admission inherits that budget through thread-local
state while retaining the existing fixed transport callback identity.

Recording a 429 also consumes the remaining deadline. `record_429` accepts an
optional persistence `timeout` (default five seconds, zero allowed). It installs
the process hold before attempting storage, even when no persistence time remains.
Indefinite process holds remain active after a failed write. Repeated records
merge with both the current process hold and the durable record under its lock:
indefinite holds dominate, and finite expiries can only increase. A shorter
Retry-After from another worker cannot shorten an existing hold. A storage error
still means other processes may not learn a new hold; no cross-process guarantee
is claimed when the hold could not be persisted.

Late results are rejected even if an underlying operation exceeded its time
budget. Physical timing is subject to OS scheduling and filesystem calls; this
is not a real-time execution guarantee or a mechanism to kill arbitrary hung
Python callbacks. The production reader retains its bounded transport and DNS
path. Test-only transports must honor their own completion contract.

## Statistics

`stats()` preserves `network_calls`, `hits` and `served_bytes`. `network_calls`
counts successful admitted leader calls; `hits` and `served_bytes` describe
successful reuse, including followers. Additional counters expose network
attempts/errors/bytes, joined followers, coalesced hits, follower timeouts,
evictions, expiry, uncached oversized entries and saturation rejections.

Current gauges include cached bytes/entries, unfinished shared keys, active
requests, retained flight slots and waiting followers. No URL, response body or
exception prose is included in this statistics snapshot.

`reset_stats()` resets counters without changing entries, active requests,
followers, holds or capacity. A request that started before a reset can finish
after it, so completion and attempt counters span different windows during an
in-progress reset. Gauges always report current state.

## Verification

From the source root, with pytest available:

```bash
PYTHONPATH=.:engines PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -B -m pytest \
  --import-mode=importlib -p no:cacheprovider -q \
  tests/test_http_cache_machine.py \
  tests/test_http_cache_security_regressions.py \
  tests/test_http_policy_contracts.py \
  tests/test_read_transport_audit_20260924.py tests/test_shared_http.py
```

The new cases exercise real thread interleavings, follower expiry, differing
limits, LRU/byte eviction, capacity exhaustion, completed-result retention,
in-flight invalidation, redirected 429s, leader failures including `SystemExit`,
late results and real local file-lock contention. Network exchanges are fakes.
