import copy
from datetime import datetime, timezone
import unittest
from keel_local.contracts import ContractError
from keel_local.readiness import dependency_hash, evaluate_readiness, inventory, refill_plan, DEPENDENCIES
from keel_local.operations import *

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def lead():
    deps = {key:"fixture-"+key for key in DEPENDENCIES}
    return {"schema_version":1, "observed_at":"2026-09-17T11:59:00Z", "status":"READY",
            "posting_url":"https://example.org/jobs/1", "identity":"synthetic-1",
            "history_reconciled":True, "attempt_state":"NONE", "launch_lock_held":False,
            "policy_pass":True, "posting_verified":True, "answers_resolved":True,
            "approval_valid":True, "packet_present":True, "route":"browser", "route_supported":True,
            "dependencies":deps, "packet_dependency_hash":dependency_hash(deps),
            "approval_expires_at":"2026-09-18T00:00:00Z"}


class ReadinessTests(unittest.TestCase):
    def test_complete_observation_is_executable_shadow(self):
        self.assertTrue(evaluate_readiness(lead(), now=NOW).executable)

    def test_nominal_ready_does_not_override_holds(self):
        for state in ("UNKNOWN", "INTENT", "DISPATCHED", "SUBMITTED", None):
            row=lead(); row["attempt_state"]=state
            with self.subTest(state=state): self.assertFalse(evaluate_readiness(row, now=NOW).executable)

    def test_dependencies_all_invalidate(self):
        for key in DEPENDENCIES:
            row=lead(); row["dependencies"][key]="changed"
            with self.subTest(key=key): self.assertIn("packet_dependencies_changed", evaluate_readiness(row, now=NOW).reasons)

    def test_missing_true_and_false_evidence_fail_closed(self):
        for key in ("history_reconciled", "launch_lock_held", "policy_pass", "posting_verified", "answers_resolved",
                    "approval_valid", "packet_present", "route_supported"):
            row=lead(); row.pop(key)
            self.assertFalse(evaluate_readiness(row, now=NOW).executable)

    def test_expired_approval(self):
        row=lead(); row["approval_expires_at"]="2026-09-17T12:00:00Z"
        self.assertIn("approval_expired", evaluate_readiness(row, now=NOW).reasons)

    def test_stale_and_future_observation(self):
        for stamp in ("2026-09-17T10:00:00Z", "2026-09-17T13:00:00Z"):
            row=lead(); row["observed_at"]=stamp
            self.assertIn("observation_stale", evaluate_readiness(row, now=NOW).reasons)

    def test_api_requires_provider_contract(self):
        row=lead(); row["route"]="api"
        self.assertEqual(evaluate_readiness(row, now=NOW).state, "PROVIDER_LIMITED")

    def test_unknown_schema(self):
        row=lead(); row["schema_version"]=True
        self.assertFalse(evaluate_readiness(row, now=NOW).executable)

    def test_disjoint_inventory_and_duplicate_ids(self):
        a,b=lead(),lead(); report=inventory([a,b], now=NOW)
        self.assertEqual(report["nominal_ready"], 2)
        self.assertEqual(report["executable_ready"], 0)
        self.assertEqual(sum(report["states"].values()), 2)

    def test_wrong_status_token(self):
        row=lead(); row["status"]="NOT_READY"
        self.assertFalse(evaluate_readiness(row, now=NOW).executable)

    def test_malformed_identity_fails_closed_without_crashing_inventory(self):
        row=lead(); row["identity"]={"not":"an ID"}
        self.assertEqual(inventory([row,None],now=NOW)["executable_ready"],0)


class ControllerTests(unittest.TestCase):
    def plan(self, **kw):
        return refill_plan(**{**dict(active=True, permitted_slots=2, capacity_per_hour=12,
            p95_prepare_seconds=900, burst=2, executable=0, preparation_wip=0, verified_supply=20,
            yield_rate=.5, max_checks=30), **kw})

    def test_starved_lane_does_not_collapse_target(self):
        self.assertEqual(self.plan()["target"], 5)
        self.assertEqual(self.plan()["prepare"], 5)

    def test_subtract_wip(self):
        self.assertEqual(self.plan(preparation_wip=3)["prepare"], 2)

    def test_scheduled_pause(self):
        self.assertEqual(self.plan(active=False)["state"], "SCHEDULED_PAUSE")

    def test_no_near_zero_division(self):
        for value in (0, .000001):
            r=self.plan(yield_rate=value, verified_supply=0)
            self.assertEqual(r["state"], "SUPPLY_LIMITED")
            self.assertEqual(r["checks"], 0)
            self.assertLessEqual(r["bounded_diagnostic_checks"], 5)

    def test_budget_shortfall_explicit(self):
        r=self.plan(max_checks=1, verified_supply=0)
        self.assertEqual(r["state"], "SUPPLY_LIMITED")
        self.assertGreater(r["unfunded_promotions"], 0)

    def test_unmeasured_capacity_does_not_invent_demand_rate(self):
        r=self.plan(capacity_per_hour=0)
        self.assertEqual(r["state"], "CAPACITY_UNMEASURED")
        self.assertGreater(r["target"], 0)

    def test_invalid_config(self):
        for key,val in (("yield_rate",float("nan")),("executable",True),("permitted_slots",-1)):
            with self.assertRaises(ContractError): self.plan(**{key:val})


class OperationsTests(unittest.TestCase):
    def test_guard_never_scrapes_go(self):
        for raw in ('Traceback GO', 'GO', '{"status":"HELD","verdict":"GO"}', '}'):
            self.assertFalse(parse_guard_verdict(raw)[0])

    def test_guard_names_holder(self):
        ok,reason=parse_guard_verdict('{"status":"HELD","verdict":"STAND_DOWN","lock":{"task_id":"worker-7"}}')
        self.assertFalse(ok); self.assertIn("worker-7",reason)

    def test_guard_accepts_structured_go(self):
        self.assertTrue(parse_guard_verdict('{"status":"ACQUIRED","verdict":"GO"}')[0])

    def test_queue_shapes(self):
        for key in ("leads", "entries", "items"):
            self.assertEqual(queue_entries({key:[{"role_id":"r"}]}), [{"role_id":"r"}])
        with self.assertRaises(ContractError): queue_entries({"leads":[], "entries":[]})

    def test_buffer_orphans_and_false_inflight(self):
        queues={"q":[{"role_id":"r", "status":"NOT_IN-FLIGHT"}]}
        result=buffer_plan(queues,[{"role_id":"r"},{"role_id":"orphan"}])
        self.assertEqual([x["action"] for x in result], ["SHELVE","SHELVE"])

    def test_buffer_duplicate_and_inflight(self):
        queues={"q":[{"role_id":"r","status":"READY"}], "q2":[{"role_id":"r","status":"PARKED"}]}
        self.assertEqual(buffer_plan(queues,[{"role_id":"r"}])[0]["action"],"HOLD")
        self.assertEqual(buffer_plan({"q":[{"role_id":"r","status":"IN-FLIGHT"}]},[{"role_id":"r"}])[0]["action"],"KEEP_NO_LAUNCH")

    def test_url_aliases(self):
        for key in ("application_url", "apply_url", "posting_url"):
            self.assertEqual(posting_url({key:"https://example.org"}), "https://example.org")

    def test_scheduler_failure_vs_vanishing(self):
        expected=[{"id":"discovery", "schedule":"hourly", "enabled":True, "command_hash":"abc"}]
        self.assertEqual(scheduler_drift(expected, None)["status"],"UNVERIFIED")
        self.assertEqual(scheduler_drift(expected, [])["missing"],["discovery"])
        self.assertEqual(scheduler_drift(expected, expected)["status"],"MATCH")

    def test_scheduler_unexpected_observed_jobs(self):
        # Observed jobs absent from the expected manifest are drift, not silence.
        expected=[{"id":"discovery", "schedule":"hourly", "enabled":True, "command_hash":"abc"}]
        observed=expected+[{"id":"rogue-nightly", "schedule":"daily", "enabled":True, "command_hash":"zzz"}]
        result=scheduler_drift(expected, observed)
        self.assertEqual(result["status"],"DRIFT")
        self.assertEqual(result["unexpected"],["rogue-nightly"])
        self.assertEqual(result["missing"],[])
        self.assertEqual(result["changed"],[])

    def test_scheduler_unexpected_empty_on_match_and_unverified(self):
        expected=[{"id":"discovery", "schedule":"hourly", "enabled":True, "command_hash":"abc"}]
        self.assertEqual(scheduler_drift(expected, expected)["unexpected"],[])
        self.assertEqual(scheduler_drift(expected, None)["unexpected"],[])

    def test_keep_user_takeover_sessions(self):
        session=dict(task_id="t",state="needs_user",updated_at="2026-09-17T10:00:00Z",
                     lane_routable=True,user_takeover=True,same_day_deadline=False,attempt_state="NONE")
        self.assertEqual(parked_session_plan([session],now=NOW)[0]["action"],"KEEP")
        session["user_takeover"]=False
        self.assertEqual(parked_session_plan([session],now=NOW)[0]["action"],"PROPOSE_CLOSE")
        session["attempt_state"]="UNKNOWN"
        self.assertEqual(parked_session_plan([session],now=NOW)[0]["action"],"KEEP")

    def test_no_model_fallback_is_honest(self):
        self.assertEqual(classify_without_model("invent an excuse")["classification"],"UNCLASSIFIED")
        self.assertFalse(classify_without_model("no_packet")["model_used"])

    def test_pacific_clock_is_aware(self):
        self.assertIsNotNone(pacific_now().utcoffset())
