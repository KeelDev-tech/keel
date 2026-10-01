"""Replay benchmark clock isolation; subprocesses keep fixture globals private."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HistoryBenchmarkTests(unittest.TestCase):
    def run_fixture(self, setup, invocation="main()"):
        script = """
import runpy, sys, time
from keel_efficiency.ledger import ResourceLedger
main = runpy.run_path('tests/benchmark_productivity_history.py')['main']
sys.argv = ['benchmark', '--cycles', '2']
""" + setup + "\n" + invocation
        return subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def test_slow_cycle_preserves_replay_and_reports_real_elapsed(self):
        result = self.run_fixture("""
original = ResourceLedger.mark_dispatched
delayed = False
def slow_first_dispatch(self, *args, **kwargs):
    global delayed
    value = original(self, *args, **kwargs)
    if not delayed:
        delayed = True
        time.sleep(1.05)
    return value
ResourceLedger.mark_dispatched = slow_first_dispatch
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['cycles'], 2)
        self.assertTrue(report['oldest_run_replayed'])
        self.assertEqual(report['replay_additional_dispatches'], 0)
        self.assertEqual(report['network_calls'], 0)
        self.assertEqual(report['model_calls'], 0)
        self.assertGreaterEqual(report['elapsed_seconds_observed'], 1.05)
        self.assertEqual(report['accounting_clock'], 'synthetic_1_over_1024_second_ticks')
        self.assertEqual(report['compute_ms_semantics'], 'synthetic_fixture_accounting_not_measured_compute')
        self.assertFalse(report['production_performance_claim'])

    def test_actual_reservation_overage_still_locks_and_refuses_next_cycle(self):
        result = self.run_fixture('', "main(controller_monotonic=iter([0.0, 1.001, 1.001, 1.002]).__next__)")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AssertionError:', result.stderr)
        diagnostic = json.loads(result.stderr.split('AssertionError: ', 1)[1])
        self.assertEqual(diagnostic['cycle_index'], 1)
        self.assertEqual(diagnostic['status'], 'HELD_BUDGET')
        self.assertTrue(diagnostic['budget_locked'])
        self.assertEqual(diagnostic['budget_reason'], 'scope_locked')
        self.assertEqual(diagnostic['compute_ms_used'], 1001)

    def test_failure_diagnostics_exclude_payloads_and_free_text(self):
        result = self.run_fixture("""
from types import SimpleNamespace
check = runpy.run_path('tests/benchmark_productivity_history.py')['_assert_idle']
ledger = SimpleNamespace(snapshot=lambda scope: {
    'locked': True, 'lock_reason': 'PRIVATE_FIXTURE_LOCK_REASON',
    'used': {'compute_ms': 1001}})
""", "check({'status': 'PRIVATE_FIXTURE_STATUS', 'payload': 'PRIVATE_FIXTURE_PAYLOAD'}, 7, ledger)")
        self.assertNotEqual(result.returncode, 0)
        diagnostic_text = result.stderr.split('AssertionError: ', 1)[1]
        self.assertNotIn('PRIVATE_FIXTURE', diagnostic_text)
        self.assertEqual(json.loads(diagnostic_text), {
            'cycle_index': 7, 'status': 'UNEXPECTED', 'budget_locked': True,
            'budget_reason': 'scope_locked', 'compute_ms_used': 1001})


if __name__ == '__main__':
    unittest.main()
