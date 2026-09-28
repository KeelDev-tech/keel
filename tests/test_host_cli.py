"""Cold CLI checks of non-migrating inspection and host-preflight boundaries."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
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
        raise AssertionError("offline host command attempted network access")
sys.addaudithook(reject_network)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class HostCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-host-cli-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'private workspace'

    def cli(self, *args, role='operator'):
        env = {key: value for key, value in os.environ.items()
               if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run([sys.executable, '-c', OFFLINE, str(ROOT / 'keel.py'),
            '--home', str(self.home), '--role', role, *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30)

    def success(self, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def files(self):
        return {str(p.relative_to(self.home)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.home.rglob('*') if p.is_file()
                and not p.name.endswith(('-wal', '-shm'))}

    def test_missing_workspace_is_blocked_without_creating_it(self):
        result = self.cli('host-preflight')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['local_checks_passed'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(self.home.exists())

    def test_existing_host_check_is_offline_and_preserves_business_files(self):
        self.success('init')
        path = self.home / 'data/resources.sqlite3'
        ledger = ResourceLedger(path)
        ledger.create_scope('window', {'calls': 32, 'compute_ms': 1000000})
        before = self.files()
        for role in ('operator', 'discovery', 'preparation', 'evidence', 'qa',
                     'security', 'ux', 'reliability'):
            report = self.success('host-preflight', '--budget-ledger', str(path),
                                  '--budget-scope', 'window', role=role)
            self.assertTrue(report['local_checks_passed'])
            self.assertFalse(report['submission_authorized'])
        self.assertEqual(self.files(), before)
        self.assertFalse((self.home / 'data/productivity').exists())
        self.assertFalse((self.home / 'hidden_files/queue.lock').exists())

    def test_missing_ledger_is_never_initialized_by_inspection(self):
        self.success('init')
        path = self.home / 'data/missing.sqlite3'
        result = self.cli('host-preflight', '--budget-ledger', str(path),
                          '--budget-scope', 'window')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.cli('productivity-status', '--budget-ledger', str(path),
                                 '--budget-scope', 'window').returncode, 2)
        self.assertFalse(path.exists())

    def test_inspection_does_not_initialize_unrelated_sqlite_database(self):
        self.success('init')
        path = self.home / 'data/unrelated.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')
        before = path.read_bytes()
        commands = (('productivity-status',), ('productivity-once',),
                    ('productivity-trial', '--trial-id', 'preview'),
                    ('productivity-trial-report', '--trial-id', 'missing'),
                    ('productivity-compare', '--baseline', 'a', '--candidate', 'b'))
        for command in commands:
            result = self.cli(*command, '--budget-ledger', str(path), '--budget-scope', 'window')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.home / 'data/productivity').exists())

    def test_preflight_has_no_live_or_repair_switch(self):
        for flag in ('--live', '--repair', '--reset-budget'):
            result = self.cli('host-preflight', flag)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)


if __name__ == '__main__':
    unittest.main()
