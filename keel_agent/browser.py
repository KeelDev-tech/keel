"""Local, bounded Playwright bridge. Model output is data, never executable code.

The only enabled submission target is the bundled loopback fixture. Public sites
are prepare-only. This module does not grant user consent or authenticate users.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
WORKER = Path(__file__).resolve().parents[1] / "tools" / "browser_worker.mjs"


class BrowserError(ValueError):
    """Rejected browser contract or failed/unverified browser operation."""


def _text(value, name, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise BrowserError(f"invalid {name}")
    return value


def validate_contract(contract):
    """Return a JSON snapshot; reject targets or actions outside this adapter."""
    if not isinstance(contract, dict):
        raise BrowserError("contract must be an object")
    if set(contract) - {"mode", "origin", "url", "account_id", "allowed_origins", "fixture_nonce",
                        "fields", "attachment", "submit_label", "operation", "prepared", "bundle_hash"}:
        raise BrowserError("unknown browser contract field")
    try:
        value = json.loads(json.dumps(contract, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise BrowserError("contract must be finite JSON") from exc
    mode = value.get("mode")
    if mode not in {"local_fixture", "external_prepare"}:
        raise BrowserError("unsupported browser mode")
    origin = _text(value.get("origin"), "origin", 2048)
    url = _text(value.get("url"), "url", 4096)
    try:
        parsed = urlsplit(origin)
        destination = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserError("invalid URL") from exc
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path or
            parsed.query or parsed.fragment or destination.username or destination.password or
            destination.fragment or destination.scheme != parsed.scheme or
            destination.netloc != parsed.netloc or "\\" in url or "\\" in origin):
        raise BrowserError("URL must match exact canonical origin without credentials")
    if value.get("allowed_origins") != [origin]:
        raise BrowserError("exact single origin allowlist is required")
    if mode == "local_fixture":
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port:
            raise BrowserError("fixture requires explicit http://127.0.0.1:PORT")
        if destination.path != "/apply" or destination.query:
            raise BrowserError("fixture URL must be /apply")
        if not re.fullmatch(r"[a-f0-9]{64}", value.get("fixture_nonce", "")):
            raise BrowserError("fixture nonce must be 256-bit lowercase hex")
    else:
        if parsed.scheme != "https" or port not in (None, 443):
            raise BrowserError("external preparation requires HTTPS port 443")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", parsed.hostname) or "." not in parsed.hostname:
                raise BrowserError("external hostname must be a public DNS name")
        else:
            raise BrowserError("external IP literals are forbidden")
        if value.get("operation") == "submit_local":
            raise BrowserError("external submission is disabled")
    _text(value.get("account_id"), "account_id", 256)
    fields = value.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
        raise BrowserError("one to 100 explicit fields required")
    labels = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"label", "kind", "value"}:
            raise BrowserError("field contract only permits label, kind, value")
        label = _text(field.get("label"), "field label", 256)
        if label in labels:
            raise BrowserError("duplicate field labels")
        labels.add(label)
        kind = field.setdefault("kind", "text")
        if kind not in {"text", "select", "checkbox"}:
            raise BrowserError("unsupported field kind")
        if kind == "checkbox":
            if not isinstance(field.get("value"), bool):
                raise BrowserError("checkbox requires boolean")
        elif not isinstance(field.get("value"), str) or len(field["value"]) > 20000 or "\x00" in field["value"]:
            raise BrowserError("invalid field value")
    attachment = value.get("attachment")
    if attachment is not None:
        if not isinstance(attachment, dict) or set(attachment) != {"label", "name", "mime_type", "base64"}:
            raise BrowserError("attachment requires label, name, mime_type, base64")
        _text(attachment["label"], "attachment label", 256)
        name = _text(attachment["name"], "attachment name", 200)
        if name in {".", ".."} or any(c in name for c in "/\\\r\n"):
            raise BrowserError("attachment name must be a basename")
        if attachment["mime_type"] not in {"text/plain", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
            raise BrowserError("unsupported attachment MIME type")
        encoded = attachment["base64"]
        if not isinstance(encoded, str) or len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4:
            raise BrowserError("attachment too large")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise BrowserError("invalid attachment encoding") from exc
        if not raw or len(raw) > MAX_ATTACHMENT_BYTES or base64.b64encode(raw).decode() != encoded:
            raise BrowserError("invalid attachment size or noncanonical encoding")
    _text(value.setdefault("submit_label", "Submit application"), "submit label", 256)
    if value.get("operation", "prepare") not in {"prepare", "submit_local"}:
        raise BrowserError("unsupported operation")
    return value


def contract_hash(contract):
    value = validate_contract(contract)
    for key in ("operation", "prepared", "bundle_hash"):
        value.pop(key, None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class BrowserAdapter:
    def __init__(self, *, node_path=None, playwright_module=None, chromium_path=None, timeout=60):
        self.node_path = node_path or shutil.which("node")
        self.playwright_module = playwright_module or os.environ.get("KEEL_PLAYWRIGHT_MODULE", "playwright")
        self.chromium_path = chromium_path or os.environ.get("KEEL_CHROMIUM_EXECUTABLE")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
            raise BrowserError("timeout must be between 1 and 120 seconds")
        self.timeout = timeout

    def prepare(self, contract):
        return self.run({**contract, "operation": "prepare"})

    def submit_local(self, contract, prepared):
        return self.run({**contract, "operation": "submit_local", "prepared": prepared})

    def run(self, contract):
        value = validate_contract(contract)
        value["bundle_hash"] = contract_hash(value)
        if value.get("operation") == "submit_local":
            prepared = value.get("prepared")
            if not isinstance(prepared, dict) or prepared.get("status") != "PREPARED" or prepared.get("bundle_hash") != value["bundle_hash"]:
                raise BrowserError("submit requires preparation for the exact bundle")
            if not re.fullmatch(r"[a-f0-9]{64}", prepared.get("form_fingerprint", "")):
                raise BrowserError("invalid prepared fingerprint")
        if not self.node_path:
            raise BrowserError("Node.js is not installed; install free local browser dependencies")
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "TEMP", "SYSTEMROOT") if key in os.environ}
        env["KEEL_PLAYWRIGHT_MODULE"] = self.playwright_module
        if self.chromium_path:
            env["KEEL_CHROMIUM_EXECUTABLE"] = str(self.chromium_path)
        # A fixed reviewed worker, list argv, no shell, no model-generated scripts.
        try:
            result = subprocess.run([str(self.node_path), str(WORKER)],
                                    input=json.dumps(value, ensure_ascii=False),
                                    capture_output=True, text=True, timeout=self.timeout,
                                    check=False, env=env, cwd=str(WORKER.parent.parent))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BrowserError("browser unavailable or timed out; outcome unverified") from exc
        if len(result.stdout.encode()) > MAX_OUTPUT_BYTES:
            raise BrowserError("browser output exceeds limit")
        try:
            output = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise BrowserError("browser returned no valid evidence") from exc
        if result.returncode or not isinstance(output, dict) or output.get("status") not in {"PREPARED", "LOCAL_FIXTURE_CONFIRMED"}:
            detail = output.get("error", "unverified browser failure") if isinstance(output, dict) else "invalid browser result"
            raise BrowserError(str(detail)[:1000])
        if output.get("bundle_hash") != value["bundle_hash"] or output.get("account_id") != value["account_id"] or output.get("origin") != value["origin"]:
            raise BrowserError("browser evidence does not match contract")
        expected_readback = {field["label"]: field["value"] for field in value["fields"]}
        if value.get("attachment"):
            attachment = value["attachment"]
            raw = base64.b64decode(attachment["base64"])
            expected_readback["attachment"] = {"name": attachment["name"], "size": len(raw),
                                                "sha256": hashlib.sha256(raw).hexdigest()}
        if output.get("readback") != expected_readback or not re.fullmatch(r"[a-f0-9]{64}", output.get("form_fingerprint", "")):
            raise BrowserError("browser field or attachment evidence mismatch")
        if output.get("execution_authorized") is not False or output.get("submitted") is not False:
            raise BrowserError("browser adapter cannot claim external execution authority")
        if value.get("operation", "prepare") == "prepare" and (output["status"] != "PREPARED" or output.get("submitted") is not False):
            raise BrowserError("preparation must not submit")
        if value.get("operation") == "submit_local" and output["status"] != "LOCAL_FIXTURE_CONFIRMED":
            raise BrowserError("local submission lacks receipt")
        if value.get("operation") == "submit_local":
            receipt = output.get("receipt")
            if (not isinstance(receipt, dict) or receipt.get("fixture_nonce") != value["fixture_nonce"]
                    or receipt.get("bundle_hash") != value["bundle_hash"] or receipt.get("account_id") != value["account_id"]
                    or receipt.get("confirmed") is not True or receipt.get("local_fixture_only") is not True
                    or receipt.get("readback") != expected_readback or not isinstance(receipt.get("receipt_id"), str)
                    or not receipt["receipt_id"] or output.get("local_fixture_submitted") is not True):
                raise BrowserError("local receipt evidence mismatch")
        return output
