#!/usr/bin/env python3
"""http_cache.py — short-TTL shared HTTP response cache.

One canonical GET path for the pipeline's HTTP traffic: form intel,
ats (resolve_final_url / board APIs), api-direct detection, and
verify_retry's sync path (which funnels through ats).

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

Measurement support:
  - http_cache.stats() -> {"network_calls", "hits", "served_bytes"}.
  - http_cache.disable()/enable() for baseline-vs-post measurement.

Fail-closed: any cache-internal exception degrades to a direct network
fetch; the caller's timeout/UA semantics are preserved.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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


def _network_get(url, timeout):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200) or 200
        body = resp.read()
        final_url = resp.geturl()
    if not (200 <= status < 300):
        raise urllib.error.HTTPError(url, status, "non-2xx", None, None)
    return body, final_url


def fetch(url, timeout=25, ttl=DEFAULT_TTL):
    """GET url -> (body_bytes, final_url, from_cache).

    from_cache=True means zero network bytes this call. Raises the same
    exceptions urllib would on network/HTTP failure.
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
        body, final_url = _network_get(url, timeout)
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


def fetch_text(url, timeout=25, ttl=DEFAULT_TTL):
    """fetch() decoded as UTF-8 text (replace errors)."""
    body, final_url, hit = fetch(url, timeout=timeout, ttl=ttl)
    return body.decode("utf-8", "replace"), final_url, hit


def fetch_json(url, timeout=20, ttl=DEFAULT_TTL):
    """fetch() parsed as JSON."""
    body, final_url, hit = fetch(url, timeout=timeout, ttl=ttl)
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
