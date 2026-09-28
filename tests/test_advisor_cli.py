"""Offline advice uses canonical local trials and never dispatches work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from keel_efficiency.ledger import ResourceLedger

ROOT = Path(__file__).resolve().parents[1]
OFFLINE = '''
import runpy, sys
def deny_network(event, args):
    if event.startswith("socket."):
        raise AssertionError("advice command attempted network")
sys.addaudithook(deny_network)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class AdvisorCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-advisor-cli-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'private-workspace'
        self.success('init')
        self.budget_path = self.home / 'data/resources.sqlite3'
        self.ledger = ResourceLedger(self.budget_path)
        self.ledger.create_scope('window', {'calls': 100, 'compute_ms': 1000000})
        self.budget = ('--budget-ledger', str(self.budget_path), '--budget-scope', 'window')

    def cli(self, *args, role='operator'):
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run([sys.executable, '-B', '-c', OFFLINE, str(ROOT / 'keel.py'),
            '--home', str(self.home), '--role', role, *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30)

    def success(self, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def files(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*')
                if p.is_file() and not p.name.endswith(('-wal', '-shm'))}

    def test_idle_advice_is_sanitized_readonly_and_available_to_review_roles(self):
        self.success('productivity-trial', '--trial-id', 'PRIVATE-TRIAL-SEED',
            '--label', 'PRIVATE-LABEL-SEED', '--max-cycles', '2', '--live', *self.budget)
        before = self.files()
        for role in ('operator', 'discovery', 'preparation', 'evidence', 'qa',
                     'security', 'ux', 'reliability'):
            result = self.success('productivity-advice', '--trial-id', 'PRIVATE-TRIAL-SEED',
                                  *self.budget, role=role)
            self.assertFalse(result['execution_authorized'])
            self.assertFalse(result['submission_authorized'])
            serialized = json.dumps(result)
            self.assertNotIn('PRIVATE-TRIAL-SEED', serialized)
            self.assertNotIn('PRIVATE-LABEL-SEED', serialized)
            self.assertNotIn(str(self.home), serialized)
        self.assertEqual(self.files(), before)

    def test_missing_canonical_trial_cannot_be_replaced_by_a_report(self):
        before = self.files()
        result = self.cli('productivity-advice', '--trial-id', 'missing', *self.budget)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'HOLD')
        self.assertEqual(self.files(), before)
        for flag in ('--report-json', '--live', '--apply'):
            result = self.cli('productivity-advice', '--trial-id', 'missing', flag, *self.budget)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)

    def test_invalid_cycle_cap_and_absent_budget_are_rejected_without_creation(self):
        for value in ('0', '-1', '101'):
            result = self.cli('productivity-advice', '--trial-id', 'missing', '--max-cycles', value, *self.budget)
            self.assertEqual(result.returncode, 2)
        missing = self.home / 'data/missing.sqlite3'
        result = self.cli('productivity-advice', '--trial-id', 'missing',
                          '--budget-ledger', str(missing), '--budget-scope', 'window')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())

    def test_preflight_cli_observes_requested_budget_without_widening_it(self):
        self.ledger.create_scope('small', {'calls': 1, 'compute_ms': 1001})
        args = ('host-preflight', '--budget-ledger', str(self.budget_path), '--budget-scope', 'small')
        self.assertEqual(self.cli(*args).returncode, 1)
        report = self.success(*args, '--max-requests', '1', '--timeout', '1.0001',
                              '--target-verified', '7', '--backlog-limit', '9')
        self.assertEqual(report['budget']['requested_cycle_estimate']['compute_ms'], 1001)
        self.assertEqual(report['policy'], {'target_verified': 7, 'backlog_limit': 9})
        self.assertEqual(self.ledger.snapshot('small')['available']['calls'], 1)


if __name__ == '__main__':
    unittest.main()
