"""Exercise the shipped CLI in fresh processes with all sockets prohibited."""
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
def offline(event, args):
    if event.startswith("socket."):
        raise AssertionError("CLI fixture attempted networking")
sys.addaudithook(offline)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class ProductivityCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-productivity-cli-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'workspace with spaces'
        self.assertEqual(self.cli('init').returncode, 0)

    def cli(self, *args, role='operator'):
        env = {key: value for key, value in os.environ.items()
               if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
        env.update(HOME=self.temp.name, PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-c', OFFLINE, str(ROOT / 'keel.py'),
            '--home', str(self.home), '--role', role, *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)

    def successful(self, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def budget(self):
        path = self.home / 'data/resources.sqlite3'
        ledger = ResourceLedger(path)
        ledger.create_scope('fixture-window', {'calls': 16, 'compute_ms': 120000})
        return path, ledger

    def test_status_and_planning_are_offline_and_do_not_create_run_history(self):
        self.successful('source-add', 'greenhouse:synthetic-controller-fixture')
        report = self.successful('productivity-status')
        self.assertEqual(report['next_stage'], 'discover')
        plan = self.successful('productivity-once')
        self.assertTrue(plan['dry_run'])
        self.assertFalse(plan['execution_authorized'])
        self.assertEqual(plan['network_calls'], 0)
        self.assertFalse((self.home / 'data/productivity').exists())
        self.assertEqual(json.loads((self.home / 'data/queues/standard-queue.json').read_text()), [])

    def test_review_roles_can_inspect_but_cannot_run_the_cycle(self):
        for role in ('preparation', 'evidence', 'qa', 'security', 'ux', 'reliability'):
            with self.subTest(role=role):
                self.successful('productivity-status', role=role)
                denied = self.cli('productivity-once', role=role)
                self.assertEqual(denied.returncode, 2)
                self.assertIn('role does not allow', denied.stderr)
        self.successful('productivity-once', role='discovery')

    def test_live_requires_explicit_existing_budget_and_stable_run_id(self):
        for args in (('--live',), ('--live', '--run-id', 'fixture-run')):
            with self.subTest(args=args):
                self.assertEqual(self.cli('productivity-once', *args).returncode, 2)
        self.assertFalse((self.home / 'data/productivity').exists())
        path, _ = self.budget()
        result = self.cli('productivity-once', '--live', '--budget-ledger', str(path),
                          '--budget-scope', 'fixture-window')
        self.assertEqual(result.returncode, 2)

    def test_missing_or_incomplete_budget_configuration_never_creates_an_allowance(self):
        path = self.home / 'data/missing.sqlite3'
        missing = self.cli('productivity-status', '--budget-ledger', str(path), '--budget-scope', 'new')
        self.assertEqual(missing.returncode, 2)
        self.assertFalse(path.exists())
        self.assertEqual(self.cli('productivity-once', '--budget-scope', 'new').returncode, 2)

    def test_idle_receipt_replays_without_additional_budget_use(self):
        path, ledger = self.budget()
        args = ('productivity-once', '--live', '--run-id', 'fixture-idle',
                '--budget-ledger', str(path), '--budget-scope', 'fixture-window')
        first = self.successful(*args)
        charged = ledger.snapshot('fixture-window')
        second = self.successful(*args)
        self.assertFalse(first['execution_authorized'])
        self.assertTrue(second['replayed'])
        self.assertEqual(second['receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(ledger.snapshot('fixture-window'), charged)
        self.assertEqual(charged['used']['calls'], 0)

    def test_reusing_run_id_with_changed_limits_is_rejected(self):
        path, _ = self.budget()
        args = ('productivity-once', '--live', '--run-id', 'fixture-bound',
                '--budget-ledger', str(path), '--budget-scope', 'fixture-window')
        self.successful(*args)
        self.assertEqual(self.cli(*args, '--max-requests', '1').returncode, 2)

    def test_recovery_is_operator_only_and_does_not_repeat_completed_work(self):
        path, ledger = self.budget()
        budget = ('--budget-ledger', str(path), '--budget-scope', 'fixture-window')
        first = self.successful('productivity-once', '--live', '--run-id', 'fixture-recover', *budget)
        before = ledger.snapshot('fixture-window')
        denied = self.cli('productivity-recover', '--run-id', 'fixture-recover', *budget, role='discovery')
        self.assertEqual(denied.returncode, 2)
        recovered = self.successful('productivity-recover', '--run-id', 'fixture-recover', *budget)
        self.assertEqual(recovered['receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(ledger.snapshot('fixture-window'), before)
        self.assertFalse(recovered['execution_authorized'])

    def test_invalid_limits_fail_without_network_or_run_receipts(self):
        for flag in ('--max-requests', '--timeout', '--max-new', '--verify-limit', '--target-verified'):
            with self.subTest(flag=flag):
                self.assertEqual(self.cli('productivity-once', flag, '-1').returncode, 2)
        self.assertFalse((self.home / 'data/productivity').exists())


if __name__ == '__main__':
    unittest.main()
