#!/usr/bin/env python3
"""Silent-defect sweep (2026-09-19) — http_policy.py contracts.

FIX 3a: _proxied() calls urllib.request.getproxies() but the module never
imported urllib.request -> AttributeError swallowed by
`except Exception: return False`, so the documented proxy adaptation was
silently inert (behavior depended on whether some OTHER module had
imported urllib.request first). Fixed: `import urllib.request` at module
top.

The test runs in a FRESH interpreter with no prior urllib.request import
and spies on urllib.request.getproxies through the module object the
imported http_policy itself pulled in — proving _proxied actually
consults the proxy registry rather than falling into the AttributeError
path. Result value is env-dependent, so we only assert it is a bool.

FIX 3b (comment at :17): the host-429 "already live" claim was false
(zero importers of host_cooldowns.py repo-wide); the comment now says it
is not yet wired. No behavior change, no test — noted here only.
"""

import os
import subprocess
import sys

import pytest

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import http_policy  # noqa: E402


def test_proxied_returns_bool_in_process():
    assert isinstance(http_policy._proxied("http://example.com/"), bool)


def test_proxied_consults_proxy_registry_independent_of_import_order():
    """Fresh interpreter, urllib.request NOT pre-imported: _proxied must
    reach urllib.request.getproxies (spy proves it), not the AttributeError
    fallback. Reverting FIX 3a makes this test fail (KeyError on
    sys.modules['urllib.request'])."""
    engines = os.path.join(KEEL_DIR, "engines")
    code = (
        "import sys\n"
        "assert 'urllib.request' not in sys.modules, "
        "'precondition: fresh interpreter must not have urllib.request'\n"
        f"sys.path.insert(0, {engines!r})\n"
        "import http_policy\n"
        "urllib_request = sys.modules['urllib.request']\n"
        "called = []\n"
        "orig = urllib_request.getproxies\n"
        "def spy():\n"
        "    called.append(True)\n"
        "    return orig()\n"
        "urllib_request.getproxies = spy\n"
        "result = http_policy._proxied('http://example.com/')\n"
        "assert isinstance(result, bool), repr(result)\n"
        "assert called, "
        "'getproxies never consulted -> _proxied is import-order dependent'\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert "OK" in proc.stdout
