"""The shipped offline rehearsal runs real APIs without touching a live home."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/run_productivity_rehearsal.py'
OFFLINE = '''
import runpy, sys
def deny_network(event, args):
    if event.startswith("socket."):
        raise AssertionError("offline rehearsal launcher attempted network access")
sys.addaudithook(deny_network)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class ProductivityRehearsalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-productivity-rehearsal-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.existing = self.root / 'existing-home'
        self.existing.mkdir()
        (self.existing / 'sentinel.json').write_text('{"do_not_modify":true}\n', encoding='utf-8')

    def cli(self, output, *extra):
        env = {key: value for key, value in os.environ.items()
               if key in {'PATH', 'LANG', 'LC_ALL'}}
        env.update(KEEL_HOME=str(self.existing),
                   JOB_PIPELINE_HTTP_COOLDOWN_DIR=str(self.existing / 'cooldowns'),
                   PYTHONPATH=str(self.existing), PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-B', '-c', OFFLINE, str(TOOL),
                               '--out', str(output), *extra], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_real_offline_flow_records_receipts_replay_budget_and_protected_rows(self):
        output = self.root / 'new private rehearsal'
        before = (self.existing / 'sentinel.json').read_bytes()
        result = self.cli(output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, json.loads((output / 'report.json').read_text()))
        self.assertEqual(report['status'], 'PASS')
        self.assertGreaterEqual(len(report['checks']), 15)
        self.assertTrue(all(report['checks'].values()))
        self.assertEqual(report['observations']['stages'], ['discover', 'verify', 'idle'])
        self.assertEqual(report['observations']['retained_run_receipts'], 3)
        self.assertEqual(report['effects'], {'network_calls': 0, 'model_calls': 0,
                         'submissions': 0, 'synthetic_fetch_calls': 2, 'socket_attempts_denied': 0})
        self.assertTrue(report['synthetic'])
        for field in ('production_performance_claim', 'live_host_qualified',
                      'execution_authorized', 'submission_authorized', 'paid_services_required'):
            self.assertIs(report[field], False)
        self.assertIsNone(report['actual_credit_savings'])

        evidence = json.loads((output / 'replay-evidence.json').read_text())
        self.assertEqual(evidence['before'], evidence['after'])
        receipts = evidence['after']['receipts']['receipts']
        self.assertEqual(len({row['run_id'] for row in receipts}), 3)
        self.assertTrue(all(row['phase'] == 'COMPLETE' for row in receipts))
        home = output / 'workspace'
        with sqlite3.connect((home / 'data/productivity/history.sqlite3').as_uri() + '?mode=ro', uri=True) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 3)
        with sqlite3.connect((home / 'data/resources.sqlite3').as_uri() + '?mode=ro', uri=True) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM efficiency_requests').fetchone()[0], 3)
        budget = json.loads((output / 'budget-evidence.json').read_text())
        self.assertEqual(budget['before'], budget['after'])
        refusal = json.loads((output / 'budget-refusal.json').read_text())
        self.assertEqual((refusal['stop_reason'], refusal['measurement_status'], refusal['receipt_runs']),
                         ('HELD_BUDGET', 'INCONCLUSIVE', 0))
        trial = json.loads((output / 'trial.json').read_text())
        self.assertEqual((trial['stop_reason'], len(trial['planned_not_dispatched'])), ('IDLE', 1))
        rows = json.loads((home / 'data/queues/standard-queue.json').read_text())
        by_id = {row['role_id']: row for row in rows}
        self.assertEqual(by_id['synthetic-unknown']['status'], 'UNKNOWN_OUTCOME')
        self.assertEqual(by_id['synthetic-unknown']['holds'], ['unknown_application'])
        self.assertEqual(by_id['synthetic-human-hold']['approval'], {'approved': False})
        self.assertEqual(by_id['synthetic-human-hold']['holds'], ['human_approval_required'])
        self.assertEqual(json.loads((home / 'data/application-ledger.json').read_text()), [])
        self.assertEqual((self.existing / 'sentinel.json').read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.existing.iterdir()), ['sentinel.json'])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for path in output.rglob('*'):
            self.assertFalse(path.is_symlink())
            self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o077, str(path))
        self.assertFalse(list(self.root.rglob('__pycache__')))

    def test_existing_empty_nonempty_and_symlink_destinations_are_refused(self):
        empty = self.root / 'empty'
        empty.mkdir()
        link = self.root / 'linked-output'
        link.symlink_to(self.existing, target_is_directory=True)
        before = (self.existing / 'sentinel.json').read_bytes()
        for output in (empty, self.existing, link):
            for extra in ((), ('--_worker',)):
                with self.subTest(output=output.name, extra=extra):
                    result = self.cli(output, *extra)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual((self.existing / 'sentinel.json').read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.existing.iterdir()), ['sentinel.json'])

    def test_missing_or_symlinked_parent_is_refused_without_creation(self):
        missing = self.root / 'missing'
        parent_link = self.root / 'parent-link'
        parent_link.symlink_to(self.existing, target_is_directory=True)
        for output in (missing / 'rehearsal', parent_link / 'rehearsal'):
            with self.subTest(output=str(output)):
                result = self.cli(output)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(output.exists())
        self.assertFalse(missing.exists())

    def test_import_does_not_change_environment_path_or_import_engines(self):
        environment = dict(os.environ)
        path = list(sys.path)
        names = ('keel_paths', 'queue_io', 'pipeline_service', 'productivity_service', 'productivity_trials')
        engines = {name: sys.modules.get(name) for name in names}
        spec = importlib.util.spec_from_file_location('keel_rehearsal_import_safety', TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(sys.path, path)
        self.assertEqual({name: sys.modules.get(name) for name in names}, engines)


if __name__ == '__main__':
    unittest.main()
