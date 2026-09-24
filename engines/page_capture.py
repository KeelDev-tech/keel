#!/usr/bin/env python3
"""page_capture.py — discovery-time listing-page capture.

Advisory fold-in (2026-09-16, discovery-scale arm): the URL-less arm found
winebusiness.com listings flip between HTTP 200 and Sucuri 307 bot-blocks
from datacenter IPs (urlless-2026-09-16.json: listings 312787/313382/313303/
314008 all 307'd via curl while 313979/313943/313948 returned 200), making
HTTP-only re-verification flaky for that source.

Contract: sweep arms capturing a listing page at discovery time save the raw
HTML here and attach the returned manifest to the staged entry as
`discovery_page_capture`. verify_retry (or any later reader) can inspect
the captured page as advisory history. It never proves current posting liveness.
Fail-closed: fetch failures (Sucuri 307, timeouts, blocks) return
{"captured": False, "reason": ...} — never raise into the sweep.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from safe_http import urlopen as safe_urlopen
from safe_io import atomic_bytes, append_jsonl
from keel_paths import HOME  # noqa: E402
CAPTURE_DIR = os.path.join(HOME, "hidden_files", "page-captures")
MANIFEST = os.path.join(CAPTURE_DIR, "manifest.jsonl")
PDT = ZoneInfo("America/Los_Angeles")

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0 Safari/537.36")}

# K23 mirror port (2026-09-18): upper bound on a capture read, adapted from
# the review candidate's safe_http.MAX_BYTES (4 MiB) — stdlib only.
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def pdt_stamp():
    return datetime.now(PDT).strftime("%Y-%m-%d %H:%M:%S %Z")


def _ensure_dir():
    os.makedirs(CAPTURE_DIR, mode=0o700, exist_ok=True)


def capture(url, html=None, timeout=25):
    """Capture a listing page at discovery time.

    url: the listing URL. html: optional pre-fetched bytes/str (when the
    arm already has the page — avoids a second fetch against a flaky host).
    Returns a manifest dict; attach it to the staged entry as
    `discovery_page_capture`.
    """
    _ensure_dir()
    body = None
    status = None
    reason = None
    if html is not None:
        if not isinstance(html, (str, bytes, bytearray)):
            reason = 'provided capture must be text or bytes'
        elif len(html) > MAX_CAPTURE_BYTES:
            reason = 'capture exceeds byte limit'
        else:
            body = html.encode("utf-8") if isinstance(html, str) else bytes(html)
            status = "provided"
    else:
        try:
            req = urllib.request.Request(url, headers=UA)
            with safe_urlopen(req, timeout=timeout, max_bytes=MAX_CAPTURE_BYTES) as r:
                status = r.status
                body = r.read()
        except urllib.error.HTTPError as e:
            reason = f"http_{e.code}"
        except Exception as e:
            reason = f"{type(e).__name__}:{str(e)[:80]}"
    if body is not None and len(body) > MAX_CAPTURE_BYTES:
        body, reason = None, "capture exceeds byte limit"
    if body is None:
        return {"captured": False, "url": url, "reason": reason,
                "captured_at": pdt_stamp(), "usable_for_liveness": False}
    digest = hashlib.sha256(body).hexdigest()
    path = os.path.join(CAPTURE_DIR, f"{digest}.html")
    if not os.path.exists(path):
        atomic_bytes(path, body)
    manifest = {"captured": True, "usable_for_liveness": False, "url": url,
                "capture_path": path, "sha256": digest,
                "bytes": len(body), "http_status": status,
                "captured_at": pdt_stamp()}
    append_jsonl(MANIFEST, manifest)
    return manifest


def load_capture(manifest, *, max_age_hours=2, now=None):
    """Read back a captured page. Returns bytes, or None on any doubt.

    K23 mirror port (2026-09-18, adapted): fail-closed integrity checks —
    every condition must hold, otherwise None is returned (the reader falls
    back to an HTTP fetch, never to untrusted bytes):
      - manifest is a dict with captured=True
      - capture age within max_age_hours. Manifests stamp NAIVE PDT
        wall-clock strings ("2026-09-17 14:23:01 PDT"); the bound is computed
        against America/Los_Angeles, never assumed UTC. A missing or
        unparseable stamp fails closed.
      - capture_path is contained in CAPTURE_DIR (symlinks resolved —
        traversal and link escapes fail closed)
      - the file's basename is <sha256>.html for the manifest's sha256
      - bytes read are non-empty, within MAX_CAPTURE_BYTES, and match the
        manifest's byte count
      - sha256 of the bytes matches the manifest digest
    Never raises.
    """
    try:
        if not isinstance(manifest, dict) or manifest.get("captured") is not True:
            return None
        stamped = _parse_pdt_stamp(manifest.get("captured_at"))
        if stamped is None:
            return None
        ref = now if now is not None else datetime.now(PDT)
        age = ref - stamped
        if not (timedelta(0) <= age <= timedelta(hours=max_age_hours)):
            return None
        digest = str(manifest.get("sha256") or "")
        nbytes = manifest.get("bytes")
        path = _contained_path(CAPTURE_DIR, manifest["capture_path"])
        if os.path.basename(path) != digest + ".html":
            return None
        with open(path, "rb") as f:
            body = f.read(MAX_CAPTURE_BYTES + 1)
        if not body or len(body) > MAX_CAPTURE_BYTES:
            return None
        if not isinstance(nbytes, int) or len(body) != nbytes:
            return None
        if hashlib.sha256(body).hexdigest() != digest:
            return None
        return body
    except Exception:
        return None


def _contained_path(base_dir, raw):
    """stdlib-only contained_path: resolve symlinks and require containment
    in base_dir. Raises on escape."""
    base = os.path.realpath(base_dir)
    resolved = os.path.realpath(raw)
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError("path escapes capture dir")
    return resolved


def _parse_pdt_stamp(s):
    """Parse pdt_stamp() output ('2026-09-17 14:23:01 PDT') into an
    America/Los_Angeles-aware datetime. Returns None if unparseable.

    Note: strptime's %Z only recognizes the local zone's names, so the
    trailing zone-name token is stripped and the wall clock is treated as
    Los Angeles local time (ZoneInfo resolves DST per the date)."""
    if not isinstance(s, str) or not s.strip():
        return None
    text = s.strip()
    # zone-less stamp first; otherwise drop the trailing zone-name token
    # (e.g. "PDT"/"PST") and parse the bare wall clock.
    for candidate in (text, text.rsplit(" ", 1)[0]):
        try:
            naive = datetime.strptime(candidate, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        return naive.replace(tzinfo=PDT)
    return None
