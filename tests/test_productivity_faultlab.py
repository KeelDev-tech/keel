"""Actual subprocess deaths qualify fixed controller windows, never a live host."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/run_productivity_faultlab.py'
OFFLINE = '''
import runpy, sys
def deny_network(event, args):
    if event.startswith("socket."):
        raise AssertionError("faultlab launcher attempted network access")
sys.addaudithook(deny_network)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def load_tool():
    spec = importlib.util.spec_from_file_location('faultlab_test_import', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProductivityFaultlabTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-productivity-faultlab-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.existing = self.root / 'existing-home'
        self.existing.mkdir()
        (self.existing / 'sentinel.json').write_text('{"do_not_modify":true}\n')

    def cli(self, output, *extra):
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'LANG', 'LC_ALL'}}
        environment.update(KEEL_HOME=str(self.existing), PYTHONPATH=str(self.existing),
                           JOB_PIPELINE_HTTP_COOLDOWN_DIR=str(self.existing / 'cooldowns'))
        return subprocess.run([sys.executable, '-B', '-c', OFFLINE, str(TOOL),
                               '--out', str(output), *extra], env=environment, cwd=self.root,
                              capture_output=True, text=True, timeout=60)

    def test_five_real_sigkills_and_two_fresh_restarts_preserve_actual_evidence(self):
        output = self.root / 'new faultlab'
        result = self.cli(output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, json.loads((output / 'report.json').read_text()))
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(len(report['cases']), 5)
        self.assertTrue(report['source_files_unchanged'])
        self.assertEqual(set(report['source_fingerprints']), set(load_tool().BUILD_FILES))
        for case, item in report['cases'].items():
            with self.subTest(case=case):
                self.assertEqual(item['crash']['returncode'], -signal.SIGKILL)
                self.assertTrue(all(item['checks'].values()))
                self.assertEqual(len(item['restarts']), 2)
                self.assertTrue(all(child['returncode'] == 0 and not child['timed_out']
                                    for child in item['restarts']))
                cut = json.loads((output / case / 'cutpoint.json').read_text())
                first = json.loads((output / case / 'restart-one.json').read_text())
                second = json.loads((output / case / 'restart-two.json').read_text())
                self.assertEqual(cut['before'], first['before'])
                self.assertEqual(first['after'], second['before'])
                self.assertEqual(second['before'], second['after'])
                self.assertTrue(all(first['checks'].values()))
                self.assertTrue(all(second['checks'].values()))
                self.assertTrue(second['after']['protected_rows_intact'])
                self.assertTrue(second['after']['application_ledger_empty'])
                self.assertEqual(second['after']['network_attempts_denied'], 0)
                self.assertEqual(sum(second['after']['ledger']['requests_by_state'].values()), 1)
                self.assertIsNone(second['after']['request']['usage']['external_credit_micros'])
                if case == 'after_reservation':
                    self.assertIsNone(cut['before']['retained_run'])
                    self.assertEqual(first['after']['synthetic_fetches'] - first['before']['synthetic_fetches'], 1)
                elif case == 'before_dispatch':
                    self.assertEqual(second['after']['request']['state'], 'CANCELLED')
                    self.assertEqual(second['after']['synthetic_fetches'], 0)
                    self.assertEqual(second['after']['scope']['reserved']['calls'], 0)
                elif case == 'after_queue_commit':
                    self.assertEqual(second['after']['request']['state'], 'DISPATCHED')
                    self.assertEqual(second['after']['retained_run']['phase'], 'INTENT')
                    self.assertEqual(second['after']['scope']['reserved']['calls'], 2)
                    self.assertEqual(second['after']['scope']['used']['calls'], 0)
                else:
                    self.assertEqual(second['after']['retained_run']['phase'], 'COMPLETE')
                    self.assertEqual(second['after']['scope']['used']['calls'], 1)
        for name in ('live_host_qualified', 'production_performance_claim',
                     'execution_authorized', 'submission_authorized', 'paid_services_required'):
            self.assertIs(report[name], False)
        self.assertIsNone(report['actual_credit_savings'])
        self.assertEqual((report['network_calls'], report['model_calls'], report['submissions']), (0, 0, 0))
        self.assertEqual((self.existing / 'sentinel.json').read_text(), '{"do_not_modify":true}\n')
        self.assertEqual([path.name for path in self.existing.iterdir()], ['sentinel.json'])
        self.assertFalse(list(self.root.rglob('__pycache__')))
        for path in [output, *output.rglob('*')]:
            self.assertFalse(path.is_symlink())
            self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o077, str(path))

    def test_failed_or_missing_crash_evidence_cannot_report_pass(self):
        module = load_tool()
        for result in ({'returncode': 0, 'timed_out': False, 'diagnostic_sha256': '0' * 64},
                       {'returncode': None, 'timed_out': True, 'diagnostic_sha256': None},
                       {'returncode': -signal.SIGKILL, 'timed_out': False, 'diagnostic_sha256': '0' * 64}):
            with self.subTest(result=result):
                output = self.root / ('failed-' + str(len(list(self.root.iterdir()))))
                with patch.object(module, '_child', return_value=result) as child:
                    report = module.run(output)
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual(child.call_count, 5)
                self.assertTrue(all(not all(item['checks'].values()) for item in report['cases'].values()))
                self.assertEqual(report, json.loads((output / 'report.json').read_text()))

    def test_timeout_is_failed_and_isolated_subprocess_has_a_bound(self):
        module = load_tool()
        error = subprocess.TimeoutExpired(cmd='synthetic', timeout=module.WORKER_TIMEOUT)
        with patch.object(module.subprocess, 'run', side_effect=error) as run:
            result = module._child(self.root, 'after_recorded', 'crash', 'a' * 64)
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['returncode'])
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertEqual(run.call_args.args[0][1:4], ['-I', '-B', '-S'])
        self.assertNotIn('KEEL_HOME', run.call_args.kwargs['env'])
        self.assertNotIn('PYTHONPATH', run.call_args.kwargs['env'])

    def test_existing_output_unsafe_parent_and_direct_worker_are_refused(self):
        empty = self.root / 'empty'
        empty.mkdir()
        linked = self.root / 'linked'
        linked.symlink_to(self.existing, target_is_directory=True)
        for output in (empty, self.existing, linked, self.root / 'absent' / 'out', linked / 'out'):
            with self.subTest(output=str(output)):
                result = self.cli(output)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for mode in ('crash', 'restart-one', 'restart-two'):
            result = self.cli(self.existing, '--_worker', mode, '--_case', 'after_recorded')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual([path.name for path in self.existing.iterdir()], ['sentinel.json'])
        self.assertEqual(list(empty.iterdir()), [])
        self.assertFalse((self.root / 'absent').exists())

    def test_import_has_no_engine_environment_path_or_process_side_effect(self):
        environment, path = dict(os.environ), list(sys.path)
        names = ('keel_paths', 'queue_io', 'pipeline_service', 'productivity_service')
        engines = {name: sys.modules.get(name) for name in names}
        with patch.object(subprocess, 'run', side_effect=AssertionError('no process on import')):
            load_tool()
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(sys.path, path)
        self.assertEqual({name: sys.modules.get(name) for name in names}, engines)


if __name__ == '__main__':
    unittest.main()
