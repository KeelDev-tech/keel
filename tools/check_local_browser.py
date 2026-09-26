#!/usr/bin/env python3
"""Exercise the real loopback fixture HTTP boundary, without claiming rendering.

This command starts a private ephemeral server and uses synthetic data only.
For actual Chromium rendering use local_application_fixture.py --rehearse.
"""
from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import secrets
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.local_application_fixture import create_fixture, fixture_contract


class ReceiptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_receipt = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "pre" and dict(attrs).get("id") == "keel-receipt":
            self.in_receipt = True

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_receipt = False

    def handle_data(self, data):
        if self.in_receipt:
            self.parts.append(data)


def encode_multipart(contract, bundle_hash, *, changed_attachment=False, nonce=None):
    boundary = "keel-" + secrets.token_hex(16)
    fields = {"fixture_nonce": nonce or contract["fixture_nonce"], "bundle_hash": bundle_hash}
    names = {"Full name": "fullname", "Email": "email", "Motivation": "motivation",
             "Work authorization": "work_authorization", "Confirm accuracy": "accuracy"}
    for field in contract["fields"]:
        fields[names[field["label"]]] = "yes" if field["value"] is True else field["value"]
    body = b""
    for key, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
    attachment = contract["attachment"]
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="resume"; filename="{attachment["name"]}"\r\nContent-Type: {attachment["mime_type"]}\r\n\r\n').encode()
    body += b"Changed synthetic bytes" if changed_attachment else base64.b64decode(attachment["base64"])
    body += f'\r\n--{boundary}--\r\n'.encode()
    return f"multipart/form-data; boundary={boundary}", body


def check_fixture():
    server, state = create_fixture()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    checks = {}
    try:
        contract = fixture_contract(server, state)
        connection.request("GET", "/apply")
        response = connection.getresponse()
        page = response.read().decode()
        checks["form_get"] = response.status == 200 and "Local test application" in page
        checks["fixture_nonce_visible"] = state.nonce in page
        # This deliberately tests HTTP/fixture behavior, not BrowserAdapter hashes.
        bundle_hash = hashlib.sha256(b"synthetic-local-fixture-http-check").hexdigest()
        receipts = []
        for _ in range(2):
            content_type, body = encode_multipart(contract, bundle_hash)
            connection.request("POST", "/submit", body, {"Content-Type": content_type, "Origin": contract["origin"]})
            response = connection.getresponse()
            payload = response.read().decode()
            if response.status != 200:
                raise ValueError("synthetic fixture upload failed")
            parser = ReceiptParser()
            parser.feed(payload)
            receipts.append(json.loads("".join(parser.parts)))
        expected = {field["label"]: field["value"] for field in contract["fields"]}
        raw = base64.b64decode(contract["attachment"]["base64"])
        expected["attachment"] = {"name": contract["attachment"]["name"], "size": len(raw),
                                  "sha256": hashlib.sha256(raw).hexdigest()}
        checks["received_values_and_attachment"] = receipts[0]["readback"] == expected
        checks["duplicate_receipt_stable"] = receipts[0] == receipts[1] and len(state.receipts) == 1
        for name, params in (("changed_payload_rejected", {"changed_attachment": True}),
                             ("foreign_nonce_rejected", {"nonce": "0" * 64})):
            content_type, body = encode_multipart(contract, bundle_hash, **params)
            connection.request("POST", "/submit", body, {"Content-Type": content_type, "Origin": contract["origin"]})
            response = connection.getresponse()
            response.read()
            checks[name] = response.status == 400
        content_type, body = encode_multipart(contract, bundle_hash)
        connection.request("POST", "/submit", body, {"Content-Type": content_type, "Origin": "https://foreign.invalid"})
        response = connection.getresponse()
        response.read()
        checks["foreign_origin_rejected"] = response.status == 403
        if not all(checks.values()):
            raise ValueError("local fixture check failed")
        return {"status": "LOCAL_HTTP_FIXTURE_PASS", "rendered_browser": False,
                "external_submission": False, "receipt_count": len(state.receipts), "checks": checks}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    try:
        print(json.dumps(check_fixture(), sort_keys=True))
    except Exception:
        print(json.dumps({"status": "LOCAL_HTTP_FIXTURE_FAIL", "rendered_browser": False,
                          "external_submission": False}))
        raise SystemExit(1)
