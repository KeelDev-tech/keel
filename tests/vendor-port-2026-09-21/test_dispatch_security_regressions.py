"""Export-bound dispatch regressions, with explicitly synthetic dependencies.

These execute the complete supplied api_submit/batch modules and real security
authority. The private transport, intent, approval, and verifier dependencies
were not exported: fail-on-use stubs make that limit visible. Only the synthetic
upload function is permitted to use the loopback endpoint. This does not qualify
the private executor, browser tool, human approval store, or production transport.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.request

# PORTED 2026-09-21 from Keel_Security_Repair_2026-09-21 (vendor handoff).
# 2 vendor tests EXCLUDED (not ported) — they assert the lane-retirement policy
# (test_api_direct_retirement_sends_zero_requests,
#  test_batch_preparation_cannot_grant_browser_submission). Trent's 2026-09-21
# directive keeps the human-gated lanes; our dispatch routes via record_submission.
# Path adapted: pipeline modules load from the candidate's application-executor.
ROOT = Path(__file__).resolve().parents[3] / "job-pipeline" / "engines" / "application-executor"
_KEEL_DIR = Path(__file__).resolve().parents[3] / "keel"
sys.path.insert(0, str(_KEEL_DIR))
sys.path.insert(0, str(ROOT))
from security.actions import interceptor
from security.policy import engine


class UnboundDependency(RuntimeError):
    pass


def unbound(*args, **kwargs):
    raise UnboundDependency("private host dependency not exported")


def load_export_module(filename):
    """Import the unchanged module bodies without loading private host code."""
    modules = {}
    for name in ("ats", "greenhouse_direct", "submission_outcome", "launch_lock",
                 "submit_intent", "log_event", "answer_resolver", "approval_records",
                 "verify_retry"):
        m = types.ModuleType(name)
        m.__getattr__ = lambda name: unbound
        modules[name] = m
    modules["log_event"].log = unbound
    for name in ("GENUINE_PAT", "_TRENT_NOTE_PAT", "_RESOLVED_STATE_PAT"):
        setattr(modules["verify_retry"], name, re.compile(r"(?!x)x"))
    spec = importlib.util.spec_from_file_location(
        "export_" + Path(filename).stem, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    saved_path = list(sys.path)
    try:
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
    return module


class DispatchSecurityRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="keel-dispatch-regression-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "KEEL_SECURITY_LEDGER": str(self.base / "security.jsonl"),
            "KEEL_SAFE_MODE_FILE": str(self.base / "safe-mode"),
            "KEEL_SAFE_MODE": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        engine._reset_caches()
        self.addCleanup(engine._reset_caches)
        self.api = load_export_module("api_submit.py")
        self.batch = load_export_module("batch_staged_launches.py")
        self.batch.BATCH_DIR = str(self.base / "batches")
        self.api.FASTLANE_APPROVAL_PATH = str(self.base / "unbound-approvals.json")
        self.api.REFUSE_DEDUPE_PATH = str(self.base / "refusals.json")
        self.hits = []
        hits = self.hits

        class Recorder(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        url = "http://127.0.0.1:%d/upload" % self.server.server_address[1]

        def synthetic_upload(*args, **kwargs):
            request = urllib.request.Request(url, data=b"synthetic-only")
            with urllib.request.urlopen(request, timeout=2) as response:
                response.read()
            # Deliberately stop after the first observed forbidden effect; no
            # missing host outcome or intent component is simulated as working.
            raise UnboundDependency("synthetic upload complete; host intent unbound")

        self.api.gh.upload_file = synthetic_upload
        self.api.gh.submit = unbound
        self.resume = self.base / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 synthetic identity fixture")
        self.payload = {
            "first_name": "Synthetic", "last_name": "Example",
            "email": "fixture@example.invalid", "phone": "+15550000000",
            "location": "Fixture", "answers_attributes": {"q1": "fixture"},
        }
        # P3 (our candidate): the host-cooldown gate requires an absolute submit
        # URL with a coolable host; point at the loopback recorder so fixtures
        # reach the dispatch/fingerprint gates under test.
        _port = self.server.server_address[1]
        self.page = {"board": "fixture", "job_id": "job-1", "fingerprint": "fv1",
                     "submit_path": f"http://127.0.0.1:{_port}/jobs/1/apply"}
        self.bundle = self.api.canonical_bundle(
            role_id="role-fixture", board="fixture", job_id="job-1",
            company="Fixture", ats="greenhouse", resume_path=str(self.resume),
            payload=self.payload, questions=[], form_version="fv1")
        self.fp = self.api.bundle_fingerprint(self.bundle)
        self.leads = [{"role_id": "role-fixture", "packet_path": "unbound.json",
                       "apply_url": "https://example.invalid/job"}]

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def live(self, fingerprint=None):
        return self.api._live_submit(
            "role-fixture", "Fixture", "greenhouse", self.page,
            dict(self.payload), {}, str(self.resume), self.bundle, None, False,
            self.fp if fingerprint is None else fingerprint)


    def test_authority_must_return_explicit_allow(self):
        for value in (None, False, {}, types.SimpleNamespace(allowed=False),
                      types.SimpleNamespace(allowed=1), types.SimpleNamespace(allowed="yes")):
            with self.subTest(value=repr(value)):
                self.hits.clear()
                with patch.object(self.api, "_sec_authorize_dispatch", return_value=value):
                    result = self.live()
                self.assertEqual(result["outcome"], "refused")
                self.assertEqual(self.hits, [])

    def test_fingerprint_must_match_actual_bundle(self):
        # Synthetic helper ALLOW isolates byte-binding defense. It is not a
        # policy grant or an assertion that API dispatch is authorized.
        allow = types.SimpleNamespace(allowed=True)
        with patch.object(self.api, "_sec_authorize_dispatch", return_value=allow):
            result = self.live("0" * 32)
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("integrity_violation", result["reason"])
        self.assertEqual(self.hits, [])

    def test_malformed_bundle_refuses_before_upload(self):
        self.bundle.pop("bundle_version")
        with patch.object(self.api, "_sec_authorize_dispatch",
                          return_value=types.SimpleNamespace(allowed=True)):
            result = self.live()
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(self.hits, [])


    def test_batch_requires_explicit_allow_result(self):
        with patch.object(interceptor, "request_dispatch_authorization", return_value=None):
            batch, path = self.batch.write_batch(self.leads, 0)
        self.assertIsNone(path)
        self.assertFalse(Path(self.batch.BATCH_DIR).exists())

    def test_safe_mode_denies_with_zero_requests(self):
        with patch.dict(os.environ, {"KEEL_SAFE_MODE": "1"}):
            result = self.live()
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(self.hits, [])

    def test_authority_unavailable_fails_closed(self):
        with patch.object(self.api, "_SECURITY_AVAILABLE", False):
            result = self.live()
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(self.hits, [])

    def test_dry_run_remains_available_without_authorizing_execution(self):
        result = self.batch.write_batch(self.leads, 0, dry_run=True)
        self.assertFalse(result["fired"])
        self.assertIs(result["execution_authorized"], False)
        self.assertFalse(Path(self.batch.BATCH_DIR).exists())
        self.assertFalse((self.base / "security.jsonl").exists())

    def test_authorization_error_does_not_leak_exception_secrets(self):
        secret = "synthetic-secret-do-not-log"
        with patch.object(self.api, "_sec_authorize_dispatch", side_effect=RuntimeError(secret)):
            result = self.live()
        self.assertNotIn(secret, result["reason"])
        with patch.object(interceptor, "request_dispatch_authorization", side_effect=RuntimeError(secret)):
            batch, path = self.batch.write_batch(self.leads, 0)
        self.assertIsNone(path)
        self.assertNotIn(secret, batch["security_denial"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
