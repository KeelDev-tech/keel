"""Shared polite HTTP fetcher for ats_discovery adapters.

Read-only GET only. 429 / Retry-After = hard stop (raise RateLimited).
Adapters receive an http_get callable; this module provides the production
implementation with descriptive UA and per-call pacing handled by the caller
(sweep.py paces between boards; adapters pace between detail fetches).
"""

import time
import urllib.request
import urllib.error

UA = "KeelPipelineDiscovery/1.0 (job-discovery research; contact: pipeline)"


class RateLimited(Exception):
    """Raised when a host rate-limits us. Callers must stop that host."""


class HttpError(Exception):
    """Non-429 transport failure (network down, DNS, timeout)."""


def http_get(url, timeout=25):
    """Return (status:int, content_type:str, body:str). Raise RateLimited on 429."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "") or ""
            return r.status, ct, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"429 from {url}")
        return e.code, "", ""
    except Exception as e:
        raise HttpError(f"{url}: {e}")


def paced_get(url, pace_seconds=1.5, timeout=25):
    """http_get with a polite sleep before the request."""
    time.sleep(pace_seconds)
    return http_get(url, timeout=timeout)
