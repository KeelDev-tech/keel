import random
import unittest
from keel_maint.contracts import ContractError
from keel_maint.monitoring import supply_health,conformance

class SupplyTests(unittest.TestCase):
    def run_health(self,ready,actionable,when="2026-09-18T00:00:10Z"):
        return supply_health({"schema_version":1,"ready":ready,"actionable":actionable,"observed_at":"2026-09-18T00:00:00Z"},evaluated_at=when)
    def test_exact_reported_regression(self):self.assertEqual(self.run_health(5,0)["status"],"BUFFER_PRESENT_SUPPLY_STARVED")
    def test_low_and_starved(self):self.assertEqual(self.run_health(4,0)["status"],"BUFFER_LOW_SUPPLY_STARVED")
    def test_low_with_supply(self):self.assertEqual(self.run_health(2,10)["status"],"BUFFER_LOW_SUPPLY_PRESENT")
    def test_present_not_whole_system_healthy(self):
        r=self.run_health(5,10);self.assertEqual(r["status"],"BUFFER_AND_SUPPLY_PRESENT");self.assertEqual(r["whole_system_health"],"NOT_ESTABLISHED")
    def test_unknown_not_zero(self):self.assertEqual(self.run_health(None,0)["status"],"UNVERIFIED")
    def test_stale(self):self.assertEqual(self.run_health(5,10,"2026-09-18T00:10:00Z")["status"],"UNVERIFIED")
    def test_future_observation(self):self.assertEqual(self.run_health(5,10,"2026-09-17T23:59:59Z")["status"],"UNVERIFIED")
    def test_bool_count_rejected(self):
        with self.assertRaises(ContractError):self.run_health(True,0)
    def test_no_side_effect_directives(self):
        r=self.run_health(5,0);self.assertEqual(r["browser_action"],"NONE");self.assertEqual(r["scheduler_changes"],0)
    def test_property_starved_never_present(self):
        for n in range(250):self.assertIn("STARVED",self.run_health(n,0)["status"])

class ConformanceTests(unittest.TestCase):
    def event(self,n,state,rev="v1"):
        return {"schema_version":1,"event_id":"e"+str(n),"attempt_id":"a1","sequence":n,"state":state,
                "evidence_revision":rev,"observed_at":"2026-09-18T00:00:00Z"}
    def test_empty_not_clean(self):self.assertEqual(conformance([])["status"],"NO_DATA")
    def test_duplicate_dedup(self):
        r=conformance([self.event(1,"HELD")]*4);self.assertEqual(r["unique_events"],1);self.assertEqual(r["duplicate_rows_ignored"],3);self.assertFalse(r["findings"])
    def test_conflicting_duplicate(self):
        with self.assertRaises(ContractError):conformance([self.event(1,"HELD"),self.event(1,"UNKNOWN")])
    def test_repeated_hold(self):
        r=conformance([self.event(n,"HELD") for n in range(3)])
        self.assertEqual(r["findings"][0]["kind"],"REPEATED_HOLD_WITH_UNCHANGED_EVIDENCE")
    def test_changed_evidence_not_same_loop(self):
        r=conformance([self.event(n,"HELD",str(n)) for n in range(3)]);self.assertFalse(r["findings"])
    def test_unknown_retry_requires_review(self):
        r=conformance([self.event(0,"UNKNOWN"),self.event(1,"QUEUED")]);self.assertIn("RECONCILIATION",r["findings"][0]["kind"])
    def test_export_never_acceptance(self):
        r=conformance([self.event(0,"COMPLETED")]);self.assertFalse(r["provider_acceptance_verified"])
    def test_sequence_gap(self):self.assertEqual(len(conformance([self.event(1,"HELD"),self.event(3,"HELD")])["sequence_gaps"]),1)
    def test_unknown_state_rejected(self):
        with self.assertRaises(ContractError):conformance([self.event(1,"FANCY_NEW_STATE")])
    def test_reordering_invariance(self):
        events=[self.event(n,"HELD") for n in range(5)];expected=conformance(events)
        random.Random(5).shuffle(events);self.assertEqual(conformance(events),expected)
