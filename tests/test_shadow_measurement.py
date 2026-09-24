import unittest
from keel_local.contracts import ContractError
from keel_local.measurement import summarize_cost, receipt_diagnostic, internal_telemetry

class MeasurementTests(unittest.TestCase):
    def event(self):
        return {"event_id":"e", "attempt_id":"a", "dimension":"http_requests", "value":2,
                "unit":"requests", "is_proxy":False, "complete":True}

    def test_meter_replays_and_failures_included(self):
        event=self.event(); result=summarize_cost(["a","failed"],[event,event])
        self.assertEqual(result["known_totals"]["http_requests"],2)
        self.assertIn("failed",result["missing_attempts_by_dimension"]["http_requests"])
        self.assertFalse(result["complete"])

    def test_proxy_taps_refused(self):
        event=self.event(); event.update(dimension="browser_taps",unit="taps",is_proxy=True)
        with self.assertRaises(ContractError): summarize_cost(["a"],[event])

    def test_partial_sample_not_complete_coverage(self):
        event=self.event(); event["complete"]=False
        self.assertIn("a",summarize_cost(["a"],[event])["missing_attempts_by_dimension"]["http_requests"])

    def test_http200_never_becomes_proof(self):
        result=receipt_diagnostic({"http_status":200,"confirmation":"Thanks!"},{})
        self.assertEqual(result["status"],"UNKNOWN"); self.assertFalse(result["may_retry"])

    def test_correlated_but_caller_constructed_receipt_not_trusted(self):
        data={"attempt_id":"a","provider":"greenhouse","employer_id":"acme","posting_id":"1",
              "request_hash":"x","provider_receipt_id":"fictional"}
        self.assertFalse(receipt_diagnostic(data,data)["may_count_submitted"])

    def test_telemetry_one_contribution_per_candidate(self):
        row={"candidate_id":"a","category":"SUPPLY_LIMITED"}
        result=internal_telemetry([row]*100)
        self.assertEqual(sum(result["counts"].values()),1)
        self.assertFalse(result["public_export_allowed"])

    def test_arbitrary_bucket_label_rejected(self):
        with self.assertRaises(ContractError): internal_telemetry([{"candidate_id":"a","category":"person@example.com"}])
