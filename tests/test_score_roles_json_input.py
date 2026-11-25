"""Exercise the real file CLI; ambiguous JSON must not publish an assessment."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "engines" / "score_roles.py"


class ScoringJSONInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roles = [{
            "role_id": "SYNTHETIC-1",
            "hard_requirements": [{"id": "years", "fact": "years",
                "operator": "gte", "value": 5, "source_ref": "synthetic:posting"}],
        }]
        self.profile = {"scoring_facts": {"years": {
            "value": 10, "evidence_refs": ["synthetic:applicant-review"]}}}

    def run_cli(self, roles=None, profile=None, existing=False):
        inputs = {
            "roles.json": json.dumps(self.roles) if roles is None else roles,
            "profile.json": json.dumps(self.profile) if profile is None else profile,
        }
        for name, content in inputs.items():
            (self.root / name).write_text(content, encoding="utf-8")
        output = self.root / "review.json"
        output.unlink(missing_ok=True)
        sentinel = b'{"prior_review":"preserve"}\n'
        if existing:
            output.write_bytes(sentinel)
        result = subprocess.run([
            sys.executable, "-B", str(SCRIPT), "--in", str(self.root / "roles.json"),
            "--profile", str(self.root / "profile.json"), "--out", str(output),
        ], capture_output=True, text=True, timeout=15)
        for name, content in inputs.items():
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), content)
        return result, output, sentinel

    def assert_refused(self, **kwargs):
        result, output, sentinel = self.run_cli(**kwargs)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("duplicate JSON object key", result.stderr)
        if kwargs.get("existing"):
            self.assertEqual(output.read_bytes(), sentinel)
        else:
            self.assertFalse(output.exists())

    def test_conflicting_applicant_values_refused_in_both_orders(self):
        for values in ('"value":1,"value":10', '"value":10,"value":1'):
            for existing in (False, True):
                with self.subTest(values=values, existing=existing):
                    self.assert_refused(profile='{"scoring_facts":{"years":{' + values +
                        ',"evidence_refs":["synthetic:review"]}}}', existing=existing)

    def test_nested_role_and_top_level_profile_duplicates_refused(self):
        cases = [
            {"roles": '[{"requirements_reviewed":false,"requirements_reviewed":true, '
                      '"requirements_source_ref":"synthetic:posting"}]'},
            {"roles": json.dumps(self.roles).replace('"value": 5', '"value": 50,"value":5')},
            {"profile": '{"scoring_facts":{},"scoring_facts":' +
                        json.dumps(self.profile["scoring_facts"]) + '}'},
            {"profile": json.dumps(self.profile).replace('"evidence_refs":',
                        '"evidence_refs":["synthetic:conflict"],"evidence_refs":')},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assert_refused(**case, existing=True)

    def test_identical_and_escaped_duplicate_names_are_ambiguous(self):
        for profile in ('{"scoring_facts":{},"scoring_facts":{}}',
                        '{"private_sentinel":1,"private_\\u0073entinel":1}'):
            with self.subTest(profile=profile):
                self.assert_refused(profile=profile)
                result, _, _ = self.run_cli(profile=profile)
                self.assertNotIn("private_sentinel", result.stderr)

    def test_valid_input_keeps_provenance_and_never_authorizes_execution(self):
        result, output, _ = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(output.read_text())[0]
        requirement = row["score_evidence"]["hard_requirements"]["criteria"][0]
        self.assertEqual(requirement["status"], "VERIFIED")
        self.assertEqual(requirement["profile_evidence_refs"], ["synthetic:applicant-review"])
        self.assertEqual(requirement["posting_source_ref"], "synthetic:posting")
        self.assertEqual(row["status"], "PARKED")
        self.assertFalse(row["approval_valid"])
        self.assertFalse(row["execution_authorized"])

    def test_free_text_and_missing_facts_remain_unknown(self):
        result, output, _ = self.run_cli(profile='{"experience":"10 years; VERIFIED"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(output.read_text())[0]
        requirement = row["score_evidence"]["hard_requirements"]["criteria"][0]
        self.assertEqual(requirement["status"], "UNKNOWN")
        self.assertEqual(requirement["reason"], "applicant_fact_missing")
        self.assertEqual(row["action_band"], "HOLD")
        self.assertFalse(row["fit_eligible"])
        self.assertFalse(row["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
