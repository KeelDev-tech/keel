"""Offline connector contract: no real accounts, applicant records or network."""
from copy import deepcopy
from contextlib import ExitStack
from datetime import timedelta
import builtins
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from keel_live.review import Principal
from keel_connector import ReadinessAdapter
from keel_connector import adapter as module
from keel_connector.synthetic import NOW, make_workspace, principal_for, rebind
from keel_flow.common import canonical, digest


class ConnectorReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.document = make_workspace(self.root)
        self.principal = principal_for(self.document)
        self.now = NOW
        self.adapter = ReadinessAdapter(workspace_id=self.document["workspace_id"], synthetic=True,
            principal_provider=lambda: self.principal, snapshot_provider=lambda: self.document,
            attachment_root=self.root, host_clock=lambda: self.now)

    def call(self, operation="list_application_blockers", arguments=b"{}"):
        raw = self.adapter.call(operation, arguments)
        self.assertIs(type(raw), bytes)
        self.assertLessEqual(len(raw), module.MAX_OUTPUT_BYTES)
        result = json.loads(raw)
        self.assertIs(result["execution_authorized"], False)
        return result

    def error(self, code, **kwargs):
        result = self.call(**kwargs)
        self.assertEqual(result, {"schema": module.SCHEMA, "ok": False,
                                  "error": {"code": code}, "execution_authorized": False})
        self.assertLess(len(json.dumps(result)), 200)

    def grant(self, *, scopes=None, actions=None, workspace=None, actor="synthetic-operator"):
        return Principal(actor_id=actor, authority_record_ref="synthetic:connector-authority",
            workspace_id=workspace or self.document["workspace_id"],
            allowed_actions={"review:read"} if actions is None else actions,
            allowed_scopes=self.principal.allowed_scopes if scopes is None else scopes)

    def test_portable_adapter_is_available_and_real_qualification_is_reused(self):
        with patch.object(module, "qualified_view", wraps=module.qualified_view) as reducer:
            result = self.call()
        reducer.assert_called_once()
        self.assertTrue(result["ok"] and result["current"] and result["synthetic"])
        self.assertEqual(result["applications"][0]["state"], "CAPTURE_TO_REVIEW_CHECKS_PASSED")
        self.assertEqual(result["applications"][0]["next_step"]["code"], "VERIFY_PREPARATION_AND_READBACK")
        self.assertEqual(result["pins"]["snapshot_sha256"], digest(self.document))
        self.assertFalse(result["uncertainty"]["source_authenticity_verified"])
        self.assertEqual(result["uncertainty"]["question_impact"], "NOT_ASSESSED")

    def test_dedicated_synthetic_demo(self):
        from keel_connector.demo import run_demo
        report = run_demo()
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["scenarios"]), 9)
        self.assertTrue(all(item["passed"] for item in report["scenarios"]))

    def test_get_one_uses_only_opaque_scope_reference(self):
        listing = self.call()
        ref = listing["applications"][0]["scope_ref"]
        result = self.call("get_application_readiness", json.dumps({"scope_ref": ref}).encode())
        self.assertEqual(result, listing)
        self.error("NOT_FOUND", operation="get_application_readiness",
                   arguments=json.dumps({"scope_ref": "f" * 64}).encode())

    def test_client_cannot_choose_host_inputs_or_extra_fields(self):
        for field in ("principal", "workspace_id", "attachment_root", "now", "role_id", "snapshot", "actor_id"):
            with self.subTest(field=field), patch.object(self.adapter, "principal_provider") as provider:
                self.error("INVALID_REQUEST", arguments=json.dumps({field: "private-canary"}).encode())
                provider.assert_not_called()

    def test_strict_bounded_request_and_operations(self):
        for payload in (b"[]", b"null", b"1", b"NaN", b"{}{}", b"\xff", b"{" + b" " * 256,
                        b'{"scope_ref":"a","scope_ref":"b"}', {}, "{}", b""):
            with self.subTest(payload=repr(payload)[:40]):
                self.error("INVALID_REQUEST", arguments=payload)
        for operation in ("submit", "approve", "request", None, {}, 7):
            with self.subTest(operation=operation):
                self.error("INVALID_REQUEST", operation=operation)
        self.assertTrue(self.call(arguments=b"{}" + b" " * 254)["ok"])
        self.error("INVALID_REQUEST", arguments=b"{}" + b" " * 255)

    def test_get_requires_exact_digest_argument(self):
        for request in ({}, {"scope_ref": "short"}, {"scope_ref": "A"*64},
                        {"scope_ref": "f"*64, "now": "canary"}, {"scope_ref": []}):
            self.error("INVALID_REQUEST", operation="get_application_readiness",
                       arguments=json.dumps(request).encode())

    def test_denied_grants_prevent_snapshot_and_projection_reads(self):
        for principal in (None, {}, self.grant(actions=set()),
                          self.grant(scopes=[], workspace="another-workspace")):
            self.principal = principal
            with patch.object(self.adapter, "snapshot_provider") as provider, patch.object(module, "qualified_view") as reducer:
                self.error("ACCESS_DENIED")
                provider.assert_not_called()
                reducer.assert_not_called()

    def test_entire_workspace_must_be_authorized_even_for_get_one(self):
        second = self.root / "two"
        second.mkdir()
        self.document = make_workspace(second, count=2)
        self.adapter.attachment_root = second
        self.principal = principal_for(self.document)
        allowed = self.call()["applications"][0]["scope_ref"]
        self.principal = self.grant(scopes=[self.principal.allowed_scopes[0]])
        with patch.object(module, "qualified_view") as reducer:
            self.error("ACCESS_DENIED", operation="get_application_readiness",
                       arguments=json.dumps({"scope_ref": allowed}).encode())
            reducer.assert_not_called()

    def test_application_identity_and_action_are_part_of_grant(self):
        scopes = [dict(scope) for scope in self.principal.allowed_scopes]
        for field, value in (("application_id", "f" * 64), ("action", "SUBMIT")):
            changed = deepcopy(scopes)
            changed[0][field] = value
            self.principal = self.grant(scopes=changed)
            with patch.object(module, "qualified_view") as reducer:
                self.error("ACCESS_DENIED")
                reducer.assert_not_called()

    def test_empty_workspace_still_requires_permission(self):
        self.document["flow"]["leads"] = []
        self.principal = self.grant(actions=set())
        self.error("ACCESS_DENIED")

    def test_principal_subclasses_are_not_host_claims(self):
        class PretendPrincipal(Principal):
            pass
        self.principal = PretendPrincipal(actor_id="synthetic", authority_record_ref="synthetic:a",
            workspace_id=self.document["workspace_id"], allowed_actions={"review:read"}, allowed_scopes=[])
        self.error("ACCESS_DENIED")

    def test_revocation_and_identity_changes_during_projection_deny_output(self):
        granted = self.principal
        for denied in (None, self.grant(actions=set()), self.grant(scopes=[]), self.grant(actor="other-operator")):
            with self.subTest(denied=type(denied).__name__), patch.object(
                    self.adapter, "principal_provider", side_effect=[granted, denied]):
                self.error("ACCESS_DENIED")
        with patch.object(self.adapter, "principal_provider", side_effect=[granted, RuntimeError("secret-canary")]):
            self.error("ACCESS_DENIED")

    def test_no_cached_success_after_revocation(self):
        self.assertTrue(self.call()["ok"])
        self.principal = self.grant(actions=set())
        self.error("ACCESS_DENIED")

    def test_get_revocation_during_snapshot_hides_known_and_unknown_scope(self):
        granted = self.principal
        known = module.scope_reference(dict(granted.allowed_scopes[0]))
        self.assertNotEqual(known, "f" * 64)
        for change in ("revoked", "changed", "provider_throws"):
            for reference in (known, "f" * 64):
                with self.subTest(change=change, known_scope=reference == known):
                    current = {"principal": granted}
                    events = []

                    def principal():
                        events.append("grant")
                        if isinstance(current["principal"], Exception):
                            raise current["principal"]
                        return current["principal"]

                    def snapshot():
                        events.append("snapshot")
                        current["principal"] = (
                            self.grant(actions=set()) if change == "revoked" else
                            self.grant(actor="synthetic-different-reader") if change == "changed" else
                            RuntimeError("synthetic-revoked-grant-canary"))
                        return self.document

                    with patch.object(self.adapter, "principal_provider", side_effect=principal) as grants, \
                         patch.object(self.adapter, "snapshot_provider", side_effect=snapshot) as snapshots:
                        result = self.call("get_application_readiness", json.dumps({"scope_ref": reference}).encode())
                    self.assertEqual(result, {"schema": module.SCHEMA, "ok": False,
                        "error": {"code": "ACCESS_DENIED"}, "execution_authorized": False})
                    self.assertEqual(grants.call_count, 2)
                    snapshots.assert_called_once_with()
                    self.assertEqual(events, ["grant", "snapshot", "grant"])

    def test_every_post_initial_grant_response_rechecks_current_authority(self):
        outcomes = {"snapshot_error": "HOST_UNAVAILABLE", "clock_error": "HOST_UNAVAILABLE",
                    "invalid_snapshot": "INVALID_SNAPSHOT", "workload": "WORKLOAD_EXCEEDED",
                    "not_found": "NOT_FOUND", "output_limit": "OUTPUT_LIMIT_EXCEEDED",
                    "evaluation_error": "EVALUATION_FAILED", "scope_denied": "ACCESS_DENIED",
                    "success": None}
        granted = self.principal
        for outcome, stable_code in outcomes.items():
            for change in ("stable", "revoked", "changed", "provider_throws"):
                with self.subTest(outcome=outcome, grant=change):
                    candidate = deepcopy(self.document)
                    if outcome == "invalid_snapshot":
                        candidate = None
                    elif outcome == "workload":
                        candidate["flow"]["leads"] *= module.MAX_REPORT_ROLES + 1
                    elif outcome == "scope_denied":
                        candidate["flow"]["leads"][0]["identity"] = "e" * 64
                    current = {"principal": granted}
                    events = []

                    def principal():
                        events.append("grant")
                        if isinstance(current["principal"], Exception):
                            raise current["principal"]
                        return current["principal"]

                    def snapshot():
                        events.append("snapshot")
                        if change == "revoked":
                            current["principal"] = self.grant(actions=set())
                        elif change == "changed":
                            current["principal"] = self.grant(actor="synthetic-different-reader")
                        elif change == "provider_throws":
                            current["principal"] = RuntimeError("synthetic-revoked-grant-canary")
                        if outcome == "snapshot_error":
                            raise RuntimeError("synthetic-host-error-canary")
                        return candidate

                    with ExitStack() as stack:
                        grants = stack.enter_context(patch.object(self.adapter, "principal_provider", side_effect=principal))
                        snapshots = stack.enter_context(patch.object(self.adapter, "snapshot_provider", side_effect=snapshot))
                        if outcome == "clock_error":
                            stack.enter_context(patch.object(self.adapter, "clock", side_effect=RuntimeError("synthetic-clock-canary")))
                        elif outcome == "output_limit":
                            stack.enter_context(patch.object(module, "MAX_OUTPUT_BYTES", 256))
                        elif outcome == "evaluation_error":
                            stack.enter_context(patch.object(module, "qualified_view", side_effect=RuntimeError("synthetic-evaluation-canary")))
                        operation = "get_application_readiness" if outcome == "not_found" else "list_application_blockers"
                        arguments = json.dumps({"scope_ref": "f" * 64}).encode() if outcome == "not_found" else b"{}"
                        result = self.call(operation, arguments)
                    code = stable_code if change == "stable" else "ACCESS_DENIED"
                    if code is None:
                        self.assertTrue(result["ok"])
                        self.assertTrue(result["applications"][0]["capture_review_checks_passed"])
                    else:
                        self.assertEqual(result, {"schema": module.SCHEMA, "ok": False,
                            "error": {"code": code}, "execution_authorized": False})
                    self.assertEqual(grants.call_count, 2)
                    snapshots.assert_called_once_with()
                    self.assertEqual(events, ["grant", "snapshot", "grant"])

    def test_request_and_initial_authorization_failures_never_read_snapshot(self):
        cases = [("invalid_operation", "submit", b"{}", self.principal, "INVALID_REQUEST", 0),
                 ("invalid_arguments", "list_application_blockers", b"[]", self.principal, "INVALID_REQUEST", 0),
                 ("denied", "list_application_blockers", b"{}", self.grant(actions=set()), "ACCESS_DENIED", 1),
                 ("missing", "list_application_blockers", b"{}", None, "ACCESS_DENIED", 1),
                 ("provider_throws", "list_application_blockers", b"{}", RuntimeError("synthetic-grant-canary"), "ACCESS_DENIED", 1)]
        for name, operation, arguments, principal, code, expected_reads in cases:
            with self.subTest(case=name):
                behavior = {"side_effect": principal} if isinstance(principal, Exception) else {"return_value": principal}
                with patch.object(self.adapter, "principal_provider", **behavior) as grants, \
                     patch.object(self.adapter, "snapshot_provider") as snapshots, \
                     patch.object(self.adapter, "clock") as clock:
                    result = self.call(operation, arguments)
                self.assertEqual(result, {"schema": module.SCHEMA, "ok": False,
                    "error": {"code": code}, "execution_authorized": False})
                self.assertEqual(grants.call_count, expected_reads)
                snapshots.assert_not_called()
                clock.assert_not_called()

    def test_host_failures_are_fixed_errors_without_fallback(self):
        self.assertTrue(self.call()["ok"])
        with patch.object(self.adapter, "snapshot_provider", side_effect=RuntimeError("secret-canary")):
            self.error("HOST_UNAVAILABLE")
        with patch.object(self.adapter, "clock", return_value=NOW.replace(tzinfo=None)):
            self.error("HOST_UNAVAILABLE")
        with patch.object(module, "qualified_view", side_effect=RuntimeError("secret-canary")):
            self.error("EVALUATION_FAILED")

    def test_stale_future_partial_and_missing_history_require_refresh(self):
        original = deepcopy(self.document)
        for scenario in ("stale", "future", "partial", "history"):
            self.document = deepcopy(original)
            self.now = NOW
            if scenario == "stale":
                self.now += timedelta(seconds=91)
            elif scenario == "future":
                self.now -= timedelta(seconds=1)
            elif scenario == "partial":
                self.document["flow"]["complete"] = False
                rebind(self.document)
            else:
                self.document["flow"]["attempt_history_complete"] = False
                rebind(self.document)
            with self.subTest(scenario=scenario):
                result = self.call()
                self.assertFalse(result["current"])
                row = result["applications"][0]
                self.assertEqual(row["state"], "UNKNOWN")
                self.assertEqual(row["next_step"], {"code": "REFRESH_COMPLETE_WORKSPACE", "responsibility": "system"})
                self.assertEqual(result["observed_at"], original["flow"]["observed_at"])

    def test_missing_sources_are_system_work_not_invented_human_questions(self):
        self.document["revision_sources"] = None
        row = self.call()["applications"][0]
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertTrue(all(source["responsibility"] == "system" for source in row["sources"]))
        self.assertEqual(row["next_step"]["responsibility"], "system")

    def test_only_observed_pending_approval_is_human_work(self):
        sources = self.document["revision_sources"]["roles"][0]["sources"]
        sources["approval"] = {"source_ref": "synthetic:pending", "source_version": "v1",
            "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
            "absence": {"kind": "HUMAN_DECISION_REQUIRED", "decision_request_ref": "synthetic:request"}}
        row = self.call()["applications"][0]
        self.assertEqual(row["next_step"], {"code": "REVIEW_EXISTING_APPROVAL_REQUEST", "responsibility": "human"})
        self.now += timedelta(seconds=61)
        row = self.call()["applications"][0]
        self.assertEqual(row["next_step"]["responsibility"], "system")

    def test_source_repair_precedes_pending_human_approval(self):
        sources = self.document["revision_sources"]["roles"][0]["sources"]
        sources["approval"] = {"source_ref": "synthetic:pending", "source_version": "v1",
            "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
            "absence": {"kind": "HUMAN_DECISION_REQUIRED", "decision_request_ref": "synthetic:request"}}
        sources["target"] = None
        row = self.call()["applications"][0]
        self.assertEqual(row["next_step"], {"code": "REFRESH_OR_REPAIR_SOURCE_EVIDENCE", "responsibility": "system"})

    def test_expired_source_wrapper_reports_its_original_freshness(self):
        wrapper = self.document["revision_sources"]["snapshot"]
        wrapper["observed_at"] = (NOW - timedelta(seconds=1)).isoformat()
        wrapper["expires_at"] = NOW.isoformat()
        result = self.call()
        self.assertFalse(result["current"])
        self.assertEqual(result["source_snapshot"]["status"], "STALE")
        self.assertEqual(result["source_snapshot"]["expires_at"], NOW.isoformat())
        self.assertEqual(result["source_snapshot"]["reason_code"], "source_expired")
        self.assertEqual(result["applications"][0]["next_step"]["code"], "REFRESH_SOURCE_EXPORT")

    def test_missing_source_wrapper_preserves_authoritative_diagnostic(self):
        del self.document["revision_sources"]["snapshot"]
        result = self.call()
        self.assertEqual(result["source_snapshot"]["status"], "MISSING")
        self.assertEqual(result["source_snapshot"]["reason_code"], "source_descriptor_missing")
        self.assertFalse(result["current"])

    def test_expired_source_and_revoked_approval_remain_blocked(self):
        sources = self.document["revision_sources"]["roles"][0]["sources"]
        sources["policy"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
        sources["approval"]["record"]["revoked"] = True
        row = self.call()["applications"][0]
        statuses = {item["component"]: item["status"] for item in row["sources"]}
        self.assertEqual(statuses["policy"], "STALE")
        self.assertEqual(statuses["approval"], "REVOKED")
        self.assertFalse(row["capture_review_checks_passed"])

    def test_changed_attachment_bytes_and_canonical_holds_cannot_pass(self):
        original = deepcopy(self.document)
        self.document["flow"]["leads"][0]["holds"] = ["consent"]
        rebind(self.document)
        row = self.call()["applications"][0]
        self.assertIn("canonical_hold_present", row["reason_codes"])
        self.assertFalse(row["capture_review_checks_passed"])
        self.document = original
        next(self.root.iterdir()).write_bytes(b"DIFFERENT SYNTHETIC BYTES")
        self.assertFalse(self.call()["applications"][0]["capture_review_checks_passed"])

    def test_malformed_source_is_bounded_diagnostic(self):
        self.document["revision_sources"]["roles"][0]["sources"]["target"] = "secret-canary"
        row = self.call()["applications"][0]
        self.assertFalse(row["capture_review_checks_passed"])
        self.assertNotIn("secret-canary", json.dumps(row))

    def test_cross_workspace_and_broken_bindings_reject(self):
        original = deepcopy(self.document)
        for key in (None, "trust", "revision_sources"):
            self.document = deepcopy(original)
            location = self.document if key is None else self.document[key]
            location["workspace_id"] = "different-workspace"
            self.error("INVALID_SNAPSHOT")
        self.document = deepcopy(original)
        self.document["assurance"]["export_sha256"] = "f"*64
        self.error("INVALID_SNAPSHOT")

    def test_snapshot_invalid_types_and_complexity_are_bounded(self):
        for value in ([], None, {"secret": float("nan")}, {"secret": object()}):
            with patch.object(self.adapter, "snapshot_provider", return_value=value):
                self.error("INVALID_SNAPSHOT")
        for value in ({"padding": "a" * (module.MAX_SNAPSHOT_BYTES + 1)},
                      {"items": [None] * module.MAX_NODES}, {"integer": 1 << 257}):
            with patch.object(self.adapter, "snapshot_provider", return_value=value):
                self.error("WORKLOAD_EXCEEDED")
        cyclic = []
        cyclic.append(cyclic)
        with patch.object(self.adapter, "snapshot_provider", return_value=cyclic):
            self.error("WORKLOAD_EXCEEDED")

    def test_exact_structural_and_byte_limits(self):
        self.assertEqual(module._freeze([None] * (module.MAX_NODES - 1)), [None] * (module.MAX_NODES - 1))
        with self.assertRaises(module._Limit):
            module._freeze([None] * module.MAX_NODES)
        nested = None
        for _ in range(module.MAX_DEPTH):
            nested = [nested]
        module._freeze(nested)
        with self.assertRaises(module._Limit):
            module._freeze([nested])
        # Quotes consume two bytes. Exact encoded boundary succeeds.
        module._freeze("a" * (module.MAX_SNAPSHOT_BYTES - 2))
        with self.assertRaises(module._Limit):
            module._freeze("a" * (module.MAX_SNAPSHOT_BYTES - 1))
        with self.assertRaises(module._Limit):
            module._freeze("é" * (module.MAX_SNAPSHOT_BYTES // 2))

    def test_31_roles_fail_without_partial_projection(self):
        self.document["flow"]["leads"] *= 31
        with patch.object(module, "qualified_view") as reducer:
            self.error("WORKLOAD_EXCEEDED")
            reducer.assert_not_called()

    def test_supported_and_headroom_complete_workspaces(self):
        for count in (module.SUPPORTED_ROLES, module.MAX_REPORT_ROLES):
            directory = self.root / str(count)
            self.document = make_workspace(directory, count=count)
            self.principal = principal_for(self.document)
            self.adapter.attachment_root = directory
            result = self.call()
            self.assertEqual(result["workspace_roles"], count)
            self.assertEqual(result["returned_roles"], count)
            self.assertTrue(all(row["capture_review_checks_passed"] for row in result["applications"]))
            ref = result["applications"][0]["scope_ref"]
            selected = self.call("get_application_readiness", json.dumps({"scope_ref": ref}).encode())
            self.assertEqual(selected["workspace_roles"], count)
            self.assertEqual(selected["returned_roles"], 1)

    def test_output_overflow_has_fixed_error(self):
        with patch.object(module, "MAX_OUTPUT_BYTES", 256):
            self.error("OUTPUT_LIMIT_EXCEEDED")

    def test_unknown_reason_and_source_status_fail_conservatively(self):
        real = module.qualified_view
        def changed(*args, **kwargs):
            value = real(*args, **kwargs)
            value["roles"][0]["reasons"] = ["secret-canary"]
            value["roles"][0]["sources"]["target"]["status"] = "secret-canary"
            return value
        with patch.object(module, "qualified_view", side_effect=changed):
            row = self.call()["applications"][0]
        self.assertEqual(row["reason_codes"], ["unclassified_blocker"])
        self.assertFalse(row["capture_review_checks_passed"])
        self.assertNotIn("secret-canary", json.dumps(row))

    def test_scope_pseudonym_binds_whole_scope(self):
        scope = dict(self.principal.allowed_scopes[0])
        initial = module.scope_reference(scope)
        for field in scope:
            changed = {**scope, field: "different"}
            self.assertNotEqual(initial, module.scope_reference(changed))

    def test_minimization_with_private_synthetic_canaries(self):
        canary = "SECRET_CANARY_9387"
        self.document["labels"] = [{"role_id": self.document["flow"]["leads"][0]["role_id"],
                                    "company": canary, "title": canary, "lane": "General"}]
        sources = self.document["revision_sources"]["roles"][0]["sources"]
        for source in sources.values():
            source["source_ref"] = "synthetic:" + canary
            source["record"]["metadata"] = {"synthetic": True, "private": canary}
        sources["answers"]["record"]["fields"]["name"] = canary
        self.document["flow"]["source_revision"] = canary
        rebind(self.document)
        result = self.call()
        rendered = json.dumps(result)
        self.assertNotIn(canary, rendered)
        for raw in (self.document["workspace_id"], self.principal.actor_id,
                    self.document["flow"]["leads"][0]["identity"], str(self.root), "synthetic.invalid"):
            self.assertNotIn(raw, rendered)
        self.assertEqual(set(result["applications"][0]), {"scope_ref", "state", "capture_review_checks_passed",
            "reason_codes", "sources", "next_step", "execution_authorized"})
        self.assertTrue(all(set(source) == {"component", "status", "reason_code", "responsibility",
            "revision_sha256", "observed_at", "expires_at"} for source in result["applications"][0]["sources"]))

    def test_no_input_or_file_mutation_and_no_outbound_model_browser_or_commands(self):
        before = canonical(self.document)
        files = {path: path.read_bytes() for path in self.root.iterdir()}
        def denied(*args, **kwargs):
            raise AssertionError("forbidden side effect")
        original_open, original_io_open, original_os_open = builtins.open, io.open, os.open
        def readonly_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                denied()
            return original_open(file, mode, *args, **kwargs)
        def readonly_io_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                denied()
            return original_io_open(file, mode, *args, **kwargs)
        def readonly_os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                denied()
            return original_os_open(path, flags, *args, **kwargs)
        with patch("builtins.open", readonly_open), patch("io.open", readonly_io_open), patch("os.open", readonly_os_open), \
             patch.object(socket, "getaddrinfo", denied), patch.object(socket.socket, "connect", denied), \
             patch.object(socket.socket, "connect_ex", denied), \
             patch.object(socket.socket, "sendto", denied), patch.object(urllib.request, "urlopen", denied), \
             patch.object(subprocess, "Popen", denied), patch("os.system", denied), \
             patch("keel_workbench.service.Workbench.run", denied), patch("keel_live.review.ReviewService.inspect", denied):
            first = self.call()
            second = self.call()
        self.assertTrue(first["ok"])
        self.assertEqual(first, second)
        self.assertEqual(canonical(self.document), before)
        self.assertEqual(files, {path: path.read_bytes() for path in self.root.iterdir()})

    def test_authorized_frozen_copy_is_used_and_report_is_as_of_clock(self):
        original = deepcopy(self.document)
        real = module.qualified_view
        def mutate_original(document, **kwargs):
            self.document["flow"]["leads"][0]["identity"] = "f"*64
            self.now += timedelta(seconds=91)
            return real(document, **kwargs)
        with patch.object(module, "qualified_view", side_effect=mutate_original):
            result = self.call()
        self.assertTrue(result["applications"][0]["capture_review_checks_passed"])
        self.assertEqual(result["pins"]["snapshot_sha256"], digest(original))
        self.assertEqual(result["evaluated_at"], NOW.isoformat())
        self.assertEqual(result["freshness_basis"], "AS_OF_EVALUATED_AT")


if __name__ == "__main__":
    unittest.main()
