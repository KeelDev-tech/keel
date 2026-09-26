#!/usr/bin/env python3
"""Short-lived, process-local cache for bounded public HTTPS GET responses.

All misses use safe_http's checked-IP TLS transport, per-hop admission,
4 MiB maximum, and durable 429 holds. Hits enforce the caller's byte/age
limits and both shared and legacy host holds. Cache keys preserve path and
query semantics; only safe_http's authority/fragment normalization applies.
No proxy, HTTP downgrade, credentials, POST, or oversized-body fallback.

Lineage notes (ported from the pre-evolution line):
  - One canonical GET path for the packet-build / verification HTTP traffic:
    form_intel, ats (resolve_final_url / board APIs), api_direct_detect,
    lever_submit, and verify_retry's sync path (which funnels through ats).
  - Measurement support (J-20260916-0022-inte-426): stats() returns the
    counters above plus cache/inflight gauges; disable()/enable() bypass
    the cache for baseline-vs-post measurement (disable() also retires
    in-flight coalesced requests via a generation bump).
  - The 30-min pulse's async verify path (aiohttp) is out of scope for
    this module — its per-host semaphore pacing already bounds it.
  - Fail-closed: any cache-internal exception degrades to a direct network
    fetch; the caller's timeout/UA semantics are preserved.
"""

import json
import math
from collections import OrderedDict
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import host_cooldowns, safe_http
except ImportError:  # Legacy engine scripts use top-level imports.
    import host_cooldowns
    import safe_http

DEFAULT_TTL = 600  # seconds; also the maximum accepted retention
MAX_ENTRIES = 256
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_INFLIGHT = 16
MAX_FOLLOWERS_PER_KEY = 32
UA = {"User-Agent": "Mozilla/5.0"}

_lock = threading.Lock()
_store = OrderedDict()  # Least recently used first; body bytes are immutable.
_cache_bytes = 0
_generation = 0
_flights = {}  # (generation, URL) -> unfinished shared request
_slots = set()  # Includes completed flights retained by waiting consumers.
_disabled = False
_COUNTERS = ("network_calls", "network_attempts", "network_errors", "network_bytes",
             "hits", "served_bytes", "coalesced_hits", "followers_joined",
             "follower_timeouts", "evictions", "expired", "oversize_uncached", "saturation_rejections")
_stats = dict.fromkeys(_COUNTERS, 0)
_request_budget = threading.local()


class _Flight:
    def __init__(self, key, generation, shared):
        self.key, self.generation, self.shared = key, generation, shared
        self.ready = threading.Event()
        self.followers = 0
        self.done = False
        self.result = None
        self.error = "request_failed"
        self.error_url = key


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("public HTTPS cache deadline exceeded")
    return min(60, remaining)


def _remove(key, reason):
    global _cache_bytes
    row = _store.pop(key)
    _cache_bytes -= len(row["body"])
    _stats[reason] += 1


def _expire(now):
    # Bounded scan: at most MAX_ENTRIES. No background thread or timer.
    for key in list(_store):
        row = _store[key]
        if now < row["created"] or row["expires"] <= now:
            _remove(key, "expired")


def _publish(key, row):
    global _cache_bytes
    if len(row["body"]) > MAX_CACHE_BYTES:
        _stats["oversize_uncached"] += 1
        return
    if key in _store:
        _cache_bytes -= len(_store.pop(key)["body"])
    while _store and (len(_store) >= MAX_ENTRIES or _cache_bytes + len(row["body"]) > MAX_CACHE_BYTES):
        _remove(next(iter(_store)), "evictions")
    _store[key] = row
    _cache_bytes += len(row["body"])


def _admit(targets, deadline):
    for target in dict.fromkeys(targets):
        safe_http.check_host_cooldown(target, timeout=_remaining(deadline))
        _legacy_admission(target, deadline=deadline)
        _remaining(deadline)


def _cached_result(key, row, *, ttl, limit, deadline, generation, coalesced=False):
    # Every recipient checks both stores, including a redirected final host.
    _admit((key, row["final_url"]), deadline)
    now = time.monotonic()
    if now < row["created"] or now - row["created"] >= ttl or now >= row["expires"]:
        raise safe_http.NetworkPolicyError("cached response exceeds caller age limit")
    if len(row["body"]) > limit:
        raise safe_http.NetworkPolicyError("cached response exceeds caller size limit")
    _remaining(deadline)
    with _lock:
        if generation != _generation or _disabled:
            raise safe_http.NetworkPolicyError("cache invalidated during read")
        _stats["hits"] += 1
        _stats["served_bytes"] += len(row["body"])
        _stats["coalesced_hits"] += int(coalesced)
        if _store.get(key) is row:
            _store.move_to_end(key)
    return row["body"], row["final_url"], True


def canonical_key(url: str) -> str:
    """Validate before lookup, preserving meaningful path/query differences."""
    return safe_http.validate_url(url)


def _legacy_admission(url, *, deadline=None):
    # Preserve the fixed safe_http callback identity while carrying the current
    # fetch budget through redirect admission on this thread only.
    deadline = getattr(_request_budget, 'deadline', None) if deadline is None else deadline
    timeout = 5.0 if deadline is None else min(5.0, _remaining(deadline))
    host_cooldowns.check_host(urllib.parse.urlsplit(url).hostname, timeout=timeout)
    if deadline is not None:
        _remaining(deadline)


class _BoundedRequest(urllib.request.Request):
    def __init__(self, url, limit):
        super().__init__(url, headers=UA, method="GET")
        self.max_bytes = limit


class _PinnedOpener:
    """Keep the existing open(req, timeout) test seam without a raw opener."""

    def open(self, req, timeout):
        return safe_http.urlopen(req, timeout=timeout, max_bytes=req.max_bytes,
                                 before_request=_legacy_admission)


_OPENER = _PinnedOpener()


def _network_get(url, timeout, limit=safe_http.MAX_BYTES):
    target = safe_http.validate_url(url)
    req = _BoundedRequest(target, limit)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            body = resp.read(limit + 1)
            if len(body) > limit:
                raise safe_http.NetworkPolicyError("response exceeds caller size limit")
            final_url = safe_http.validate_url(resp.geturl())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # safe_http records its own hold before raising; preserve the
            # legacy async reader's store too, including redirected hosts.
            host = urllib.parse.urlsplit(safe_http.validate_url(exc.url)).hostname
            deadline = getattr(_request_budget, 'deadline', None)
            remaining = 5.0 if deadline is None else max(0, min(5.0, deadline - time.monotonic()))
            # Even an exhausted deadline must install the in-process hold before
            # refusing persistence. Do not reset the caller's budget to 5 seconds.
            host_cooldowns.record_429(host, (exc.headers or {}).get("Retry-After"), timeout=remaining)
        raise
    if not 200 <= status < 300:
        raise urllib.error.HTTPError(target, status, "non-2xx", None, None)
    return body, final_url


def fetch(url, timeout=25, ttl=DEFAULT_TTL, limit=safe_http.MAX_BYTES):
    """GET -> (body, final_url, from_cache), enforcing bounds even on hits.

    ttl is a maximum acceptable age in seconds (0 bypasses cache), at most
    600. Bodies over the caller limit are refused, never truncated. The
    shared transport's hard maximum is 4 MiB, including board API reads;
    callers needing larger boards must use a separately reviewed paginated
    reader instead of increasing this limit.
    """
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 <= ttl <= DEFAULT_TTL:
        raise safe_http.NetworkPolicyError("cache TTL must be between 0 and 600 seconds")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise safe_http.NetworkPolicyError("timeout must be between 0 and 60 seconds")
    if type(limit) is not int or not 0 < limit <= safe_http.MAX_BYTES:
        raise safe_http.NetworkPolicyError("response limit must be an integer from 1 byte to 4 MiB")
    deadline = time.monotonic() + timeout
    key = canonical_key(url)
    # Check policy before waiting behind another caller or doing any I/O. A
    # cache hit or flight result is only a response reuse, never new admission.
    _admit((key,), deadline)
    with _lock:
        now = time.monotonic()
        _remaining(deadline)
        _expire(now)
        generation = _generation
        shared = not _disabled and ttl > 0
        hit = _store.get(key) if shared else None
        if hit and now - hit["created"] >= ttl:
            hit = None  # Other callers may still accept this younger-than-TTL entry.
        flight = None
        follower = False
        if hit is None:
            flight = _flights.get((generation, key)) if shared else None
            if flight is not None:
                if flight.followers >= MAX_FOLLOWERS_PER_KEY:
                    _stats["saturation_rejections"] += 1
                    raise safe_http.NetworkPolicyError("public HTTPS follower capacity exhausted")
                flight.followers += 1
                _stats["followers_joined"] += 1
                follower = True
            else:
                if len(_slots) >= MAX_INFLIGHT:
                    _stats["saturation_rejections"] += 1
                    raise safe_http.NetworkPolicyError("public HTTPS request capacity exhausted")
                flight = _Flight(key, generation, shared)
                _slots.add(flight)
                if shared:
                    _flights[(generation, key)] = flight
    if hit is not None:
        return _cached_result(key, hit, ttl=ttl, limit=limit, deadline=deadline, generation=generation)
    if follower:
        try:
            if not flight.ready.wait(_remaining(deadline)):
                raise TimeoutError("public HTTPS shared request deadline exceeded")
            _remaining(deadline)
            if flight.result is None:
                _admit((key, flight.error_url), deadline)
                if flight.error == "http_429":
                    raise urllib.error.HTTPError(flight.error_url, 429, "shared request rate limited", None, None)
                raise safe_http.NetworkPolicyError("shared public HTTPS request failed; no automatic retry")
            return _cached_result(key, flight.result, ttl=ttl, limit=limit,
                                  deadline=deadline, generation=generation, coalesced=True)
        except TimeoutError:
            with _lock:
                _stats["follower_timeouts"] += 1
            raise
        finally:
            with _lock:
                flight.followers -= 1
                if flight.done and not flight.followers:
                    _slots.discard(flight)
    previous_deadline = getattr(_request_budget, 'deadline', None)
    _request_budget.deadline = deadline
    try:
        with _lock:
            _stats["network_attempts"] += 1
        body, final_url = _network_get(key, _remaining(deadline), limit=limit)
        if type(body) is not bytes or len(body) > limit:
            raise safe_http.NetworkPolicyError("response exceeds caller size limit or is not bytes")
        final_url = safe_http.validate_url(final_url)
        # A 429 in another worker between fetch and publication must stop reuse.
        _admit((key, final_url), deadline)
        created = time.monotonic()
        row = {"body": body, "final_url": final_url, "created": created, "expires": created + ttl}
        with _lock:
            _remaining(deadline)
            _expire(created)
            if shared and generation == _generation and not _disabled:
                _publish(key, row)
            flight.result = row
            _stats["network_calls"] += 1
            _stats["network_bytes"] += len(body)
        return body, final_url, False
    except BaseException as exc:
        with _lock:
            _stats["network_errors"] += 1
            rate_error = exc if isinstance(exc, urllib.error.HTTPError) else exc.__context__
            if isinstance(rate_error, urllib.error.HTTPError) and rate_error.code == 429:
                flight.error = "http_429"
                try:
                    flight.error_url = safe_http.validate_url(rate_error.url)
                except Exception:
                    pass
        raise
    finally:
        if previous_deadline is None:
            del _request_budget.deadline
        else:
            _request_budget.deadline = previous_deadline
        with _lock:
            flight.done = True
            if shared and _flights.get((generation, key)) is flight:
                del _flights[(generation, key)]
            if not flight.followers:
                _slots.discard(flight)
            flight.ready.set()


def fetch_text(url, timeout=25, ttl=DEFAULT_TTL,
               limit=safe_http.MAX_BYTES):
    """fetch() decoded as UTF-8 text (replace errors)."""
    body, final_url, hit = fetch(url, timeout=timeout, ttl=ttl, limit=limit)
    return body.decode("utf-8", "replace"), final_url, hit


def fetch_json(url, timeout=20, ttl=DEFAULT_TTL,
               limit=safe_http.MAX_BYTES):
    """fetch() parsed as JSON."""
    body, final_url, hit = fetch(url, timeout=timeout, ttl=ttl, limit=limit)
    return json.loads(body.decode("utf-8")), final_url, hit


def resolve_final_url(url, timeout=15, ttl=DEFAULT_TTL):
    """Redirect-following URL resolution, cache-backed.

    Same contract as ats.resolve_final_url: returns the final URL, or the
    original URL on any failure.
    """
    try:
        _, final_url, _ = fetch(url, timeout=timeout, ttl=ttl)
        return final_url
    except Exception:
        return url


def stats():
    with _lock:
        _expire(time.monotonic())
        return {**_stats, 'cache_entries': len(_store), 'cache_bytes': _cache_bytes,
                'inflight_keys': len(_flights), 'inflight_requests': sum(not f.done for f in _slots),
                'retained_flight_slots': len(_slots), 'waiting_followers': sum(f.followers for f in _slots),
                'max_entries': MAX_ENTRIES, 'max_cache_bytes': MAX_CACHE_BYTES,
                'max_inflight': MAX_INFLIGHT, 'max_followers_per_key': MAX_FOLLOWERS_PER_KEY}


def reset_stats():
    with _lock:
        _stats.update(dict.fromkeys(_COUNTERS, 0))


def clear():
    global _cache_bytes, _generation
    with _lock:
        _store.clear()
        _cache_bytes = 0
        _generation += 1


def disable():
    """Bypass the cache (all calls go to network). For measurement."""
    global _disabled, _generation
    with _lock:
        _disabled = True
        _generation += 1


def enable():
    global _disabled
    with _lock:
        _disabled = False


def cache_size():
    with _lock:
        _expire(time.monotonic())
        return len(_store)
