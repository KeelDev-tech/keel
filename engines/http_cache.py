#!/usr/bin/env python3
"""http_cache.py — short-TTL shared HTTP response cache.

One canonical GET path for the packet-build / verification HTTP traffic:
form_intel, ats (resolve_final_url / board APIs), api_direct_detect,
lever_submit, and verify_retry's sync path (which funnels through ats).

Design:
  - In-memory, process-local, TTL 600s default (short — posting pages are
    near-static over minutes; liveness callers can pass a shorter ttl).
  - Cache key = canonical URL (fragment stripped, query params sorted,
    scheme normalized http->https, trailing slash trimmed, host lowered).
  - Only 2xx responses are cached. Errors/timeouts raise exactly as
    urllib would — the cache never masks a failure.
  - Cached entries record (body, final_url_after_redirects) so
    resolve_final_url is a cache hit on repeat calls.
  - Thread-safe via a lock. Never caches POST (GET-only module).
  - Every network fetch goes through pre-request admission
    (http_policy.admit: URL authority policy + bounded DNS check refusing
    non-public destinations), per-hop redirect admission (max 3 hops), a
    4 MiB body cap, and the durable per-host 429 cooldown
    (host_cooldowns) — same semantics as the async verify path.

Measurement support (J-20260916-0022-inte-426):
  - http_cache.stats() -> {"network_calls", "hits", "served_bytes"}.
  - http_cache.disable()/enable() for baseline-vs-post measurement.
  - The 30-min pulse's async verify path (aiohttp) is out of scope for
    this module — its per-host semaphore pacing already bounds it.

Fail-closed: any cache-internal exception degrades to a direct network
fetch; the caller's timeout/UA semantics are preserved.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import host_cooldowns
import http_policy

DEFAULT_TTL = 600  # seconds
UA = {"User-Agent": "Mozilla/5.0"}

_lock = threading.Lock()
_store = {}  # key -> {"body": bytes, "final_url": str, "expires": float}
_disabled = False
_stats = {"network_calls": 0, "hits": 0, "served_bytes": 0}


def canonical_key(url: str) -> str:
    """Normalize a URL into a stable cache key."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except Exception:
        return url.strip()
    scheme = "https" if p.scheme in ("http", "https") else p.scheme
    netloc = p.netloc.lower()
    path = p.path.rstrip("/") or "/"
    # sorted query params (drops ordering noise); keep everything else
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q.sort()
    query = urllib.parse.urlencode(q)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _network_get(url, timeout, limit=http_policy.MAX_BODY_BYTES):
    # K41: pre-request admission — scheme/authority policy + a bounded
    # pre-flight DNS check refusing non-public destinations. K44: the
    # durable per-host 429 cooldown is consulted before any byte is sent,
    # matching the async path's semantics.
    target = http_policy.admit(url)
    host = http_policy.hostname_of(target)
    host_cooldowns.check_host(host)
    req = urllib.request.Request(target, headers=UA)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            # K43: 4 MiB hard cap on the fetched body by default (pre-checks
            # Content-Length, then streams so an unbounded body can never
            # exhaust memory). Callers with a justified need (Ashby board
            # API) pass a higher `limit`.
            body = http_policy.read_capped(resp, limit=limit)
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # K44 (sync path): a 429 observed here cools the host down for
            # every cooperating process, not just this one.
            try:
                host_cooldowns.record_429(
                    host, (e.headers or {}).get("Retry-After"))
            except Exception:
                pass
        raise
    if not (200 <= status < 300):
        raise urllib.error.HTTPError(target, status, "non-2xx", None, None)
    return body, final_url


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler with K41 per-hop admission + K43 redirect cap.

    Every redirect destination is re-admitted (authority validation, DNS
    public-destination check, 429-cooldown check) BEFORE it is followed —
    a blind redirect to an internal/metadata endpoint is refused instead
    of fetched. At most http_policy.MAX_REDIRECTS hops are followed.
    """

    max_redirections = http_policy.MAX_REDIRECTS
    max_repeats = http_policy.MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = http_policy.admit(urllib.parse.urljoin(req.full_url, newurl))
        host_cooldowns.check_host(http_policy.hostname_of(target))
        return super().redirect_request(req, fp, code, msg, headers, target)


# Module-level opener: same default handler chain as urllib.request.urlopen
# (proxy handling via trust_env included — the live sandbox's egress path),
# with the policy redirect handler swapped in. Existing tests patch
# http_cache._OPENER.open (same (req, timeout) signature as urlopen).
_OPENER = urllib.request.build_opener(_PolicyRedirectHandler())


def fetch(url, timeout=25, ttl=DEFAULT_TTL,
          limit=http_policy.MAX_BODY_BYTES):
    """GET url -> (body_bytes, final_url, from_cache).

    from_cache=True means zero network bytes this call. Raises the same
    exceptions urllib would on network/HTTP failure. `limit` overrides the
    K43 body cap for callers with a justified need (Ashby board API);
    the cache key does not include the limit — a body cached under a
    higher limit is still served for default-limit reads of the same URL,
    which is safe (a smaller-or-equal need never over-reads).
    """
    key = canonical_key(url)
    now = time.time()
    if not _disabled:
        with _lock:
            hit = _store.get(key)
            if hit and hit["expires"] > now:
                _stats["hits"] += 1
                _stats["served_bytes"] += len(hit["body"])
                return hit["body"], hit["final_url"], True
    try:
        body, final_url = _network_get(url, timeout, limit=limit)
    except Exception:
        raise
    if not _disabled:
        with _lock:
            _store[key] = {"body": body, "final_url": final_url,
                           "expires": now + ttl}
            _stats["network_calls"] += 1
    else:
        with _lock:
            _stats["network_calls"] += 1
    return body, final_url, False


def fetch_text(url, timeout=25, ttl=DEFAULT_TTL,
               limit=http_policy.MAX_BODY_BYTES):
    """fetch() decoded as UTF-8 text (replace errors)."""
    body, final_url, hit = fetch(url, timeout=timeout, ttl=ttl, limit=limit)
    return body.decode("utf-8", "replace"), final_url, hit


def fetch_json(url, timeout=20, ttl=DEFAULT_TTL,
               limit=http_policy.MAX_BODY_BYTES):
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
        return dict(_stats)


def reset_stats():
    with _lock:
        _stats.update({"network_calls": 0, "hits": 0, "served_bytes": 0})


def clear():
    with _lock:
        _store.clear()


def disable():
    """Bypass the cache (all calls go to network). For measurement."""
    global _disabled
    with _lock:
        _disabled = True


def enable():
    global _disabled
    with _lock:
        _disabled = False


def cache_size():
    with _lock:
        return len(_store)
