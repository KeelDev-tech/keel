#!/usr/bin/env python3
"""Exercise real loopback HTTP against synthetic responders; no model inference.

Run separately from tools/run_tests.py so its no-network gate stays intact.
"""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keel_agent.models import ModelError, ReviewerConfig, loopback_transport, reviewer_config_digest, run_blind_review
from keel_workflow.reviews import ReviewStore


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.requests.append(self.path)
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.server.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://must-not-contact.invalid/chat")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.server.mode == "drip":
            try:
                for character in b"HTTP/1.1 200 OK\r\nX-Drip: incomplete-header-forever":
                    self.wfile.write(bytes([character]))
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        assert data["stream"] is False and len(data["messages"]) == 2 and "tools" not in data
        view = json.loads(data["messages"][1]["content"])
        verdict = {"verdict": "PASS", "covered_claim_ids": view["subject"]["required_claim_ids"], "findings": []}
        message = {"role": "assistant", "content": json.dumps(verdict)}
        if self.path == "/api/chat":
            answer = {"model": data["model"], "created_at": "synthetic-response-time", "message": message, "done": True, "done_reason": "stop"}
        elif self.path == "/v1/chat/completions":
            answer = {"model": data["model"], "id": "synthetic-response-id", "choices": [{"message": message, "finish_reason": "stop"}]}
        else:
            raise AssertionError("Unexpected local adapter path")
        encoded = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.daemon_threads = True
    server.mode, server.requests = "normal", []
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        root = "http://127.0.0.1:" + str(server.server_port)
        reviewers = [ReviewerConfig("reviewer_a", "ollama", root + "/api/chat", "synthetic-fixture:1"),
                     ReviewerConfig("reviewer_b", "llama_cpp", root + "/v1/chat/completions", "synthetic-fixture:2")]
        subject = {"required_claim_ids": ["fixture"], "task": "Synthetic contract test only",
                   "reviewer_config_sha256": {r.reviewer_id: reviewer_config_digest(r) for r in reviewers}}
        with tempfile.TemporaryDirectory(prefix="keel-local-model-http-") as directory:
            result = run_blind_review(ReviewStore(Path(directory) / "review.sqlite"), round_id="synthetic-http-1",
                subject=subject, reviewers=reviewers, expires_at=datetime.now(timezone.utc) + timedelta(seconds=120))
            assert result["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW", result
            assert not result["execution_authorized"]
            assert all(c["transport"] == "loopback_http" for c in result["calls"])
        server.mode = "redirect"
        redirected = loopback_transport(reviewers[0], {"fixture": True}, 1)
        assert redirected.status == 302 and len(server.requests) == 5
        server.mode = "drip"
        start = time.monotonic()
        try:
            loopback_transport(reviewers[0], {"fixture": True}, 0.05)
        except ModelError as exc:
            assert str(exc) == "model_deadline_exceeded", str(exc)
        else:
            raise AssertionError("slow header did not hit deadline")
        assert time.monotonic() - start < 1, "deadline watchdog did not interrupt socket"
        print(json.dumps({"status": "PASS", "fixture": "synthetic_loopback_http", "model_calls": 0,
                          "http_requests": len(server.requests), "checks": ["ollama_wire_contract", "llama_cpp_wire_contract",
                          "blind_round_persistence", "no_redirect_follow", "slow_header_deadline"],
                          "real_model_status": "REAL_MODEL_NOT_RUN", "execution_authorized": False}, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
