"""test_parked_sweep_telemetry_matcher.py — raw-telemetry submitted
evidence matcher (2026-09-21/22).

Covers _telemetry_submitted_by_company and _entry_telemetry_submitted:
both event aliases count, matching is on company+normalized-title (never
role_id alone), prefix tolerance for suffix-word titles, and fail-closed
on unreadable input. All fixtures synthetic.

unittest style, run with:
    python3 -m unittest tests.test_parked_sweep_telemetry_matcher
"""
import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))
import parked_task_sweep as pts  # noqa: E402


def _write_events(rows):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "events.jsonl")
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _event(company, title=None, role_id=None, etype="submitted"):
    e = {"event_type": etype, "company": company}
    det = {}
    if title:
        det["title"] = title
    if det:
        e["details"] = det
    if role_id:
        e["role_id"] = role_id
    return e


class TestSubmittedEventTypes(unittest.TestCase):
    def test_both_aliases_authoritative(self):
        self.assertIn("submitted", pts.SUBMITTED_EVENT_TYPES)
        self.assertIn("submission_claimed", pts.SUBMITTED_EVENT_TYPES)


class TestTelemetrySubmittedByCompany(unittest.TestCase):
    def test_exact_match(self):
        p = _write_events([_event("ExampleCo", "Senior Backend Engineer")])
        by_company = pts._telemetry_submitted_by_company(p)
        self.assertIn("seniorbackendengineer",
                      by_company.get("exampleco", set()))

    def test_submission_claimed_alias_counts(self):
        p = _write_events([_event("ExampleCo", "Senior Backend Engineer",
                                  etype="submission_claimed")])
        by_company = pts._telemetry_submitted_by_company(p)
        self.assertIn("seniorbackendengineer",
                      by_company.get("exampleco", set()))

    def test_unrelated_event_types_ignored(self):
        p = _write_events([_event("ExampleCo", "Senior Backend Engineer",
                                  etype="browser_task_started")])
        self.assertEqual(pts._telemetry_submitted_by_company(p), {})

    def test_fail_closed_on_missing_file(self):
        self.assertEqual(pts._telemetry_submitted_by_company("/nonexistent/x.jsonl"), {})

    def test_slug_derived_title_candidate(self):
        p = _write_events([_event(
            "ExampleCo", role_id="ATS8-GREENHOUSE-EXAMPLECO-PRODUCT-MANAGER-20260915-J1234567")])
        by_company = pts._telemetry_submitted_by_company(p)
        titles = by_company.get("exampleco", set())
        self.assertTrue(any("productmanager" in t for t in titles),
                        titles)

    def test_cache_refreshes_on_mtime(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps(_event("ExampleCo", "Title One")) + "\n")
        first = pts._telemetry_submitted_by_company(p)
        self.assertIn("titleone", first.get("exampleco", set()))
        with open(p, "a") as f:
            f.write(json.dumps(_event("ExampleCo", "Title Two")) + "\n")
        second = pts._telemetry_submitted_by_company(p)
        self.assertIn("titletwo", second.get("exampleco", set()))


class TestEntryTelemetrySubmitted(unittest.TestCase):
    def test_match_exact(self):
        by_company = {"exampleco": {"seniorbackendengineer"}}
        entry = {"company": "ExampleCo", "title": "Senior Backend Engineer"}
        self.assertEqual(pts._entry_telemetry_submitted(entry, by_company),
                         "seniorbackendengineer")

    def test_prefix_tolerance(self):
        # Queue titles often carry suffix words the event title lacks.
        by_company = {"exampleco": {"accountexecutivelargeenterprisejoin"}}
        entry = {"company": "ExampleCo",
                 "title": "Account Executive, Large/Enterprise — Join Our Remote Talent Community"}
        self.assertIsNotNone(pts._entry_telemetry_submitted(entry, by_company))

    def test_short_title_no_prefix_match(self):
        # A bare "sales" must never match "sales engineer".
        by_company = {"exampleco": {"salesengineer"}}
        entry = {"company": "ExampleCo", "title": "Sales"}
        self.assertIsNone(pts._entry_telemetry_submitted(entry, by_company))

    def test_never_matches_role_id_alone(self):
        by_company = {}
        entry = {"company": "ExampleCo", "title": "Unrelated Title"}
        self.assertIsNone(pts._entry_telemetry_submitted(entry, by_company))

    def test_missing_company_or_title(self):
        by_company = {"exampleco": {"seniorbackendengineer"}}
        self.assertIsNone(pts._entry_telemetry_submitted(
            {"company": "", "title": "Senior Backend Engineer"}, by_company))
        self.assertIsNone(pts._entry_telemetry_submitted(
            {"company": "ExampleCo", "title": ""}, by_company))

    def test_norm_token(self):
        self.assertEqual(pts._norm_token("Account Executive — Enterprise, Grower"),
                         "accountexecutiveenterprisegrower")


if __name__ == "__main__":
    unittest.main()
