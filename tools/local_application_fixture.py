#!/usr/bin/env python3
"""Private loopback application form for testing a real local browser.

This fixture never contacts an employer or writes an application outside memory.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from email import policy
from email.parser import BytesParser
import re
import secrets
from pathlib import Path
import sys
import threading
import uuid


MAX_BODY = 6 * 1024 * 1024


class FixtureState:
    def __init__(self, nonce=None, account_id="synthetic-test-account"):
        self.nonce = nonce or secrets.token_hex(32)
        if not re.fullmatch(r"[0-9a-f]{64}", self.nonce):
            raise ValueError("nonce must be 256-bit lowercase hex")
        self.account_id = account_id
        self.receipts = {}
        self.lock = threading.Lock()

    def accept(self, content_type, body):
        if not content_type.startswith("multipart/form-data;") or len(body) > MAX_BODY:
            raise ValueError("bounded multipart upload required")
        message = BytesParser(policy=policy.default).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body)
        if not message.is_multipart() or message.defects:
            raise ValueError("invalid multipart upload")
        fields = {}
        attachment = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or name in fields or (name == "resume" and attachment is not None):
                raise ValueError("missing or repeated form field")
            raw = part.get_payload(decode=True)
            if not isinstance(raw, bytes):
                raise ValueError("invalid field")
            if name == "resume":
                filename = part.get_filename()
                if not filename or len(raw) > 5 * 1024 * 1024 or not raw or any(c in filename for c in "/\\\r\n"):
                    raise ValueError("invalid attachment")
                attachment = {"name": filename, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            else:
                fields[name] = raw.decode("utf-8", errors="strict")
        if set(fields) != {"fixture_nonce", "bundle_hash", "fullname", "email", "motivation", "work_authorization", "accuracy"}:
            raise ValueError("unexpected or missing field")
        if not secrets.compare_digest(fields["fixture_nonce"], self.nonce):
            raise ValueError("fixture nonce mismatch")
        if not re.fullmatch(r"[a-f0-9]{64}", fields["bundle_hash"]):
            raise ValueError("invalid bundle digest")
        if not fields["fullname"] or not fields["motivation"] or "@" not in fields["email"]:
            raise ValueError("required form value missing")
        if fields["work_authorization"] not in {"yes", "no"} or fields["accuracy"] != "yes" or attachment is None:
            raise ValueError("required attestation or attachment missing")
        readback = {"Full name": fields["fullname"], "Email": fields["email"],
                    "Motivation": fields["motivation"], "Work authorization": fields["work_authorization"],
                    "Confirm accuracy": True, "attachment": attachment}
        receipt = {"receipt_id": "fixture-" + uuid.uuid4().hex, "fixture_nonce": self.nonce,
                   "bundle_hash": fields["bundle_hash"], "account_id": self.account_id,
                   "confirmed": True, "local_fixture_only": True, "readback": readback}
        with self.lock:
            previous = self.receipts.get(fields["bundle_hash"])
            if previous:
                if previous["readback"] != readback:
                    raise ValueError("bundle reused with different received content")
                return previous
            self.receipts[fields["bundle_hash"]] = receipt
        return receipt


def create_fixture(*, port=0, nonce=None, account_id="synthetic-test-account"):
    state = FixtureState(nonce=nonce, account_id=account_id)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log applicant values or cookies.

        def respond(self, code, body):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def correct_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.correct_host() or self.path != "/apply":
                return self.respond(404, "Not found")
            self.respond(200, f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="keel-fixture-nonce" content="{state.nonce}"><title>Keel local browser fixture</title></head>
<body><h1>Local test application</h1><p data-keel-account-id="{html.escape(state.account_id, quote=True)}">Synthetic account</p>
<form action="/submit" method="post" enctype="multipart/form-data">
<input type="hidden" name="fixture_nonce" value="{state.nonce}">
<input type="hidden" name="bundle_hash" value="">
<p><label>Full name <input name="fullname" required></label></p>
<p><label>Email <input name="email" type="email" required></label></p>
<p><label>Motivation <textarea name="motivation" required></textarea></label></p>
<p><label>Work authorization <select name="work_authorization" required><option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option></select></label></p>
<p><label>Confirm accuracy <input type="checkbox" name="accuracy" value="yes" required></label></p>
<p><label>Résumé <input type="file" name="resume" required></label></p>
<button type="submit">Submit application</button></form></body></html>''')

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.correct_host() or self.path != "/submit" or self.headers.get("Origin") not in {None, expected_origin}:
                return self.respond(403, "Fixture origin mismatch")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ValueError("body limit exceeded")
                self.connection.settimeout(10)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete upload")
                receipt = state.accept(self.headers.get("Content-Type", ""), body)
            except (ValueError, UnicodeError, TimeoutError):
                return self.respond(400, "Fixture rejected invalid application")
            self.respond(200, '<!doctype html><html><body><h1>Local test received</h1><pre id="keel-receipt">'
                         + html.escape(json.dumps(receipt)) + '</pre></body></html>')

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, state


def fixture_contract(server, state):
    origin = f"http://127.0.0.1:{server.server_port}"
    return {"mode": "local_fixture", "origin": origin, "url": origin + "/apply",
            "allowed_origins": [origin], "fixture_nonce": state.nonce, "account_id": state.account_id,
            "fields": [{"label": "Full name", "value": "Synthetic Applicant"},
                       {"label": "Email", "value": "synthetic@example.invalid"},
                       {"label": "Motivation", "value": "Local browser rehearsal only"},
                       {"label": "Work authorization", "kind": "select", "value": "yes"},
                       {"label": "Confirm accuracy", "kind": "checkbox", "value": True}],
            "attachment": {"label": "Résumé", "name": "resume.txt", "mime_type": "text/plain",
                           "base64": base64.b64encode(b"Synthetic resume for local browser rehearsal.").decode()}}


def rehearse_browser(adapter):
    server, state = create_fixture()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        contract = fixture_contract(server, state)
        preparation = adapter.prepare(contract)
        confirmation = adapter.submit_local(contract, preparation)
        repeated = adapter.submit_local(contract, preparation)
        if confirmation["receipt"]["receipt_id"] != repeated["receipt"]["receipt_id"] or len(state.receipts) != 1:
            raise ValueError("fixture duplicate protection failed")
        return {"status": "LOCAL_BROWSER_REHEARSAL_PASS", "external_submission": False,
                "rendered_form": True, "attachment_readback": True, "receipt_readback": True,
                "duplicate_receipt_stable": True, "bundle_hash": preparation["bundle_hash"],
                "form_fingerprint": preparation["form_fingerprint"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--account-id", default="synthetic-test-account")
    parser.add_argument("--rehearse", action="store_true", help="Run a real local browser preparation, submit and duplicate check")
    parser.add_argument("--node-path")
    parser.add_argument("--playwright-module")
    parser.add_argument("--chromium-path")
    args = parser.parse_args()
    if args.rehearse:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from keel_agent.browser import BrowserAdapter, BrowserError
        try:
            result = rehearse_browser(BrowserAdapter(node_path=args.node_path,
                playwright_module=args.playwright_module, chromium_path=args.chromium_path))
        except (BrowserError, ValueError) as exc:
            print(json.dumps({"status": "UNVERIFIED", "external_submission": False, "error": str(exc)}))
            raise SystemExit(1)
        print(json.dumps(result))
        return
    server, state = create_fixture(port=args.port, account_id=args.account_id)
    print(json.dumps({"origin": f"http://127.0.0.1:{server.server_port}",
                      "url": f"http://127.0.0.1:{server.server_port}/apply", "fixture_nonce": state.nonce,
                      "account_id": state.account_id}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
