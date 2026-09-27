"""Shipped bounded-trial commands preserve offline defaults and shared budgets."""
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
def reject_network(event, args):
    if event.startswith("socket."):
        raise AssertionError("offline CLI attempted network access")
sys.addaudithook(reject_network)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class SustainedCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-sustained-cli-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'private workspace'
        self.success('init')

    def cli(self, *args, role='operator'):
        env = {key: value for key, value in os.environ.items()
               if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run([sys.executable, '-c', OFFLINE, str(ROOT / 'keel.py'),
            '--home', str(self.home), '--role', role, *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=20)

    def success(self, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def budget(self):
        path = self.home / 'data/resources.sqlite3'
        ledger = ResourceLedger(path)
        ledger.create_scope('window', {'calls': 16, 'compute_ms': 120000})
        return ledger, ('--budget-ledger', str(path), '--budget-scope', 'window')

    def test_preview_is_offline_and_does_not_initialize_history(self):
        report = self.success('productivity-trial', '--trial-id', 'preview')
        self.assertTrue(report['dry_run'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse((self.home / 'data/productivity').exists())

    def test_trial_requires_existing_allowance_before_manifest_creation(self):
        result = self.cli('productivity-trial', '--trial-id', 'missing', '--live')
        self.assertEqual(result.returncode, 2)
        missing = self.home / 'data/missing.sqlite3'
        result = self.cli('productivity-trial', '--trial-id', 'missing', '--live',
                          '--budget-ledger', str(missing), '--budget-scope', 'window')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())
        self.assertFalse((self.home / 'data/productivity/trials').exists())

    def test_idle_trial_replay_uses_no_new_budget_and_reports_real_receipts(self):
        ledger, budget = self.budget()
        args = ('productivity-trial', '--trial-id', 'idle', '--max-cycles', '3', '--live', *budget)
        self.success(*args)
        before = ledger.snapshot('window')
        self.success(*args)
        self.assertEqual(ledger.snapshot('window'), before)
        report = self.success('productivity-trial-report', '--trial-id', 'idle', *budget)
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['production_savings_proven'])
        self.assertIsNone(report['external_credit_micros'])
        self.assertEqual(self.cli(*args, '--max-cycles', '4').returncode, 2)

    def test_cohort_size_and_role_boundaries_are_enforced(self):
        for size in ('0', '-1', '101'):
            self.assertEqual(self.cli('productivity-trial', '--trial-id', 'bad',
                                     '--max-cycles', size).returncode, 2)
        for role in ('qa', 'security', 'evidence', 'reliability', 'preparation', 'ux'):
            result = self.cli('productivity-trial', '--trial-id', 'denied', role=role)
            self.assertEqual(result.returncode, 2)
            self.assertIn('role does not allow', result.stderr)
        self.success('productivity-trial', '--trial-id', 'allowed', role='discovery')

    def test_receipt_review_cannot_load_a_validator_or_enable_writes(self):
        for extra in (('--live',), ('--validator', 'attacker.module')):
            result = self.cli('receipt-review', '--role-id', 'synthetic-role',
                '--attempt-id', 'synthetic-attempt', '--receipts', 'data/receipts.json', *extra)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)
        self.assertEqual(json.loads((self.home / 'data/queues/standard-queue.json').read_text()), [])


if __name__ == '__main__':
    unittest.main()
