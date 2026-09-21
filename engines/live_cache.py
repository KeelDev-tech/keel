#!/usr/bin/env python3
"""live_cache.py — short-TTL cache of posting liveness verdicts.

Purpose: avoid a duplicate HTTP liveness check between verify_retry's
promotion-time verification and the claim-time re-verify gate. Cache a
"live" verdict with a 2h TTL; when the cache is fresh the claim gate skips
its HTTP round-trip.

Safety contract (fail-closed toward verification):
- Every public function never raises: any I/O or parse error is swallowed
  and treated as a cache miss, so the caller always falls back to the real
  HTTP check. A broken cache can only cost an HTTP request, never a
  skipped verification.
- Only a FRESH "live" verdict short-circuits. "dead"/"ambiguous" are
  recorded for debuggability but never short-circuit anything.
- The cached URL must match the caller's URL (normalized): a changed
  application_url is always re-checked over HTTP.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from keel_paths import DATA  # noqa: E402

CACHE = os.path.join(DATA, "hidden_files", "live_verdict_cache.json")

TTL_HOURS = 2
PRUNE_AFTER_HOURS = 24  # entries older than this are dropped on write


def _norm_url(u):
    # K22 mirror port (2026-09-18): lowercase scheme + host only. Path/query
    # case is significant — a changed application_url differing only in
    # path/query case must NOT match a cached entry, or the claim gate would
    # skip a required HTTP re-check. Fragments are dropped (never sent over
    # HTTP). Trailing-slash folding preserved from the original.
    p = urlsplit((u or "").strip())
    norm = urlunsplit((p.scheme.lower(), p.netloc.lower(),
                       p.path, p.query, ""))
    return norm.rstrip("/")


def _load():
    try:
        with open(CACHE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=PRUNE_AFTER_HOURS)).isoformat()
        pruned = {k: v for k, v in data.items()
                  if isinstance(v, dict) and v.get("ts", "") >= cutoff}
        with open(CACHE, "w") as f:
            json.dump(pruned, f)
    except Exception:
        pass  # cache write failure is never fatal


def record(role_id, url, verdict):
    """Record a liveness verdict. verdict: 'live' | 'dead' | 'ambiguous'.
    Never raises."""
    try:
        if not role_id or verdict not in ("live", "dead", "ambiguous"):
            return
        data = _load()
        data[str(role_id)] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "url": _norm_url(url),
        }
        _save(data)
    except Exception:
        pass


def fresh_live(role_id, url=None, ttl_hours=TTL_HOURS):
    """True only if a 'live' verdict for this role_id is within TTL (and the
    URL matches when one is supplied). Any doubt -> False (caller re-checks
    over HTTP). Never raises."""
    try:
        entry = _load().get(str(role_id))
        if not isinstance(entry, dict) or entry.get("verdict") != "live":
            return False
        if url is not None and _norm_url(url) != _norm_url(entry.get("url")):
            return False
        ts = datetime.fromisoformat(entry.get("ts", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # K21 mirror port (2026-09-18): age must be non-negative AND within
        # TTL. A future-dated "live" stamp (clock skew, tampered cache)
        # previously satisfied (now - ts) <= ttl and short-circuited the
        # claim-time re-verify — fail closed instead (caller re-checks).
        age = datetime.now(timezone.utc) - ts
        return timedelta(0) <= age <= timedelta(hours=ttl_hours)
    except Exception:
        return False
