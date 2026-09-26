"""Tests for engines/application-executor/adversarial_review.py (2026-09-17).

Covers the safety contract of the adversarial review loop:
  1. PII pre-filter blocks email/phone/SSN/user-name BEFORE any API call
  2. Fail-closed: any review failure -> 'unavailable', never raises, never blocks
  3. Cost-ledger logging: one line per call with model + token counts
  4. Model escalation: default gpt-4o-mini, --escalate logs the stronger model
  5. Ship gate: FAIL blocks the ship; unavailable never blocks
  6. Verdict parsing: trailing 'VERDICT: PASS|FAIL' line; missing -> unavailable

Live API calls are never made here: tests inject a fake run_cli_fn.
"""

import json
import os
import subprocess
import sys
import unittest

ENGINES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")
sys.path.insert(0, ENGINES)  # tests now live in tests/; engines stay in engines/

import adversarial_review as ar  # noqa: E402

def fake_runner_factory(gemini_out="VERDICT: PASS",
                        gpt_text="VERDICT: PASS",
                        gpt_usage=None, rc=0, stderr=""):
    """Return a run_cli_fn stub. Records calls for assertion."""
    calls = []

    def _run(cli, args, stdin_text, timeout=180):
        calls.append({"cli": cli, "args": args, "stdin": stdin_text})
        if "gemini" in cli:
            return (rc, gemini_out, stderr)
        payload = {"text": gpt_text,
                   "usage": gpt_usage or {"prompt_tokens": 10,
                                          "completion_tokens": 5,
                                          "total_tokens": 15},
                   "model": "gpt-4o-mini"}
        return (rc, json.dumps(payload), stderr)

    _run.calls = calls
    return _run


CLEAN_CHANGE = """diff: in verify_retry.py, the P2 stale-park guard now compares
status_updated_park_ref (the park event's own ts) instead of wall-clock
status_updated, so the guard fires only on a strictly newer park event."""


class TestPIIFilter(unittest.TestCase):
    def test_email_blocked(self):
        with self.assertRaises(ar.PIIBlocked):
            ar.assert_no_pii("contact the applicant at jane.doe@example.com for review")

    def test_phone_blocked(self):
        with self.assertRaises(ar.PIIBlocked):
            ar.assert_no_pii("call (707) 486-6599 to verify")

    def test_ssn_blocked(self):
        with self.assertRaises(ar.PIIBlocked):
            ar.assert_no_pii("ssn 123-45-6789 on file")

    def test_user_name_blocked(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"KEEL_OPERATOR_NAME": "Jane Doe"}):
            with self.assertRaises(ar.PIIBlocked):
                ar.assert_no_pii("Jane Doe approved this design")
            with self.assertRaises(ar.PIIBlocked):
                ar.assert_no_pii("jane doe said the guard is fine")
            # first name alone still matches (original semantics)
            with self.assertRaises(ar.PIIBlocked):
                ar.assert_no_pii("Jane signed off")

    def test_user_name_unset_matches_nothing(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KEEL_OPERATOR_NAME", None)
            ar.assert_no_pii("Jane Doe approved this design")  # must not raise

    def test_clean_design_text_passes(self):
        ar.assert_no_pii(CLEAN_CHANGE)  # must not raise

    def test_pii_blocks_before_any_call(self):
        runner = fake_runner_factory()
        result = ar.review_change("email jane.doe@example.com in the diff",
                                  ledger_path="/tmp/ar-test-ledger-1.jsonl",
                                  run_cli_fn=runner)
        self.assertEqual(result["verdict"], "unavailable")
        self.assertEqual(result["gemini"]["reason"], "prompt blocked: contains email")
        self.assertEqual(len(runner.calls), 0,
                         "no API call may fire after a PII block")
        self.assertFalse(result["blocked"])


class TestFailClosed(unittest.TestCase):
    def test_cli_error_is_unavailable_not_raise(self):
        runner = fake_runner_factory(rc=1, stderr="boom")
        result = ar.review_change(CLEAN_CHANGE,
                                  ledger_path="/tmp/ar-test-ledger-2.jsonl",
                                  run_cli_fn=runner)
        self.assertEqual(result["verdict"], "unavailable")
        self.assertFalse(result["blocked"])

    def test_rate_limit_hard_stops(self):
        runner = fake_runner_factory(rc=1, stderr="HTTP Error 429: Too Many Requests")
        result = ar.review_change(CLEAN_CHANGE,
                                  ledger_path="/tmp/ar-test-ledger-3.jsonl",
                                  run_cli_fn=runner)
        self.assertEqual(result["verdict"], "unavailable")
        self.assertEqual(result["gemini"]["reason"], "rate-limited")
        # no retries: exactly 2 calls, one per reviewer
        self.assertEqual(len(runner.calls), 2)

    def test_empty_output_is_unavailable(self):
        runner = fake_runner_factory(gemini_out="", gpt_text="")
        result = ar.review_change(CLEAN_CHANGE,
                                  ledger_path="/tmp/ar-test-ledger-4.jsonl",
                                  run_cli_fn=runner)
        self.assertEqual(result["verdict"], "unavailable")


class TestVerdictParsing(unittest.TestCase):
    def test_trailing_pass(self):
        self.assertEqual(ar.parse_verdict("some analysis\nVERDICT: PASS\n"), "pass")

    def test_trailing_fail(self):
        self.assertEqual(ar.parse_verdict("found a hole\nVERDICT: FAIL"), "fail")

    def test_missing_verdict_is_unavailable(self):
        self.assertEqual(ar.parse_verdict("looks fine, no verdict line"), "unavailable")

    def test_fail_blocks(self):
        runner = fake_runner_factory(gemini_out="loophole found\nVERDICT: FAIL")
        result = ar.review_change(CLEAN_CHANGE,
                                  ledger_path="/tmp/ar-test-ledger-5.jsonl",
                                  run_cli_fn=runner)
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(result["blocked"])


class TestCostLedger(unittest.TestCase):
    def test_one_line_per_call_with_model_and_tokens(self):
        path = "/tmp/ar-test-ledger-6.jsonl"
        if os.path.exists(path):
            os.unlink(path)
        runner = fake_runner_factory()
        ar.review_change(CLEAN_CHANGE, ledger_path=path, run_cli_fn=runner)
        lines = [json.loads(l) for l in open(path) if l.strip()]
        self.assertEqual(len(lines), 2)
        by_provider = {l["provider"]: l for l in lines}
        self.assertEqual(by_provider["openai"]["model"], "gpt-4o-mini")
        self.assertEqual(by_provider["openai"]["total_tokens"], 15)
        self.assertEqual(by_provider["gemini"]["role"], "red-team")
        self.assertEqual(by_provider["openai"]["role"], "usecase-review")
        for l in lines:
            self.assertEqual(l["status"], "ok")
            self.assertIn("usecase", l)

    def test_escalation_logs_stronger_model(self):
        path = "/tmp/ar-test-ledger-7.jsonl"
        if os.path.exists(path):
            os.unlink(path)
        runner = fake_runner_factory()
        result = ar.review_change(CLEAN_CHANGE, escalate=True,
                                  ledger_path=path, run_cli_fn=runner)
        self.assertEqual(result["gpt_model"], ar.GPT_ESCALATED_MODEL)
        lines = [json.loads(l) for l in open(path) if l.strip()]
        gpt_line = [l for l in lines if l["provider"] == "openai"][0]
        self.assertEqual(gpt_line["model"], ar.GPT_ESCALATED_MODEL)

    def test_explicit_model_override_logged(self):
        path = "/tmp/ar-test-ledger-8.jsonl"
        if os.path.exists(path):
            os.unlink(path)
        runner = fake_runner_factory()
        result = ar.review_change(CLEAN_CHANGE, gpt_model="gpt-4o",
                                  ledger_path=path, run_cli_fn=runner)
        self.assertEqual(result["gpt_model"], "gpt-4o")


class TestShipGate(unittest.TestCase):
    def test_fail_blocks_ship(self):
        allow, reason = ar.ship_gate({"verdict": "fail",
                                      "gemini": {"provider": "gemini", "verdict": "fail",
                                                 "output": "hole"},
                                      "gpt": {"provider": "openai", "verdict": "pass"}})
        self.assertFalse(allow)
        self.assertIn("FAILED", reason)

    def test_pass_allows_ship(self):
        allow, reason = ar.ship_gate({"verdict": "pass"})
        self.assertTrue(allow)

    def test_unavailable_never_blocks(self):
        allow, reason = ar.ship_gate({"verdict": "unavailable"})
        self.assertTrue(allow)
        self.assertIn("never blocks", reason.lower() + "unavailable never blocks")


class TestCLI(unittest.TestCase):
    def test_dry_run_clean(self):
        rc = ar.main(["--brief", CLEAN_CHANGE, "--dry-run"])
        self.assertEqual(rc, 0)

    def test_dry_run_pii_refused(self):
        rc = ar.main(["--brief", "call jane.doe@example.com", "--dry-run"])
        self.assertEqual(rc, 3)

    def test_dry_run_makes_no_calls(self):
        # dry-run path must not touch any runner; assert via empty ledger
        path = "/tmp/ar-test-ledger-9.jsonl"
        if os.path.exists(path):
            os.unlink(path)
        rc = ar.main(["--brief", CLEAN_CHANGE, "--dry-run",
                      "--cost-ledger", path])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(path))

    def test_invalid_usecase_rejected(self):
        with self.assertRaises(ValueError):
            ar.review_change(CLEAN_CHANGE, usecase="nonsense")


if __name__ == "__main__":
    unittest.main()
