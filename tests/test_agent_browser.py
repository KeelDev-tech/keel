"""Guarded unit tests; real rendered browser rehearsal is a separate command."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from keel_agent.browser import BrowserAdapter, BrowserError, contract_hash, validate_contract
from tools.local_application_fixture import FixtureState


def contract(**changes):
    value = {"mode": "local_fixture", "origin": "http://127.0.0.1:8765",
             "url": "http://127.0.0.1:8765/apply", "allowed_origins": ["http://127.0.0.1:8765"],
             "fixture_nonce": "a" * 64, "account_id": "synthetic-test-account",
             "fields": [{"label": "Full name", "value": "Synthetic Applicant"},
                        {"label": "Email", "value": "synthetic@example.invalid"},
                        {"label": "Motivation", "value": "Local test only"},
                        {"label": "Work authorization", "kind": "select", "value": "yes"},
                        {"label": "Confirm accuracy", "kind": "checkbox", "value": True}],
             "attachment": {"label": "Résumé", "name": "resume.txt", "mime_type": "text/plain",
                            "base64": base64.b64encode(b"Synthetic resume").decode()}}
    value.update(changes)
    return value


def prepared(value):
    readback = {field["label"]: field["value"] for field in value["fields"]}
    if value.get("attachment"):
        raw = base64.b64decode(value["attachment"]["base64"])
        readback["attachment"] = {"name": value["attachment"]["name"], "size": len(raw),
                                  "sha256": hashlib.sha256(raw).hexdigest()}
    return {"status": "PREPARED", "bundle_hash": contract_hash(value),
            "form_fingerprint": "f" * 64, "readback": readback, "origin": value["origin"],
            "account_id": value["account_id"], "submitted": False, "execution_authorized": False}


def multipart(fields, resume=b"Synthetic resume"):
    boundary = "keel-test-boundary"
    body = b""
    for key, value in fields:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="resume"; filename="resume.txt"\r\nContent-Type: text/plain\r\n\r\n').encode() + resume
    body += f'\r\n--{boundary}--\r\n'.encode()
    return f"multipart/form-data; boundary={boundary}", body


def fixture_fields():
    return [("fixture_nonce", "a" * 64), ("bundle_hash", "b" * 64), ("fullname", "Synthetic Applicant"),
            ("email", "synthetic@example.invalid"), ("motivation", "Local test only"),
            ("work_authorization", "yes"), ("accuracy", "yes")]


class BrowserContractTests(unittest.TestCase):
    def test_valid_contract_is_detached_and_normalized(self):
        value = contract()
        result = validate_contract(value)
        result["fields"][0]["value"] = "changed"
        self.assertEqual(value["fields"][0]["value"], "Synthetic Applicant")
        self.assertEqual(result["fields"][0]["kind"], "text")

    def test_hash_binds_fields_files_destination_account_and_nonce(self):
        original = contract_hash(contract())
        cases = [contract(account_id="different"), contract(fixture_nonce="b" * 64)]
        field = contract(); field["fields"][0]["value"] = "Changed"; cases.append(field)
        file = contract(); file["attachment"]["base64"] = base64.b64encode(b"Changed").decode(); cases.append(file)
        for value in cases:
            self.assertNotEqual(original, contract_hash(value))

    def test_hash_excludes_runtime_prepare_observation(self):
        self.assertEqual(contract_hash(contract()), contract_hash(contract(operation="submit_local", prepared={"observed_at": 42})))

    def test_fixture_targets_require_exact_loopback_path(self):
        for origin in ("http://localhost:8765", "http://127.0.0.2:8765", "http://10.0.0.1:8765", "https://127.0.0.1:8765"):
            with self.subTest(origin=origin), self.assertRaises(BrowserError):
                validate_contract(contract(origin=origin, url=origin + "/apply", allowed_origins=[origin]))
        for suffix in ("/submit", "/apply?url=external", "/apply#hash", "//evil.test/apply"):
            with self.subTest(suffix=suffix), self.assertRaises(BrowserError):
                validate_contract(contract(url="http://127.0.0.1:8765" + suffix))

    def test_allowlist_and_nonce_required(self):
        for change in ({"allowed_origins": ["*"]}, {"allowed_origins": []}, {"fixture_nonce": "short"}, {"fixture_nonce": "A" * 64}):
            with self.assertRaises(BrowserError):
                validate_contract(contract(**change))

    def test_no_credentials_or_non_https_external(self):
        for origin in ("https://user:password@example.com", "http://example.com", "https://127.0.0.1", "https://example.com:8443", "https://localhost"):
            with self.subTest(origin=origin), self.assertRaises(BrowserError):
                validate_contract(contract(mode="external_prepare", origin=origin, url=origin + "/apply", allowed_origins=[origin]))

    def test_external_submission_rejected_before_worker(self):
        value = contract(mode="external_prepare", origin="https://example.com", url="https://example.com/apply", allowed_origins=["https://example.com"])
        with patch("keel_agent.browser.subprocess.run") as run:
            with self.assertRaises(BrowserError):
                BrowserAdapter(node_path="node").submit_local(value, prepared(value))
            run.assert_not_called()

    def test_no_arbitrary_scripts_or_selectors(self):
        for change in ({"script": "steal()"}, {"operation": "eval"}, {"fields": [{"label": "Name", "value": "safe", "selector": "body"}]},
                       {"fields": [{"label": "Name", "kind": "click", "value": "safe"}]}):
            with self.assertRaises(BrowserError):
                validate_contract(contract(**change))

    def test_duplicate_fields_and_wrong_boolean_rejected(self):
        for fields in ([{"label": "Name", "value": "a"}, {"label": "Name", "value": "b"}],
                       [{"label": "Consent", "kind": "checkbox", "value": "yes"}]):
            with self.assertRaises(BrowserError):
                validate_contract(contract(fields=fields))

    def test_attachment_paths_empty_bytes_bad_encoding_rejected(self):
        for name, encoded in (("../resume.txt", "YWJj"), ("resume.txt", ""), ("resume.txt", "YWJj!"), ("resume.txt", "YQ==\n")):
            value = contract(); value["attachment"].update(name=name, base64=encoded)
            with self.assertRaises(BrowserError):
                validate_contract(value)

    def test_prepare_uses_fixed_worker_no_shell_no_proxy_or_secret_environment(self):
        value = contract()
        response = subprocess.CompletedProcess([], 0, json.dumps(prepared(value)), "")
        with patch.dict(os.environ, {"HTTPS_PROXY": "secret-proxy", "PROVIDER_API_KEY": "secret-key"}), patch("keel_agent.browser.subprocess.run", return_value=response) as run:
            result = BrowserAdapter(node_path="/trusted/node", playwright_module="/trusted/playwright").prepare(value)
        self.assertEqual(result["status"], "PREPARED")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/trusted/node")
        self.assertEqual(Path(argv[1]).name, "browser_worker.mjs")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertNotIn("HTTPS_PROXY", run.call_args.kwargs["env"])
        self.assertNotIn("PROVIDER_API_KEY", run.call_args.kwargs["env"])

    def test_old_preparation_cannot_submit_changed_fields(self):
        value = contract(); old = prepared(value); value["fields"][0]["value"] = "changed"
        with patch("keel_agent.browser.subprocess.run") as run, self.assertRaisesRegex(BrowserError, "exact bundle"):
            BrowserAdapter(node_path="node").submit_local(value, old)
        run.assert_not_called()

    def test_worker_failure_timeout_and_bad_json_are_unknown(self):
        value = contract()
        for response in (subprocess.CompletedProcess([], 1, '{"status":"UNVERIFIED","error":"missing browser"}', ""),
                         subprocess.CompletedProcess([], 0, 'not json', "")):
            with patch("keel_agent.browser.subprocess.run", return_value=response), self.assertRaises(BrowserError):
                BrowserAdapter(node_path="node").prepare(value)
        with patch("keel_agent.browser.subprocess.run", side_effect=subprocess.TimeoutExpired("node", 1)), self.assertRaisesRegex(BrowserError, "unverified"):
            BrowserAdapter(node_path="node").prepare(value)

    def test_output_identity_or_submission_mismatch_rejected(self):
        for change in ({"account_id": "wrong"}, {"origin": "https://evil.invalid"}, {"bundle_hash": "0" * 64},
                       {"submitted": True}, {"execution_authorized": True}, {"readback": {}}, {"form_fingerprint": "invalid"}):
            response = subprocess.CompletedProcess([], 0, json.dumps({**prepared(contract()), **change}), "")
            with patch("keel_agent.browser.subprocess.run", return_value=response), self.assertRaises(BrowserError):
                BrowserAdapter(node_path="node").prepare(contract())

    def test_local_confirmation_requires_server_receipt(self):
        value = contract()
        response = subprocess.CompletedProcess([], 0, json.dumps({**prepared(value), "status": "LOCAL_FIXTURE_CONFIRMED"}), "")
        with patch("keel_agent.browser.subprocess.run", return_value=response), self.assertRaisesRegex(BrowserError, "receipt evidence"):
            BrowserAdapter(node_path="node").submit_local(value, prepared(value))


class LocalFixtureTests(unittest.TestCase):
    def test_receipt_contains_exact_received_values_and_attachment_hash(self):
        fixture = FixtureState(nonce="a" * 64)
        receipt = fixture.accept(*multipart(fixture_fields()))
        self.assertTrue(receipt["local_fixture_only"])
        self.assertEqual(receipt["readback"]["Full name"], "Synthetic Applicant")
        self.assertEqual(receipt["readback"]["attachment"]["sha256"], hashlib.sha256(b"Synthetic resume").hexdigest())

    def test_same_bundle_retry_returns_same_receipt(self):
        fixture = FixtureState(nonce="a" * 64)
        first = fixture.accept(*multipart(fixture_fields()))
        second = fixture.accept(*multipart(fixture_fields()))
        self.assertEqual(first, second)
        self.assertEqual(len(fixture.receipts), 1)

    def test_same_bundle_cannot_record_different_payload(self):
        fixture = FixtureState(nonce="a" * 64)
        fixture.accept(*multipart(fixture_fields()))
        with self.assertRaisesRegex(ValueError, "different received"):
            fixture.accept(*multipart(fixture_fields(), b"Changed attachment"))

    def test_nonce_replay_against_new_fixture_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonce mismatch"):
            FixtureState(nonce="b" * 64).accept(*multipart(fixture_fields()))

    def test_duplicate_unknown_missing_fields_rejected(self):
        for fields in (fixture_fields() + [("email", "changed@evil.invalid")], fixture_fields() + [("extra", "value")], fixture_fields()[:-1]):
            with self.assertRaises(ValueError):
                FixtureState(nonce="a" * 64).accept(*multipart(fields))

    def test_nonmultipart_or_missing_attachment_rejected(self):
        with self.assertRaises(ValueError):
            FixtureState(nonce="a" * 64).accept("application/json", b"{}")
        with self.assertRaises(ValueError):
            FixtureState(nonce="a" * 64).accept(*multipart(fixture_fields(), b""))


if __name__ == "__main__":
    unittest.main()
