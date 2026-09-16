#!/usr/bin/env python3
"""page_capture.py — discovery-time listing-page capture.

Advisory fold-in (2026-09-16, discovery-scale arm): the URL-less arm found
winebusiness.com listings flip between HTTP 200 and Sucuri 307 bot-blocks
from datacenter IPs (urlless-2026-09-16.json: listings 312787/313382/313303/
314008 all 307'd via curl while 313979/313943/313948 returned 200), making
HTTP-only re-verification flaky for that source.

Contract: sweep arms capturing a listing page at discovery time save the raw
HTML here and attach the returned manifest to the staged entry as
`discovery_page_capture`. verify_retry (or any later reader) can then treat
the captured page as liveness evidence instead of re-fetching a flaky host.
Fail-closed: fetch failures (Sucuri 307, timeouts, blocks) return
{"captured": False, "reason": ...} — never raise into the sweep.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from keel_paths import HOME  # noqa: E402
CAPTURE_DIR = os.path.join(HOME, "hidden_files", "page-captures")
MANIFEST = os.path.join(CAPTURE_DIR, "manifest.jsonl")
PDT = ZoneInfo("America/Los_Angeles")

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0 Safari/537.36")}


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
        body = html.encode("utf-8") if isinstance(html, str) else bytes(html)
        status = "provided"
    else:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status
                body = r.read()
        except urllib.error.HTTPError as e:
            reason = f"http_{e.code}"
        except Exception as e:
            reason = f"{type(e).__name__}:{str(e)[:80]}"
    if body is None:
        return {"captured": False, "url": url, "reason": reason,
                "captured_at": pdt_stamp()}
    digest = hashlib.sha256(body).hexdigest()
    path = os.path.join(CAPTURE_DIR, f"{digest}.html")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(body)
        os.chmod(path, 0o600)
    manifest = {"captured": True, "url": url,
                "capture_path": path, "sha256": digest,
                "bytes": len(body), "http_status": status,
                "captured_at": pdt_stamp()}
    with open(MANIFEST, "a") as f:
        f.write(json.dumps(manifest, sort_keys=True) + "\n")
    return manifest


def load_capture(manifest):
    """Read back a captured page. Returns bytes or None."""
    try:
        path = (manifest or {}).get("capture_path", "")
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None
