"""Cold offline onboarding observations must not become execution authority."""
import hashlib
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
import sys, runpy
def reject(event, args):
    if event.startswith('socket.'):
        raise AssertionError('onboarding must not contact a network')
sys.addaudithook(reject)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


class HostOnboardingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='keel-onboarding-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'workspace'
        self.assertEqual(self.cli('init')[0], 0)
        self.path = self.home / 'data/budget.sqlite3'
        self.ledger = ResourceLedger(self.path)
        self.ledger.create_scope('synthetic', {'calls': 100, 'compute_ms': 1000000})

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, '-B', '-c', OFFLINE, str(ROOT / 'keel.py'),
                                 '--home', str(self.home), *args], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def snapshot(self):
        result = {}
        for path in self.home.rglob('*'):
            if path.is_symlink():
                result[str(path.relative_to(self.home))] = ('link', os.readlink(path))
            elif path.is_file() and not path.name.endswith(('-wal', '-shm')):
                result[str(path.relative_to(self.home))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def inspect(self, *args):
        before = self.snapshot()
        code, report = self.cli('host-preflight', *args)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['submission_authorized'])
        self.assertFalse(report['production_readiness_established'])
        self.assertEqual(report['network_calls'], 0)
        self.assertNotIn('PRIVATE-SYNTHETIC-CANARY', json.dumps(report))
        self.assertNotIn(str(self.home), json.dumps(report))
        self.assertIn('onboarding', report)
        self.assertEqual(report['onboarding']['preparation']['status'], 'NOT_ASSESSED')
        self.assertEqual(report['onboarding']['discovery']['network_qualification'], 'UNKNOWN')
        self.assertEqual(report['onboarding']['submission']['status'], 'NOT_AUTHORIZED')
        return code, report

    def budget_reason(self, *args):
        code, report = self.inspect(*args)
        self.assertEqual(code, 1)
        check = next(c for c in report['checks'] if c['id'] == 'resource_ledger')
        self.assertEqual(check['status'], 'BLOCKED')
        return check['reason'], report['onboarding']['budget_next_step']

    def test_omitted_and_partial_configuration_have_distinct_actions(self):
        self.assertEqual(self.budget_reason()[0], 'budget_configuration_not_supplied')
        for args in [('--budget-ledger', str(self.path)), ('--budget-scope', 'synthetic')]:
            with self.subTest(args=args):
                reason, action = self.budget_reason(*args)
                self.assertEqual(reason, 'budget_configuration_incomplete')
                self.assertIn('--budget-ledger and --budget-scope', action)

    def test_missing_ledger_is_observed_without_creation(self):
        path = self.home / 'absent/ledger.sqlite3'
        reason, action = self.budget_reason('--budget-ledger', str(path), '--budget-scope', 'synthetic')
        self.assertEqual(reason, 'budget_ledger_not_found')
        self.assertFalse(path.parent.exists())
        self.assertIn('do not substitute', action)

    def test_unsafe_corrupt_and_unknown_scope_remain_generic_refusals(self):
        corrupt = self.home / 'data/corrupt.sqlite3'
        corrupt.write_bytes(b'PRIVATE-SYNTHETIC-CANARY')
        link = self.home / 'data/link.sqlite3'
        link.symlink_to(self.path)
        dangling = self.home / 'data/dangling.sqlite3'
        dangling.symlink_to(self.home / 'absent')
        parent_link = self.home / 'alias'
        parent_link.symlink_to(self.home / 'data', target_is_directory=True)
        directory = self.home / 'data/directory'
        directory.mkdir()
        for path, scope in [(corrupt, 'synthetic'), (link, 'synthetic'), (dangling, 'synthetic'),
                            (parent_link / 'absent', 'synthetic'), (directory, 'synthetic'),
                            (self.path, 'missing-scope')]:
            with self.subTest(path=path.name, scope=scope):
                reason, action = self.budget_reason('--budget-ledger', str(path), '--budget-scope', scope)
                self.assertEqual(reason, 'existing_budget_and_scope_missing_unsafe_or_invalid')
                self.assertIn('do not replace or reset', action)
        alias = self.home / 'data/hardlink.sqlite3'
        os.link(self.path, alias)
        self.assertEqual(self.budget_reason('--budget-ledger', str(alias), '--budget-scope', 'synthetic')[0],
                         'existing_budget_and_scope_missing_unsafe_or_invalid')

    def test_valid_empty_workspace_pass_is_not_preparation_or_discovery_qualification(self):
        code, report = self.inspect('--budget-ledger', str(self.path), '--budget-scope', 'synthetic')
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['suggested_stage']['reason'], 'no_registered_sources')
        self.assertEqual(report['onboarding']['discovery']['status'], 'NOT_QUALIFIED')
        self.assertEqual(report['onboarding']['discovery']['local_blockers'], ['no_enabled_sources'])
        self.assertIsNone(report['onboarding']['budget_next_step'])
        code, doctor = self.cli('doctor')
        self.assertEqual(code, 1)
        self.assertFalse(doctor['ready_for_local_preparation'])

    def test_source_registration_never_implies_network_or_submission_qualification(self):
        (self.home / 'data/sources.json').write_text(json.dumps({'sources': [
            {'ref': 'greenhouse:synthetic', 'enabled': True}]}))
        code, report = self.inspect('--budget-ledger', str(self.path), '--budget-scope', 'synthetic')
        self.assertEqual(code, 0)
        self.assertEqual(report['suggested_stage']['stage'], 'discover')
        self.assertFalse(report['suggested_stage']['dispatch_authorized'])
        self.assertEqual(report['onboarding']['discovery']['status'], 'NOT_QUALIFIED')
        self.assertEqual(report['onboarding']['discovery']['local_blockers'], [])

    def test_insufficient_allowance_preserves_existing_block(self):
        self.ledger.create_scope('tiny', {'calls': 0, 'compute_ms': 0})
        code, report = self.inspect('--budget-ledger', str(self.path), '--budget-scope', 'tiny')
        self.assertEqual(code, 1)
        self.assertIn('local_preflight_not_passed', report['onboarding']['discovery']['local_blockers'])
        self.assertEqual(next(c for c in report['checks'] if c['id'] == 'default_cycle_budget')['status'], 'BLOCKED')

    def test_early_workspace_failure_stays_unassessed(self):
        self.home = self.home / 'not-created'
        code, report = self.inspect()
        self.assertEqual(code, 1)
        self.assertFalse(self.home.exists())
        self.assertIn('sources_not_assessed', report['onboarding']['discovery']['local_blockers'])


if __name__ == '__main__':
    unittest.main()
