"""Synthetic outcome accounting regressions; no provider calls or prices."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from keel_assurance.completion_cost import summarize_completion_cost
from keel_assurance.metrics import MetricsError


def fixture():
    return {'schema_version': 1, 'cohort': 'synthetic', 'window': 'fixture-window',
            'outcome_definition': 'fixture postcondition', 'unit': 'fixture_credits',
            'basis': 'trace_estimate',
            'attempts': [{'task_id': 'a', 'attempt_id': 'a1', 'amount': '2'},
                         {'task_id': 'a', 'attempt_id': 'a2', 'amount': '3'},
                         {'task_id': 'b', 'attempt_id': 'b1', 'amount': '7'},
                         {'task_id': 'c', 'attempt_id': 'c1', 'amount': '11'}],
            'outcomes': [{'task_id': 'a', 'verified': True, 'verifier_ref': 'fixture:a'},
                         {'task_id': 'b', 'verified': False, 'verifier_ref': 'fixture:b'},
                         {'task_id': 'c', 'verified': None, 'verifier_ref': None}]}


class CompletionCostTests(unittest.TestCase):
    def test_failures_unknowns_and_retries_count_in_numerator(self):
        data = fixture()
        before = copy.deepcopy(data)
        report = summarize_completion_cost(data)
        self.assertEqual(report['cost_per_verified_completion'], '23')
        self.assertEqual(report['known_spend_on_verified_tasks'], '5')
        self.assertEqual(report['known_spend_on_failed_tasks'], '7')
        self.assertEqual(report['known_spend_on_unknown_tasks'], '11')
        self.assertEqual(report['counts']['verified_completions'], 1)
        self.assertEqual(report['counts']['additional_attempts'], 1)
        self.assertFalse(report['execution_authority'])
        self.assertEqual(data, before)

    def test_missing_spend_is_not_free(self):
        data = fixture()
        data['attempts'][2]['amount'] = None
        report = summarize_completion_cost(data)
        self.assertEqual(report['known_attempt_spend'], '16')
        self.assertEqual(report['counts']['unmetered_attempts'], 1)
        self.assertIsNone(report['total_attempt_spend'])
        self.assertIsNone(report['cost_per_verified_completion'])

    def test_no_completions_has_no_ratio(self):
        data = fixture()
        data['outcomes'][0]['verified'] = False
        self.assertIsNone(summarize_completion_cost(data)['cost_per_verified_completion'])

    def test_empty_cohort_is_unavailable(self):
        data = fixture()
        data['attempts'] = data['outcomes'] = []
        report = summarize_completion_cost(data)
        self.assertIsNone(report['total_attempt_spend'])
        self.assertIsNone(report['cost_per_verified_completion'])

    def test_measured_zero_and_decimal_precision(self):
        data = fixture()
        for row in data['attempts']:
            row['amount'] = '0'
        self.assertEqual(summarize_completion_cost(data)['cost_per_verified_completion'], '0')
        data['attempts'][0]['amount'] = '0.1'
        data['attempts'][1]['amount'] = '0.2'
        self.assertEqual(summarize_completion_cost(data)['cost_per_verified_completion'], '0.3')

    def test_duplicates_or_unmatched_records_rejected(self):
        for field in ('attempts', 'outcomes'):
            data = fixture()
            data[field].append(copy.deepcopy(data[field][0]))
            with self.assertRaises(MetricsError):
                summarize_completion_cost(data)
        for field in ('attempts', 'outcomes'):
            data = fixture()
            data[field].pop()
            with self.assertRaises(MetricsError):
                summarize_completion_cost(data)

    def test_invalid_amounts_rejected(self):
        for amount in (True, 0, 0.2, '-1', 'NaN', 'Infinity', '1e19', '1e-19', '0e999999999', ' 1', '1_000', {}, 'x'):
            data = fixture()
            data['attempts'][0]['amount'] = amount
            with self.subTest(amount=amount), self.assertRaises(MetricsError):
                summarize_completion_cost(data)

    def test_unreferenced_or_nonboolean_outcomes_rejected(self):
        for verified, ref in ((True, None), (False, None), (1, 'fixture'), ('true', 'fixture')):
            data = fixture()
            data['outcomes'][0].update(verified=verified, verifier_ref=ref)
            with self.assertRaises(MetricsError):
                summarize_completion_cost(data)

    def test_mixed_basis_unknown_fields_and_versions_rejected(self):
        for key, value in (('basis', 'mixed'), ('schema_version', True), ('unit', ''), ('extra', 0)):
            data = fixture()
            data[key] = value
            with self.assertRaises(MetricsError):
                summarize_completion_cost(data)
        data = fixture()
        data['attempts'][0]['currency'] = 'USD'
        with self.assertRaises(MetricsError):
            summarize_completion_cost(data)

    def test_cli_clean_python_and_read_only_failure(self):
        script = Path(__file__).resolve().parents[1] / 'tools/completion_cost_report.py'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            source.write_text(json.dumps(fixture()))
            before = source.read_bytes()
            run = subprocess.run([sys.executable, '-S', str(script), str(source)],
                                 capture_output=True, text=True, cwd=directory)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['cost_per_verified_completion'], '23')
            self.assertEqual(source.read_bytes(), before)
            source.write_text('{"schema_version":1,"schema_version":2}')
            run = subprocess.run([sys.executable, '-S', str(script), str(source)],
                                 capture_output=True, text=True, cwd=directory)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, '')
            self.assertIn('duplicate JSON key', run.stderr)


if __name__ == '__main__':
    unittest.main()
