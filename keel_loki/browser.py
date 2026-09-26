"""Actual local Playwright fixture rehearsal, never employer navigation/submission.

Only fixed reviewed selectors/scripts are executed. Model proposals are values,
not JavaScript, URLs or browser objects. Python checks are not an OS sandbox.
"""
from __future__ import annotations

import base64
import hashlib
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import threading
import time

from .actions import ActionGateway
from .forms import (FormError, bind_fixture, clone, demo_fixture, digest, expected_readback,
                    validate_readback)

# These are fixed trusted worker programs, never generated from model output.
_STATE_JS = r"""() => {
  const form = document.getElementById('fixture-form');
  const unexpected = [];
  const used = new Set();
  const fields = [...document.querySelectorAll('fieldset[data-field-id]')].map(w => {
    const cs = [...w.querySelectorAll('input,select,textarea,button')];
    cs.forEach(c => used.add(c));
    const kind = w.dataset.kind;
    const id = w.dataset.fieldId;
    const first = cs[0];
    let expectedTag = kind === 'text' ? 'TEXTAREA' : kind === 'select' ? 'SELECT' : 'INPUT';
    let expectedType = kind === 'radio' ? 'radio' : kind === 'attachment' ? 'file' : 'checkbox';
    if (!first || cs.some(c => c.tagName !== expectedTag || (expectedTag === 'INPUT' && c.type !== expectedType))) unexpected.push('type:' + id);
    if (kind !== 'radio' && cs.length !== 1) unexpected.push('count:' + id);
    if (cs.some(c => c.name !== id)) unexpected.push('name:' + id);
    if (cs.some(c => c.disabled !== w.hidden)) unexpected.push('disabled:' + id);
    if (cs.some(c => c.required !== (w.dataset.required === 'true'))) unexpected.push('required:' + id);
    if (kind === 'radio' && cs.some(c => c.parentElement.textContent.trim() !== c.value)) unexpected.push('radio_label:' + id);
    if (kind === 'select' && first && [...first.options].some(o => o.textContent !== o.value || o.disabled)) unexpected.push('option:' + id);
    let options = kind === 'select' && first ? [...first.options].map(o => o.value) : kind === 'radio' ? cs.map(c => c.value) : [];
    let value = null;
    if (!w.hidden && first) {
      if (kind === 'checkbox' || kind === 'attestation') value = first.checked;
      else if (kind === 'radio') value = cs.filter(c => c.checked).map(c => c.value)[0] ?? null;
      else if (kind === 'attachment') value = [...first.files].map(f => ({name:f.name,mime_type:f.type,size:f.size}));
      else value = first.value;
    }
    return {descriptor: {id, label: w.querySelector('.field-title')?.textContent ?? '', kind,
              required: w.dataset.required === 'true', options,
              condition: JSON.parse(w.dataset.condition), assistance: w.dataset.assistance}, active: !w.hidden, value};
  });
  for (const c of document.querySelectorAll('input,select,textarea,button')) if (!used.has(c)) unexpected.push('extra_control');
  if (!form || document.forms.length !== 1 || form.getAttribute('action') !== '#') unexpected.push('form');
  return {contract_sha256:document.body.dataset.contract,account_id:document.body.dataset.account,
          nonce:document.body.dataset.nonce,origin:location.origin,fields,unexpected,submitted:window.fixtureSubmitAttempts !== 0};
}"""
_FILE_HASH_JS = r"""async e => {
  if (e.files.length !== 1) return null;
  const f = e.files[0];
  const bytes = await f.arrayBuffer();
  const h = await crypto.subtle.digest('SHA-256', bytes);
  return {name:f.name,mime_type:f.type,size:f.size,sha256:[...new Uint8Array(h)].map(x=>x.toString(16).padStart(2,'0')).join('')};
}"""


def fixture_html(contract, nonce):
    blocks = []
    for field in contract["fields"]:
        fid, kind = field["id"], field["kind"]
        title = html.escape(field["label"])
        req = " required" if field["required"] else ""
        if kind == "text":
            controls = f'<textarea id="{fid}" name="{fid}"{req}></textarea>'
        elif kind == "select":
            controls = f'<select id="{fid}" name="{fid}"{req}>' + ''.join(
                f'<option value="{html.escape(option, quote=True)}">{html.escape(option)}</option>' for option in field["options"]) + '</select>'
        elif kind == "radio":
            controls = ''.join(f'<label><input type="radio" name="{fid}" value="{html.escape(option, quote=True)}"{req}>{html.escape(option)}</label>'
                               for option in field["options"])
        else:
            itype = "file" if kind == "attachment" else "checkbox"
            controls = f'<input id="{fid}" name="{fid}" type="{itype}"{req}>'
        condition = html.escape(json.dumps(field["condition"], separators=(",", ":")), quote=True)
        blocks.append(f'<fieldset data-field-id="{fid}" data-kind="{kind}" data-required="{str(field["required"]).lower()}" '
                      f'data-assistance="{field["assistance"]}" data-condition="{condition}">'
                      f'<legend class="field-title">{title}</legend>{controls}</fieldset>')
    # No submit button. A defensive handler records and prevents any submit event.
    script = r"""
window.fixtureSubmitAttempts = 0;
document.getElementById('fixture-form').addEventListener('submit',e=>{e.preventDefault();window.fixtureSubmitAttempts++;});
for (const s of document.querySelectorAll('select')) s.selectedIndex = -1;
function refresh() {
  const seen = {};
  for (const w of document.querySelectorAll('fieldset[data-field-id]')) {
    const condition = JSON.parse(w.dataset.condition);
    const active = condition === null || (Object.hasOwn(seen,condition.field_id) && JSON.stringify(seen[condition.field_id]) === JSON.stringify(condition.equals));
    w.hidden = !active;
    const cs = [...w.querySelectorAll('input,select,textarea')];
    for (const c of cs) {c.disabled=!active;if (!active) {if(c.type==='checkbox'||c.type==='radio') c.checked=false;else if(c.tagName==='SELECT') c.selectedIndex=-1;else c.value='';}}
    if(active) {const k=w.dataset.kind;seen[w.dataset.fieldId]=k==='radio'?cs.find(c=>c.checked)?.value:k==='checkbox'||k==='attestation'?cs[0].checked:cs[0].value;}
  }
}
document.addEventListener('input',refresh);document.addEventListener('change',refresh);refresh();
"""
    return ('<!doctype html><html><head><meta charset="utf-8"><title>Keel synthetic full form</title><link rel="icon" href="data:,"></head>'
            f'<body data-contract="{digest(contract)}" data-account="{contract["account_id"]}" data-nonce="{nonce}">'
            '<h1>Synthetic preparation fixture only</h1><form id="fixture-form" action="#">' + ''.join(blocks) +
            '</form><script>' + script + '</script></body></html>').encode()


def _check_dom(page, contract, nonce):
    state = page.evaluate(_STATE_JS)
    if (state["contract_sha256"] != digest(contract) or state["origin"] != contract["origin"] or
            state["account_id"] != contract["account_id"] or state["nonce"] != nonce or state["unexpected"] or
            state["submitted"] or digest([row["descriptor"] for row in state["fields"]]) != digest(contract["fields"])):
        raise FormError("rendered form schema, scope or control set changed")
    return state


def _observe(page, contract, nonce):
    state = _check_dom(page, contract, nonce)
    rows = []
    for row in state["fields"]:
        field = row["descriptor"]
        value = row["value"]
        if row["active"] and field["kind"] == "attachment":
            value = page.locator(f'[name="{field["id"]}"]').evaluate(_FILE_HASH_JS)
        rows.append({"field_id": field["id"], "kind": field["kind"], "active": row["active"], "value": value})
    return {"schema": "keel.loki.readback.v1", "contract_sha256": state["contract_sha256"],
            "origin": state["origin"], "account_id": state["account_id"], "fields": rows,
            "unexpected_controls": state["unexpected"], "submitted": state["submitted"]}


def _write(path, value):
    data = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _render(fixture, grant, gateway, server, nonce):
    # Imported only after the explicit render opt-in and dependency preflight.
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "playwright_import_failed", "browser_actions": 0}
    contract = fixture["contract"]
    url = contract["origin"] + "/fixture/" + nonce
    denied = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, args=["--disable-background-networking", "--disable-component-update"])
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                return {"status": "UNAVAILABLE", "reason": "chromium_unavailable", "browser_actions": 0}
            return {"status": "UNAVAILABLE", "reason": "chromium_launch_failed", "browser_actions": 0}
        context = None
        count = 0
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)
            context.set_default_timeout(5000)
            def route_request(route):
                if route.request.url == url and route.request.method == "GET" and route.request.resource_type == "document":
                    route.continue_()
                else:
                    denied.append({"method": route.request.method, "resource_type": route.request.resource_type})
                    route.abort()
            context.route("**/*", route_request)
            # This fixture requires no WebSocket path. New Playwright versions can block it explicitly.
            if hasattr(context, "route_web_socket"):
                context.route_web_socket("**/*", lambda websocket: websocket.close())
            page = context.new_page()
            page.goto(url, wait_until="load", timeout=10000)
            _check_dom(page, contract, nonce)
            for proposed in grant["actions"]:
                _check_dom(page, contract, nonce)
                action = gateway.consume(grant["capability"], proposed, now=int(time.time()))
                locator = page.locator(f'[name="{action["field_id"]}"]')
                operation, value = action["operation"], action["value"]
                if operation == "set_text":
                    locator.fill(value)
                elif operation == "select_option":
                    locator.select_option(value=value)
                elif operation == "choose_radio":
                    # Value is passed as data; no dynamically compiled selector/script.
                    options = locator.all()
                    matching = [option for option in options if option.get_attribute("value") == value]
                    if len(matching) != 1:
                        raise FormError("radio option not uniquely captured")
                    matching[0].check()
                elif operation in {"set_checkbox", "set_attestation"}:
                    locator.set_checked(value)
                elif operation == "attach_file":
                    raw = base64.b64decode(fixture["attachments"][action["field_id"]], validate=True)
                    if len(raw) != value["size"] or hashlib.sha256(raw).hexdigest() != value["sha256"]:
                        raise FormError("attachment bytes changed")
                    locator.set_input_files({"name": value["name"], "mimeType": value["mime_type"], "buffer": raw})
                else:
                    raise FormError("unsupported preparation action")
                count += 1
            observed = _observe(page, contract, nonce)
            validation = validate_readback(contract, grant["plan"], observed)
            if denied or server.attempted_posts:
                raise FormError("unexpected request or submission attempt")
            return {"status": "PASS" if validation["status"] == "MATCH" else "FAIL",
                    "reason": "exact_readback" if validation["status"] == "MATCH" else "readback_mismatch",
                    "browser_actions": count, "observed": observed, "observed_sha256": digest(observed),
                    "denied_requests": len(denied), "submission_attempts": server.attempted_posts,
                    "observation_provenance": "actual_playwright"}
        except (FormError, PlaywrightError, ValueError) as exc:
            gateway.revoke(grant["capability"]["capability_id"])
            return {"status": "FAIL", "reason": type(exc).__name__, "browser_actions": count,
                    "denied_requests": len(denied), "submission_attempts": server.attempted_posts,
                    "effect_after_error": "UNVERIFIED", "automatic_retry": False}
        finally:
            if context is not None:
                context.close()
            browser.close()


def run_fixture(home, *, render=False):
    """Create a new private run directory and rehearse the frozen complete form.

    Default performs no network/browser/model calls. render=True allows only an
    ephemeral loopback HTTP fixture and actual installed Playwright/Chromium.
    No dependencies or weights are downloaded. No injected adapter can yield PASS.
    """
    if type(render) is not bool:
        raise FormError("render must be boolean")
    home = Path(home).absolute()
    if home.name in {".", ".."} or home.parent.resolve() != home.parent or not home.parent.is_dir():
        raise FormError("existing nonsymlink parent directory required")
    # Exclusive directory creation; refuse reuse. Model data never chooses paths.
    os.mkdir(home, 0o700)
    fixture = bind_fixture(demo_fixture(), now=int(time.time()))
    report = {"schema": "keel.loki.form_trial.v1", "status": "PARTIAL", "synthetic": True,
              "contract_validation": "PASS", "typed_plan_validation": "PASS", "field_count": 12,
              "active_field_count": 11, "inactive_field_count": 1,
              "real_model_calls": 0, "canonical_writes": 0, "external_http_requests": 0,
              "os_network_traffic_measured": False,
              "fixture_server_started": False, "whole_fixture_prepared": False,
              "authentic_application_validated": False, "production_deployed": False,
              "execution_authorized": False, "submission_authorized": False,
              "rendered_browser": {"status": "NOT_RUN", "browser_actions": 0},
              "limits": ["Synthetic fixture approvals are not human decisions.",
                         "Evidence hashes bind supplied content; they do not establish truth.",
                         "No arbitrary employer-site discovery or submission is implemented.",
                         "Python policy checks and Playwright routing are not OS network containment."]}
    server = None
    worker = None
    nonce = secrets.token_hex(32)
    if render:
        try:
            available = importlib.util.find_spec("playwright.sync_api") is not None
        except (ModuleNotFoundError, ValueError):
            available = False
        if not available:
            report["rendered_browser"] = {"status": "UNAVAILABLE", "reason": "playwright_unavailable", "browser_actions": 0}
        else:
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def do_GET(self):
                    if self.path != "/fixture/" + nonce:
                        self.send_error(404)
                        return
                    body = self.server.fixture_body
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def do_POST(self):
                    self.server.attempted_posts += 1
                    self.send_error(405)
            server = HTTPServer(("127.0.0.1", 0), Handler)
            server.attempted_posts = 0
            fixture["contract"]["origin"] = f"http://127.0.0.1:{server.server_port}"
            fixture = bind_fixture(fixture, now=int(time.time()))
            server.fixture_body = fixture_html(fixture["contract"], nonce)
    gateway = ActionGateway(fixture["contract"], fixture["host_snapshot"], fixture["approvals"])
    grant = gateway.issue(fixture["values"], now=fixture["now"], ttl=120)
    _write(home / "contract.json", fixture["contract"])
    _write(home / "plan.json", grant["plan"])
    report["contract_sha256"] = digest(fixture["contract"])
    report["plan_sha256"] = digest(grant["plan"])
    report["typed_action_count"] = len(grant["actions"])
    report["field_count"] = len(grant["plan"]["fields"])
    report["active_field_count"] = len(grant["actions"])
    report["inactive_field_count"] = report["field_count"] - report["active_field_count"]
    if server is not None:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        try:
            worker.start()
            report["fixture_server_started"] = True
            report["external_http_requests"] = "NOT_MEASURED_AT_OS_LEVEL"
            report["rendered_browser"] = _render(fixture, grant, gateway, server, nonce)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        report["whole_fixture_prepared"] = report["rendered_browser"]["status"] == "PASS"
        report["status"] = "PASS" if report["whole_fixture_prepared"] else "FAIL" if report["rendered_browser"]["status"] == "FAIL" else "PARTIAL"
    _write(home / "report.json", report)
    return report
