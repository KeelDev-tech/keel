"""Runner routing and reporting checks; child suite execution is mocked."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import run_tests


class RunnerProfileTests(unittest.TestCase):
    def test_only_named_profile_omits_inherited_guard(self):
        root = run_tests.ROOT
        inherited = {key: 'inherited-value' for key in (
            'KEEL_AUDIT_TEST_ROOT', 'KEEL_AUDIT_REPORT', 'KEEL_AUDIT_CODE')}
        inherited['PYTHONPATH'] = str(root / 'tools/test_guard') + os.pathsep + '/synthetic/dependencies'
        with patch.dict(os.environ, inherited):
            for name in run_tests.SUITES:
                env, isolation = run_tests.suite_environment(name, root, '/tmp/synthetic-workspace',
                                                            Path('/tmp/synthetic-report'))
                with self.subTest(suite=name):
                    self.assertEqual(env['KEEL_HOME'], '/tmp/synthetic-workspace')
                    self.assertIn('/synthetic/dependencies', env['PYTHONPATH'])
                    if name == 'local_profile':
                        self.assertNotIn('KEEL_AUDIT_TEST_ROOT', env)
                        self.assertNotIn(str(root / 'tools/test_guard'), env['PYTHONPATH'].split(os.pathsep))
                        self.assertEqual(isolation['python_audit_hook'], 'not inherited')
                    else:
                        self.assertEqual(env['KEEL_AUDIT_TEST_ROOT'], '/tmp/synthetic-workspace')
                        self.assertEqual(env['KEEL_AUDIT_REPORT'], '/tmp/synthetic-report')
                        self.assertIn(str(root / 'tools/test_guard'), env['PYTHONPATH'].split(os.pathsep))
                        self.assertEqual(isolation['python_audit_hook'], 'inherited')

    def test_default_run_includes_profile_and_excludes_it_only_from_core(self):
        def fake_run(command, **kwargs):
            junit = next(arg.split('=', 1)[1] for arg in command if arg.startswith('--junitxml='))
            Path(junit).write_text('<testsuite><testcase name="synthetic-runner-result"/></testsuite>')
            return type('Result', (), {'returncode': 0})()

        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'report'
            with patch.object(run_tests.subprocess, 'run', side_effect=fake_run), \
                    patch.object(run_tests.importlib.util, 'find_spec', return_value=object()):
                self.assertEqual(run_tests.main(['--report-dir', str(report)]), 0)
            summary = json.loads((report / 'summary.json').read_text())
        self.assertEqual(summary['status'], 'PASS')
        rows = {row['suite']: row for row in summary['suites']}
        self.assertEqual(set(rows), set(run_tests.SUITES))
        self.assertIn('local_profile', rows)
        self.assertIn('--ignore=tests/test_release_profile.py', rows['core']['command'])
        self.assertIn('tests/test_release_profile.py', rows['local_profile']['command'])
        for name, row in rows.items():
            self.assertEqual(row['counts'], {'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0})
            self.assertEqual(row['isolation']['python_audit_hook'],
                             'not inherited' if name == 'local_profile' else 'inherited')
            if name != 'core':
                self.assertNotIn('--ignore=tests/test_release_profile.py', row['command'])


if __name__ == '__main__':
    unittest.main()
