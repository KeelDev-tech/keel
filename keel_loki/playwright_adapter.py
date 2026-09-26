"""Optional local Playwright transport for the fixed reviewed browser lab.

No custom URL, page, executable, browser profile, script, attachment path or
credential can enter through a recipe. Dependency installation is separate.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import threading

from .browser_lab import CASES, _drive

_SNAPSHOT = """challenge => ({challenge, sequence:++window.labSequence,
 nonce:document.body.dataset.nonce, origin:location.origin,
 fields:[...document.querySelectorAll('input')].map(e=>({field_id:e.name,
 control:e.type==='file'?'attachment':e.type})).sort((a,b)=>a.field_id.localeCompare(b.field_id)),
 submitted:window.labSubmissions!==0 || document.forms.length!==1 ||
 document.getElementById('lab')?.getAttribute('action')!=='#' ||
 document.querySelectorAll('button,textarea,select,iframe').length!==0})"""
_READBACK = """async () => {
 const e=document.querySelector('input[name="resume"]');
 if(e.files.length!==1) return null;
 const f=e.files[0], bytes=await f.arrayBuffer();
 const h=await crypto.subtle.digest('SHA-256',bytes);
 return {name:document.querySelector('input[name="name"]').value,
 resume:{name:f.name,mime_type:f.type,size:f.size,
 sha256:[...new Uint8Array(h)].map(x=>x.toString(16).padStart(2,'0')).join('')}}"""


@contextmanager
def _server(case, nonce):
    template = (Path(__file__).parent / "fixtures" / "browser_lab.html").read_text()
    layout = ""
    if case == "changed_layout":
        layout = "document.getElementById('lab').prepend(document.getElementById('resume-wrapper'));"
    elif case == "changed_schema":
        layout = "document.querySelector('input[name=name]').type='password';"
    body = template.replace("__NONCE__", nonce).replace("__LAYOUT__", layout).encode()
    path = "/fixture/" + nonce
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path != path:
                self.send_error(404)
                return
            if case == "rate_limit":
                self.send_response(429)
                self.send_header("Retry-After", "3600")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if case == "redirect":
                self.send_response(302)
                self.send_header("Location", "https://browser-lab.invalid/forbidden")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            self.server.submission_attempts += 1
            self.send_error(405)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.submission_attempts = 0
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}", path
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class _Transport:
    def __init__(self, context, server, url, case):
        self.context, self.server, self.url, self.case = context, server, url, case
        self.denied_requests = 0
        self.first_snapshot = None
        self.page = context.new_page()
        def route(request):
            if (request.request.url == url and request.request.method == "GET" and
                    request.request.resource_type == "document" and request.request.is_navigation_request()):
                request.continue_()
            else:
                self.denied_requests += 1
                request.abort()
        context.route("**/*", route)
        if hasattr(context, "route_web_socket"):
            context.route_web_socket("**/*", lambda websocket: websocket.close())

    @property
    def submission_attempts(self):
        return self.server.submission_attempts

    def open(self):
        try:
            response = self.page.goto(self.url, wait_until="load", timeout=10000)
        except Exception:
            if self.denied_requests:
                return 302
            raise
        return response.status if response else 0

    def snapshot(self, challenge):
        if self.case == "stale_snapshot" and self.first_snapshot is not None:
            return self.first_snapshot
        result = self.page.evaluate(_SNAPSHOT, challenge)
        self.first_snapshot = result
        return result

    def apply(self, field, value, encoded):
        if field == "name":
            self.page.get_by_role("textbox", name="Synthetic name", exact=True).fill(value)
            if self.case == "disconnect":
                self.context.close()
        elif field == "resume":
            self.page.get_by_label("Synthetic attachment", exact=True).set_input_files({
                "name": value["name"], "mimeType": value["mime_type"],
                "buffer": base64.b64decode(encoded, validate=True)})
        else:
            raise ValueError("unknown_fixture_field")

    def readback(self):
        return self.page.evaluate(_READBACK)


def run_cases(recipe):
    """Actually launches installed Chromium. No fallback can claim rendered PASS."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [{"case_id": case, "status": "UNAVAILABLE", "reason": "playwright_not_installed",
                 "browser_started": False, "action_attempts": 0} for case in CASES]
    rows = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True, args=["--disable-background-networking", "--disable-component-update"])
            except Exception:
                return [{"case_id": case, "status": "UNAVAILABLE", "reason": "chromium_not_launchable",
                         "browser_started": False, "action_attempts": 0} for case in CASES]
            try:
                for case in CASES:
                    nonce = secrets.token_hex(32)
                    try:
                        with _server(case, nonce) as (server, origin, path):
                            context = browser.new_context(service_workers="block", accept_downloads=False)
                            context.set_default_timeout(3000)
                            try:
                                transport = _Transport(context, server, origin + path, case)
                                row = _drive(transport, recipe, nonce=nonce, origin=origin)
                                rows.append({"case_id": case, "browser_started": True,
                                             "observation_provenance": "actual_local_playwright",
                                             "fault_injection": case if case not in {"baseline", "changed_layout"} else None,
                                             "denied_requests": transport.denied_requests,
                                             "submission_attempts": server.submission_attempts, **row})
                            finally:
                                try:
                                    context.close()
                                except Exception:
                                    pass  # disconnected fixture has already closed it
                    except OSError:
                        rows.append({"case_id": case, "status": "UNAVAILABLE", "reason": "loopback_unavailable",
                                     "browser_started": False, "action_attempts": 0})
            finally:
                browser.close()
    except Exception:
        # A partial trial cannot be promoted; never replace partial evidence
        # with a claimed full-corpus PASS or retry a failed case.
        rows.append({"case_id": "transport_failure", "status": "UNAVAILABLE",
                     "reason": "playwright_runtime_unavailable", "browser_started": False, "action_attempts": 0})
    return rows
