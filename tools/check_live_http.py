#!/usr/bin/env python3
"""Actual loopback HTTP checks using a new, disposable synthetic source store.

This checks transport and review contracts, not real operator authentication,
upstream evidence, model inference, rendered preparation or submission. The only
client is http.client connected directly to 127.0.0.1; no proxy is consulted.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keel_agent.io import MAX_JSON_BYTES, write_private
from keel_live.server import LiveServer
from keel_live.surface import LiveSurface
from tools.make_live_demo import create_demo


def check():
    checks = []
    requests = 0
    error = None

    def verify(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    try:
        with TemporaryDirectory(prefix="keel-synthetic-live-http-") as directory:
            demo = create_demo(Path(directory) / "fixture")
            original_body = deepcopy(demo.body)
            app = LiveSurface(demo.store, demo.body, action="PREPARE",
                              principal=demo.principal, synthetic=True)
            with LiveServer(app, 0) as server:
                thread = Thread(target=server.serve_forever,
                                kwargs={"poll_interval": .05}, daemon=True)
                thread.start()

                def http(method, path, body=None, *, headers=(), auth=True,
                         host=True, auto_length=True, auto_type=True):
                    nonlocal requests
                    if isinstance(body, dict):
                        body = json.dumps(body, allow_nan=False).encode("utf-8")
                    pairs = list(headers)
                    if host:
                        pairs.insert(0, ("Host", server.authority))
                    if auth:
                        pairs.append(("Authorization", "Bearer " + server.token))
                    if body is not None and auto_length:
                        pairs.append(("Content-Length", str(len(body))))
                    if body is not None and auto_type:
                        pairs.append(("Content-Type", "application/json"))
                    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        connection.putrequest(method, path, skip_host=True,
                                              skip_accept_encoding=True)
                        for key, value in pairs:
                            connection.putheader(key, value)
                        requests += 1
                        connection.endheaders(body)
                        response = connection.getresponse()
                        status, returned = response.status, dict(response.getheaders())
                        raw = response.read()
                        value = (json.loads(raw) if returned.get("Content-Type", "").startswith(
                            "application/json") else raw)
                        return status, returned, value
                    finally:
                        connection.close()

                def get(path):
                    status, _, value = http("GET", path)
                    verify("GET " + path, status == 200)
                    return value

                def command(operation, payload):
                    return http("POST", "/api/live/v1/review/" + operation, payload)

                try:
                    verify("listener is loopback only", server.server_address[0] == "127.0.0.1")
                    status, _, health = http("GET", "/health", auth=False)
                    verify("public health contains no workspace data", status == 200 and health == {
                        "status": "ok", "service": "keel-live", "execution_authorized": False})
                    for path in ("/api/live/v1/review", "/api/live/v1/proof", "/api/v1/overview"):
                        verify("private endpoint requires token " + path,
                               http("GET", path, auth=False)[0] == 401)
                    verify("wrong host rejected", http("GET", "/health", host=False,
                        headers=[("Host", "attacker.invalid")])[0] == 403)
                    verify("duplicate host rejected", http("GET", "/health",
                        headers=[("Host", server.authority)])[0] == 403)
                    verify("wrong origin rejected", http("GET", "/api/live/v1/review",
                        headers=[("Origin", "https://attacker.invalid")])[0] == 403)
                    verify("duplicate origin rejected", http("GET", "/api/live/v1/review",
                        headers=[("Origin", "http://" + server.authority)] * 2)[0] == 403)
                    verify("duplicate authorization rejected", http("GET", "/api/live/v1/review",
                        headers=[("Authorization", "Bearer " + server.token)])[0] == 401)
                    verify("cross-site browser request rejected", http("GET", "/api/live/v1/review",
                        headers=[("Sec-Fetch-Site", "cross-site")])[0] == 403)
                    verify("query credential rejected", http("GET", "/api/live/v1/review?token=x")[0] == 400)
                    verify("CORS preflight grants no permission", http("OPTIONS", "/api/live/v1/review/request")[0] == 405)
                    for path in ("/", "/app.js", "/style.css", "/review", "/review.js", "/review.css", "/live-nav.js"):
                        status, headers, body = http("GET", path, auth=False)
                        verify("static asset securely served " + path, status == 200 and len(body) > 50
                            and headers["Cache-Control"] == "no-store"
                            and headers["X-Content-Type-Options"] == "nosniff"
                            and "frame-ancestors 'none'" in headers["Content-Security-Policy"])
                    verify("static traversal refused", http("GET", "/../WORKBENCH.md")[0] == 404)
                    path = "/api/live/v1/review/request"
                    verify("transfer encoding rejected", http("POST", path, b"{}",
                        headers=[("Transfer-Encoding", "chunked")])[0] == 400)
                    verify("missing length rejected", http("POST", path, b"{}", auto_length=False)[0] == 411)
                    verify("duplicate length rejected", http("POST", path, b"{}",
                        headers=[("Content-Length", "2")])[0] == 411)
                    verify("oversized framing rejected before read", http("POST", path, b"{}",
                        headers=[("Content-Length", str(MAX_JSON_BYTES + 1))], auto_length=False)[0] == 413)
                    verify("missing content type rejected", http("POST", path, b"{}", auto_type=False)[0] == 415)
                    verify("duplicate content type rejected", http("POST", path, b"{}",
                        headers=[("Content-Type", "application/json")])[0] == 415)
                    verify("non-JSON content type rejected", http("POST", path, b"{}",
                        headers=[("Content-Type", "text/plain")], auto_type=False)[0] == 415)
                    verify("non-UTF8 content rejected", http("POST", path, b"{}",
                        headers=[("Content-Type", "application/json; charset=latin-1")], auto_type=False)[0] == 415)
                    verify("compressed content rejected", http("POST", path, b"{}",
                        headers=[("Content-Encoding", "gzip")])[0] == 415)
                    verify("duplicate JSON keys rejected", http("POST", path, b'{"scope":{},"scope":{}}')[0] == 400)
                    verify("malformed JSON rejected", http("POST", path, b'{')[0] == 400)
                    verify("GET body rejected", http("GET", "/api/live/v1/review", b"{}")[0] == 400)
                    before = demo.store.events()
                    verify("snapshot import disabled", http("POST", "/api/v1/snapshot", {
                        "snapshot": {}, "previous_sha256": "a" * 64})[0] == 405)
                    verify("HTTP source producer unavailable", http("POST", "/api/live/v1/sources", {})[0] == 404)
                    spec = get("/openapi.json")
                    verify("OpenAPI does not advertise snapshot writes", "post" not in spec["paths"]["/api/v1/snapshot"])
                    overview = get("/api/live/v1/review")
                    context = {"scope": overview["scopes"][0], "flow_sha256": overview["flow_sha256"]}
                    status, _, shown = command("show", context)
                    verify("source inspection invents no request", status == 200
                        and shown["current_request"] is None
                        and sum("record" in source for source in shown["sources"].values()) == 6
                        and shown["sources"]["approval"] == {"absence": {"kind": "SOURCE_RECORD_MISSING"}}
                        and demo.store.events() == before)
                    request = {**context, "confirmed": True,
                        "expires_at": (demo.clock() + timedelta(minutes=5)).isoformat()}
                    verify("HTTP payload cannot spoof operator", command("request", {
                        **request, "actor_id": "synthetic-injected-operator"})[0] == 400)
                    verify("decision requires explicit confirmation", command("request", {
                        **request, "confirmed": False})[0] == 400)
                    wrong_scope = deepcopy(context["scope"])
                    wrong_scope["application_id"] = "synthetic-other-application"
                    verify("application scope mismatch rejected", command("request", {
                        **request, "scope": wrong_scope})[0] == 409)
                    verify("changed canonical digest rejected", command("request", {
                        **request, "flow_sha256": "f" * 64})[0] == 409)
                    verify("rejected requests do not write decisions", demo.store.events() == before)
                    status, _, pending = command("request", request)
                    verify("explicit synthetic request creates pending review", status == 200
                        and pending["state"] == "PENDING")
                    status, _, shown = command("show", context)
                    verify("review exposes exact pending packet", status == 200
                        and shown["current_request"]["request_id"] == pending["request_id"]
                        and shown["current_request"]["review_sha256"] == pending["review_sha256"])
                    current = shown["current_request"]
                    decision = {**context, "confirmed": True, "request_id": current["request_id"],
                        "reviewed_sha256": current["review_sha256"], "decision": "APPROVE",
                        "expires_at": (demo.clock() + timedelta(minutes=2)).isoformat()}
                    verify("wrong packet digest cannot be approved", command("decide", {
                        **decision, "reviewed_sha256": "f" * 64})[0] == 400)
                    status, _, approved = command("decide", decision)
                    verify("exact packet approval uses host principal", status == 200
                        and approved["state"] == "APPROVED" and approved["approval_currently_valid"]
                        and approved["decision"]["actor_id"] == demo.principal.actor_id
                        and approved["decision"]["authority_record_ref"] == demo.principal.authority_record_ref)
                    proof = get("/api/live/v1/proof")
                    verify("approval cannot replace absent assurance and trust", proof["state"] == "BLOCKED"
                        and not proof["roles"][0]["assurance_checks_passed"]
                        and not proof["roles"][0]["trust_checks_passed"]
                        and not proof["roles"][0]["packet_dependencies_bound"]
                        and proof["execution_authorized"] is False)
                    view = get("/api/v1/overview")
                    verify("Workbench reflects actual seventh revision without qualifying", view["roles"][0]["sources"]["approval"]["status"] == "READY"
                        and view["counts"]["review_checks_passed"] == 0)
                    status, _, revoked = command("revoke", {**context, "confirmed": True,
                        "request_id": current["request_id"], "reason": "Synthetic explicit HTTP withdrawal"})
                    verify("explicit withdrawal revokes exact request", status == 200
                        and revoked["state"] == "REVOKED" and not revoked["approval_currently_valid"])
                    before = demo.store.events()
                    demo.clock.advance(91)
                    stale = get("/api/live/v1/review")
                    verify("stale reads preserve original observation", not stale["current"]
                        and stale["observed_at"] == original_body["flow"]["observed_at"])
                    verify("stale flow prevents new review command", command("request", request)[0] == 409)
                    verify("stale flow prevents decision command", command("decide", decision)[0] == 409)
                    verify("stale refusals leave source history unchanged", demo.store.events() == before)
                    verify("canonical body remains untouched", demo.body == original_body)
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
                    verify("fixture server stopped", not thread.is_alive())
    except Exception as failure:
        # Never serialize request payloads, session tokens or private material.
        error = {"type": type(failure).__name__, "message": str(failure)
                 if isinstance(failure, AssertionError) else "fixture execution failed"}
    passed = sum(row["passed"] for row in checks)
    report = {"schema": "keel.live_http_checks.v1", "suite": "live-actual-loopback-http",
        "status": "PASS" if error is None and passed == len(checks) else "FAIL",
        "checks": checks, "checks_passed": passed, "checks_total": len(checks),
        "synthetic": True, "not_live_evidence": True, "loopback_scope": "127.0.0.1 ephemeral fixture only",
        "loopback_http_requests": requests, "external_network_requests": 0, "canonical_writes": 0,
        "model_calls": 0, "browser_actions": 0, "application_submissions": 0,
        "execution_authorized": False, "production_deployed": False,
        "source_authenticity_verified": False, "operator_authenticity_verified": False,
        "limitations": ["Every source, identity, approval and withdrawal is a synthetic fixture.",
            "The host principal is supplied by the fixture; actual human authentication was not exercised.",
            "No live source, model, rendered browser, real preparation or submission was exercised."]}
    if error is not None:
        report["error"] = error
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new private JSON evidence file")
    args = parser.parse_args(argv)
    if Path(args.out).exists() or Path(args.out).is_symlink():
        raise ValueError("new result path required")
    result = check()
    write_private(args.out, result)
    print(json.dumps({key: result[key] for key in ("status", "checks_passed", "checks_total",
                                                  "loopback_http_requests", "execution_authorized")}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
