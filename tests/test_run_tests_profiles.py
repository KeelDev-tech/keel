"""Runner routing and guard-reporting checks; child suite execution is mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import run_tests


class RunnerProfileTests(unittest.TestCase):
    def test_missing_guard_blocks_default_guarded_suite(self):
        with patch.object(run_tests, 'audit_guard_available', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'audit guard unavailable'):
                run_tests.suite_environment('core', run_tests.ROOT,
                                           '/tmp/synthetic-workspace', Path('/tmp/report'))

    def test_explicit_unguarded_mode_removes_stale_guard_environment(self):
        root = run_tests.ROOT
        inherited = {key: 'inherited-value' for key in (
            'KEEL_AUDIT_TEST_ROOT', 'KEEL_AUDIT_REPORT', 'KEEL_AUDIT_CODE')}
        inherited['PYTHONPATH'] = str(root / 'tools/test_guard') + os.pathsep + '/synthetic/dependencies'
        with patch.dict(os.environ, inherited), \
                patch.object(run_tests, 'audit_guard_available', return_value=False):
            for name in run_tests.SUITES:
                env, isolation = run_tests.suite_environment(name, root, '/tmp/synthetic-workspace',
                    Path('/tmp/synthetic-report'), allow_unguarded=True)
                with self.subTest(suite=name):
                    self.assertEqual(env['KEEL_HOME'], '/tmp/synthetic-workspace')
                    self.assertIn('/synthetic/dependencies', env['PYTHONPATH'])
                    self.assertNotIn('KEEL_AUDIT_TEST_ROOT', env)
                    self.assertNotIn('KEEL_AUDIT_REPORT', env)
                    self.assertNotIn('KEEL_AUDIT_CODE', env)
                    self.assertNotIn(str(root / 'tools/test_guard'), env['PYTHONPATH'].split(os.pathsep))
                    self.assertEqual(isolation['python_audit_hook'],
                                     'not_configured' if name == 'local_profile' else 'unavailable')
                    self.assertEqual(isolation['unguarded_explicitly_allowed'], name != 'local_profile')
                    self.assertFalse(isolation['audit_hook_enforcement_verified'])
                    self.assertFalse(isolation['os_sandbox'])

    def test_existing_file_is_configuration_not_verified_inheritance(self):
        root = run_tests.ROOT
        with patch.object(run_tests, 'audit_guard_available', return_value=True):
            env, isolation = run_tests.suite_environment('core', root,
                '/tmp/synthetic-workspace', Path('/tmp/synthetic-report'))
        self.assertIn(str(root / 'tools/test_guard'), env['PYTHONPATH'].split(os.pathsep))
        self.assertEqual(env['KEEL_AUDIT_TEST_ROOT'], '/tmp/synthetic-workspace')
        self.assertEqual(isolation['python_audit_hook'], 'configured_not_verified')
        self.assertTrue(isolation['audit_guard_file_present'])
        self.assertFalse(isolation['audit_hook_enforcement_verified'])
        self.assertFalse(isolation['unguarded_explicitly_allowed'])

    def test_missing_guard_prevents_child_execution_and_report_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'report'
            with patch.object(run_tests, 'audit_guard_available', return_value=False), \
                    patch.object(run_tests.importlib.util, 'find_spec', return_value=object()), \
                    patch.object(run_tests.subprocess, 'run') as child, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as stop:
                    run_tests.main(['--suite', 'core', '--report-dir', str(report)])
                self.assertEqual(stop.exception.code, 2)
            self.assertIn('audit guard unavailable', error.getvalue())
            child.assert_not_called()
            self.assertFalse(report.exists())

    @staticmethod
    def fake_run(command, **kwargs):
        junit = next(arg.split('=', 1)[1] for arg in command if arg.startswith('--junitxml='))
        Path(junit).write_text('<testsuite><testcase name="synthetic-runner-result"/></testsuite>')
        return type('Result', (), {'returncode': 0})()

    def test_explicit_local_run_records_missing_hook_and_correct_profile_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'report'
            with patch.object(run_tests.subprocess, 'run', side_effect=self.fake_run), \
                    patch.object(run_tests.importlib.util, 'find_spec', return_value=object()), \
                    patch.object(run_tests, 'audit_guard_available', return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_tests.main(['--allow-unguarded', '--report-dir', str(report)]), 0)
            summary = json.loads((report / 'summary.json').read_text())
        self.assertEqual(summary['status'], 'PASS')
        self.assertIn('enforcement not verified', summary['guard'])
        rows = {row['suite']: row for row in summary['suites']}
        self.assertEqual(set(rows), set(run_tests.SUITES))
        self.assertIn('--ignore=tests/test_release_profile.py', rows['core']['command'])
        self.assertIn('tests/test_release_profile.py', rows['local_profile']['command'])
        for name, row in rows.items():
            self.assertEqual(row['counts'], {'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0})
            self.assertEqual(row['isolation']['python_audit_hook'],
                             'not_configured' if name == 'local_profile' else 'unavailable')
            self.assertFalse(row['isolation']['audit_hook_enforcement_verified'])
            if name != 'core':
                self.assertNotIn('--ignore=tests/test_release_profile.py', row['command'])

    def test_profile_alone_does_not_require_missing_guard_override(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'report'
            with patch.object(run_tests.subprocess, 'run', side_effect=self.fake_run), \
                    patch.object(run_tests.importlib.util, 'find_spec', return_value=object()), \
                    patch.object(run_tests, 'audit_guard_available', return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_tests.main(['--suite', 'local_profile', '--report-dir', str(report)]), 0)
            summary = json.loads((report / 'summary.json').read_text())
        self.assertEqual(len(summary['suites']), 1)
        self.assertEqual(summary['suites'][0]['isolation']['python_audit_hook'], 'not_configured')
        self.assertFalse(summary['suites'][0]['isolation']['unguarded_explicitly_allowed'])


if __name__ == '__main__':
    unittest.main()
