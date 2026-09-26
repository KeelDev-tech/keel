"""Real extracted local-profile smoke checks; all workspace state is synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import package
from tools.release_profile import capability_report


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_unconfigured_does_not_mean_live_ready(self):
        report = capability_report(self.home)
        self.assertEqual(report['public_board_discovery']['status'], 'UNCONFIGURED')
        self.assertFalse(report['provider_receipt_verification']['verified'])
        self.assertFalse(report['application_submission']['authorized'])
        self.assertFalse(report['publication_authorized'])
        self.assertTrue(report['runtime']['sqlite']['in_memory_transaction_verified'])
        self.assertTrue(report['runtime']['sqlite']['fts5_verified'])
        self.assertTrue(all(report['modules_present'].values()))
        self.assertFalse(report['rendered_browser']['rendered_execution_verified'])
        self.assertFalse(report['worker_limits']['enforced'])
        self.assertFalse(report['statistical_evidence']['improvement_proven'])
        self.assertFalse(report['machine_optimization']['production_integrated'])
        self.assertFalse(report['machine_optimization']['live_authority_cacheable'])
        self.assertFalse(report['machine_optimization']['improvement_measured'])
        self.assertFalse(report['local_machine_runtime']['databases_opened'])
        self.assertFalse(report['local_machine_runtime']['external_action_admission'])
        self.assertFalse(report['local_machine_runtime']['live_integration_verified'])
        self.assertFalse(report['pipeline_benchmark']['production_throughput_measured'])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_configured_sources_are_counted_without_network_or_identity_output(self):
        (self.home / 'data').mkdir()
        (self.home / 'data/sources.json').write_text(json.dumps({'sources': [
            {'ref': 'greenhouse:synthetic-employer', 'enabled': True},
            {'ref': 'lever:synthetic-disabled', 'enabled': False}]}))
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            report = capability_report(self.home)
        sources = report['public_board_discovery']
        self.assertEqual(sources['status'], 'CONFIGURED_NOT_PROBED')
        self.assertEqual(sources['enabled_sources'], 1)
        self.assertFalse(sources['network_checked'])
        self.assertNotIn('synthetic-employer', json.dumps(report))

    def test_invalid_configuration_is_not_no_data(self):
        (self.home / 'data').mkdir()
        path = self.home / 'data/sources.json'
        for document in ['{corrupt', json.dumps({'sources': [
                {'ref': 'greenhouse:example', 'enabled': 'true'}]})]:
            with self.subTest(document=document):
                path.write_text(document)
                self.assertEqual(capability_report(self.home)['public_board_discovery']['status'],
                                 'INVALID_CONFIGURATION')

    def test_unvalidated_platform_is_reported(self):
        with patch('platform.system', return_value='Darwin'):
            self.assertEqual(capability_report(self.home)['runtime']['status'], 'UNVALIDATED_PLATFORM')


class ProfilePackagingTests(unittest.TestCase):
    def test_runtime_and_history_cannot_be_added_to_allowlist(self):
        for name in ('data/private.json', 'audit/history.md', 'exports/bundle.py',
                     'candidates/previous/code.py', 'transfer-review-0.7.0/keel.py',
                     'hidden_files/approvals.json', '.env', 'config/.env.local',
                     'engines/answer_bank.json', 'state.sqlite3', 'identity.key',
                     'keel_memory/private.sqlite3', 'keel_observability/observations.db'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package._release_path(name)

    def test_reviewed_security_source_packages_are_not_runtime_state(self):
        package._release_path('security/data/dlp.py')
        package._release_path('security/ledger/events.py')
        with self.assertRaises(ValueError):
            package._release_path('security/ledger/events.jsonl')

    def test_profile_has_only_explicit_reviewed_paths(self):
        names = json.loads((ROOT / 'release-files.json').read_text())
        self.assertEqual(names, sorted(set(names)))
        self.assertIn('engines/score_roles.py', names)
        self.assertIn('keel_loki/decision_sessions.py', names)
        for required in ('keel_observability/store.py', 'engines/dedupe_index.py',
                         'keel_eval/reliability.py', 'keel_loki/browser_lab.py',
                         'keel_memory/index.py', 'security/execution/resource_limits.py',
                         'keel_eval/trace_conformance.py', 'keel_learning/controller.py',
                         'keel_next/__main__.py', 'keel_machine/graph.py',
                         'keel_machine/cache.py', 'keel_machine/contracts.py',
                         'keel_machine/drift.py', 'keel_machine/adapters.py',
                         'keel_machine/__main__.py', 'docs/MACHINE_SYSTEM.md',
                         'keel_machine/runtime.py', 'keel_muse/coordinator.py',
                         'keel_agent/state.py', 'keel_next/benchmark.py',
                         'docs/MACHINE_RUNTIME.md', 'docs/PIPELINE_SCALE.md',
                         'sample_data/machine-plan.example.json'):
            self.assertIn(required, names)
        self.assertNotIn('README.md', names)  # audit README contains historical assertions
        for name in names:
            self.assertTrue((ROOT / name).is_file(), name)
            package._release_path(name)

    def test_extracted_profile_runs_offline_without_third_party_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            first = package.build(temp / 'first.zip')
            second = package.build(temp / 'second.zip')
            self.assertEqual(first['sha256'], second['sha256'])
            self.assertFalse(first['publication_authorized'])
            self.assertTrue(package.verify(temp / 'first.zip')['verified_integrity'])
            code = temp / 'extracted'
            with zipfile.ZipFile(temp / 'first.zip') as archive:
                archive.extractall(code)  # already verified trusted package bytes
                self.assertFalse(json.loads(archive.read('MANIFEST.json'))['publication_authorized'])
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            env['HOME'] = str(temp / 'synthetic-user')
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            env['KEEL_HOME'] = str(temp / 'cold-api-workspace')
            home = temp / 'synthetic-workspace'

            def cli(*args, expected=0, workspace=home):
                result = subprocess.run([sys.executable, '-S', str(code / 'keel.py'),
                                         '--home', str(workspace), *args], cwd=temp,
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return json.loads(result.stdout)

            cli('init')
            before = cli('doctor', '--capabilities', expected=1)
            self.assertFalse(before['ready_for_local_preparation'])
            for key, value in [('first_name', 'Synthetic'), ('last_name', 'Fixture'),
                               ('email', 'synthetic' + '@' + 'example.invalid')]:
                cli('confirm-answer', '--key', key, '--value', value, '--source', 'synthetic test')
            after = cli('doctor', '--capabilities')
            self.assertTrue(after['ready_for_local_preparation'])
            self.assertFalse(after['provider_verification_available'])
            cli('dashboard')
            cli('supply')
            demo = cli('demo', workspace=temp / 'offline-demo')
            self.assertTrue(all(demo['checks'].values()))
            self.assertEqual(demo['external_network_requests'], 0)
            self.assertFalse(demo['packet']['execution_authorized'])
            # Exercise the shipped scoring and decision adapters from extracted
            # bytes; no source-tree imports, network, or installed packages.
            script = '''
import sys
sys.path.insert(0, sys.argv[1])
from engines.score_roles import score_role, MAXIMA
criterion = {'fact': 'demo', 'operator': 'eq', 'value': True,
             'source_ref': 'synthetic:posting'}
role = {'role_id': 'DEMO-1', 'requirements_reviewed': True,
        'requirements_source_ref': 'synthetic:posting',
        'hard_requirements': [dict(criterion, mandatory=True)],
        'scoring_criteria': {key: [dict(criterion)] for key in MAXIMA
                             if key != 'hard_requirements'}}
profile = {'scoring_facts': {'demo': {'value': True,
                                   'evidence_refs': ['synthetic:profile']}}}
score = score_role(role, profile)
assert score['fit_score'] == 100
assert score['execution_authorized'] is False
assert score['status'] == 'PARKED'
from keel_muse.review import example_snapshot, project_review, validate_projection
snapshot = example_snapshot()
review = project_review(snapshot, now=snapshot['captured_at'], session_minutes=2)
assert review['decision_session']['selected_question_ids'] == ['review-01']
assert validate_projection(review) == review
assert review['execution_authorized'] is False
# All eight lanes are imported from extracted bytes with site packages disabled.
from pathlib import Path
import importlib, json, os
sys.path.insert(0, str(Path(sys.argv[1]) / 'engines'))
for module in ('keel_observability', 'keel_observability.adapters', 'engines.dedupe_index',
               'keel_eval.reliability', 'keel_eval.trace_conformance', 'keel_loki.browser_lab',
               'keel_memory', 'keel_memory.evaluation', 'security.execution.isolation',
               'security.execution.resource_limits', 'security.execution.seccomp_profile',
               'keel_learning', 'keel_next', 'keel_machine', 'keel_machine.common',
               'keel_machine.cache', 'keel_machine.contracts', 'keel_machine.drift',
               'keel_machine.graph', 'keel_machine.builtins', 'keel_machine.adapters',
               'keel_machine.runtime', 'keel_muse.coordinator', 'keel_next.benchmark'):
    loaded = importlib.import_module(module)
    assert Path(loaded.__file__).is_relative_to(Path(sys.argv[1]))
work = Path(sys.argv[2]); work.mkdir(mode=0o700)
from engines.dedupe_index import DedupeIndex, Source
queue = work / 'synthetic-queue.json'
queue.write_text(json.dumps([{'role_id':'fixture', 'company':'Source',
    'title':'Operations Manager', 'posting_url':'https://jobs.lever.co/source/abc', 'status':'READY'}]))
idx = DedupeIndex(work / 'identity.sqlite3', [Source('synthetic', str(queue))])
assert idx.refresh()['complete']
assert idx.check_candidate('Sourcegraph', 'Operations Manager',
    'https://jobs.lever.co/sourcegraph/def')[0] == 'suspect'
assert idx.check_candidate('Source', 'Operations Manager',
    'https://jobs.lever.co/source/abc/apply')[0] == 'duplicate'
from keel_eval.reliability import synthetic_demo
lab = synthetic_demo()
assert lab['report']['execution_authorized'] is False
from keel_loki import browser_lab
from keel_loki.skills import SkillWorkshop
workshop = SkillWorkshop(work / 'browser-workshop')
recipe = workshop.quarantine(browser_lab.demo_trace(10), skill_id='synthetic-form', expires_at=100)
blocked = workshop.qualify_rendered('synthetic-form', 1, now=11,
    expected_source_revision=browser_lab.source_revision())
assert blocked['status'] == 'BLOCKED' and blocked['rendered_browser_verified'] is False
assert all(r['reason'] == 'playwright_not_installed' for r in blocked['rows'])
from security.execution.resource_limits import doctor as limits_doctor
assert limits_doctor()['status'] == 'NOT_CONFIGURED' and limits_doctor()['enforced'] is False
from security.execution.durable import SQLiteAuthority
from keel_eval.trace_conformance import inspect_authority
operator = lambda request, context, now: 'synthetic-operator' if context == 'fixture' else None
authority = SQLiteAuthority.create(work / 'authority.sqlite3', canonical_store_id='fixture',
    operator_validator=operator, operator_context='fixture', clock=lambda:1000)
assert inspect_authority(authority)['status'] == 'CONFORMANT'
from keel_learning import ExperimentRegistry, bounded_confidence_sequence
from keel_learning.controller import digest
pins = {key:'a'*64 for key in ('source_sha256','config_sha256','model_sha256','calibration_sha256')}
plan = {'schema':'keel.learning.experiment.v1', 'experiment_id':'fixture', 'bindings':pins,
 'actions':['a','b'], 'strata':{'fixture':{'logging':{'a':.5,'b':.5},'candidate':{'a':1,'b':0},
 'baseline':{'a':0,'b':1},'predictions':{'a':.5,'b':.5}}}, 'objective':'synthetic_reward',
 'predictor_frozen_at':'2026-01-01T00:00:00Z','predictor_training_sha256':'b'*64,
 'delta':.01,'min_samples':1,'min_ess':1,'min_improvement':0,'synthetic':True}
registry = ExperimentRegistry(work / 'learning.sqlite3', validator=lambda event, proof: proof=='fixture',
    clock=lambda:'2026-01-02T00:00:00Z')
pin=registry.register(plan, proof='fixture')
decision=registry.decide('fixture',expected_pin=pin,decision_id='trial',unit_id='unit',stratum='fixture',
    current_bindings=pins,proof='fixture',synthetic_draw=0)
registry.record_outcome('trial',reward=1,human_minutes=2,executed_action=decision['action'],
    observation_sha256='c'*64,current_bindings=pins,proof='fixture')
evaluation=registry.evaluate('fixture',expected_pin=pin,current_bindings=pins,proof='fixture')
assert evaluation['status']=='HOLD' and 'SYNTHETIC_ONLY' in evaluation['hold_reasons']
registry.close()
assert bounded_confidence_sequence([0,1],lower=0,upper=1,delta=.05)[-1]['n']==2
print(json.dumps({'eight_lane_imports':'PASS','synthetic_api_checks':'PASS','browser':'BLOCKED_OPTIONAL_DEPENDENCY',
                  'kernel_enforcement':'NOT_CONFIGURED','execution_authorized':False}))
'''
            extra = subprocess.run([sys.executable, '-S', '-c', script, str(code), str(temp / 'advanced-api')],
                                   cwd=temp, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(extra.returncode, 0, extra.stdout + extra.stderr)
            for module in ('keel_observability', 'keel_memory', 'keel_learning', 'keel_eval',
                           'keel_loki.browser_lab', 'engines.dedupe_index',
                           'security.execution.resource_limits', 'keel_next', 'keel_machine'):
                result = subprocess.run([sys.executable, '-S', '-m', module, '--help'], cwd=code,
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, module + ': ' + result.stdout + result.stderr)
            integrated = subprocess.run([sys.executable, '-S', '-m', 'keel_next', 'demo',
                                         '--home', str(temp / 'next-demo')], cwd=code,
                                        env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(integrated.returncode, 0, integrated.stdout + integrated.stderr)
            integrated_report = json.loads(integrated.stdout)
            self.assertFalse(integrated_report['execution_authorized'])
            self.assertEqual(integrated_report['status'], 'DEMO_PASSED')
            self.assertTrue(all(integrated_report['checks'].values()))
            # Run the actual module entry point with third-party packages absent
            # and Python socket creation rejected. This regression guard is
            # not an operating-system network sandbox.
            machine_runner = """
import runpy, socket

def no_network(*args, **kwargs):
    raise AssertionError('machine synthetic demo attempted network access')

socket.socket = no_network
socket.create_connection = no_network
runpy.run_module('keel_machine', run_name='__main__')
"""
            machine = subprocess.run([sys.executable, '-S', '-c', machine_runner, 'demo',
                                      '--home', str(temp / 'machine-demo')], cwd=code,
                                     env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(machine.returncode, 0, machine.stdout + machine.stderr)
            machine_report = json.loads(machine.stdout)
            self.assertEqual(machine_report['status'], 'DEMO_PASSED')
            self.assertEqual(set(machine_report['checks']), {
                'initial_graph_computed', 'unchanged_graph_reused',
                'only_changed_branch_recomputed', 'live_hold_blocks_cache_hit',
                'changed_contract_held', 'injected_failure_increase_detected'})
            self.assertTrue(all(machine_report['checks'].values()))
            for metric in ('model_calls', 'network_calls', 'external_actions'):
                self.assertEqual(machine_report[metric], 0)
            for flag in ('execution_authorized', 'production_deployed', 'paid_services_required'):
                self.assertFalse(machine_report[flag])
            capabilities = subprocess.run([sys.executable, '-S', '-m', 'keel_machine', 'capabilities'],
                                          cwd=code, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(capabilities.returncode, 0, capabilities.stdout + capabilities.stderr)
            self.assertFalse(json.loads(capabilities.stdout)['production_integrated'])

            # The durable host is exercised through separate processes, so
            # result persistence and passive CLI reads cannot rely on memory.
            offline_runner = """
import runpy, socket, sys
# Load ssl's socket subclass before replacing socket construction.
# Importing this standard-library transport does not issue a request.
import urllib.request

def no_network(*args, **kwargs):
    raise AssertionError('offline release check attempted network access')

socket.socket = no_network
socket.create_connection = no_network
module = sys.argv.pop(1)
runpy.run_module(module, run_name='__main__')
"""
            def module_cli(module, *args, expected=0):
                completed = subprocess.run(
                    [sys.executable, '-S', '-c', offline_runner, module, *args],
                    cwd=code, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(completed.returncode, expected,
                                 completed.stdout + completed.stderr)
                return json.loads(completed.stdout)

            runtime_home = str(temp / 'runtime-workspace')
            module_cli('keel_machine', 'init', '--home', runtime_home,
                       '--workspace-id', 'synthetic-runtime', '--account-id', 'local-owner')
            plan = {'schema': 'keel.machine.graph.v1', 'run_id': 'first',
                    'account_id': 'local-owner', 'scope': 'synthetic-analysis',
                    'purpose': 'test', 'max_compute_nodes': 1, 'max_seconds': 30,
                    'nodes': [{'id': 'tokens', 'operation': 'tokenize_text',
                               'arguments': {'text': 'Synthetic local computation.'},
                               'config': {}, 'needs': [], 'revisions': {}}],
                    'outputs': ['tokens']}
            plan_path = temp / 'synthetic-plan.json'
            def submit(run_id):
                plan['run_id'] = run_id
                plan_path.write_text(json.dumps(plan))
                return module_cli('keel_machine', 'submit', '--home', runtime_home,
                                  '--plan', str(plan_path))

            self.assertEqual(submit('first')['status'], 'QUEUED')
            self.assertEqual(submit('first')['status'], 'DUPLICATE')
            module_cli('keel_machine', 'work', '--home', runtime_home, '--max-tasks', '1')
            first_result = module_cli('keel_machine', 'result', '--home', runtime_home,
                                      '--run-id', 'first')
            self.assertEqual(first_result['status'], 'COMPLETED')
            self.assertEqual(first_result['report']['computed_nodes'], 1)
            self.assertFalse(first_result['report']['execution_authorized'])
            first_status = module_cli('keel_machine', 'status', '--home', runtime_home)
            second_status = module_cli('keel_machine', 'status', '--home', runtime_home)
            self.assertEqual(first_status['coordinator_generation'],
                             second_status['coordinator_generation'])
            self.assertEqual(submit('repeat')['status'], 'QUEUED')
            module_cli('keel_machine', 'work', '--home', runtime_home, '--max-tasks', '1')
            repeated_result = module_cli('keel_machine', 'result', '--home', runtime_home,
                                         '--run-id', 'repeat')
            self.assertEqual(repeated_result['status'], 'COMPLETED')
            self.assertEqual(repeated_result['report']['computed_nodes'], 0)
            self.assertEqual(repeated_result['report']['cache_hits'], 1)
            stopped = module_cli('keel_machine', 'hold', '--home', runtime_home,
                                 '--run-id', 'repeat', '--reason', 'synthetic_hold', expected=3)
            self.assertEqual(stopped['status'], 'HELD')
            held_result = module_cli('keel_machine', 'result', '--home', runtime_home,
                                     '--run-id', 'repeat', expected=3)
            self.assertEqual(held_result['status'], 'HELD')
            self.assertIsNone(held_result.get('report'))

            self.assertEqual(module_cli(
                'keel_machine', 'submit', '--home', runtime_home,
                '--plan', str(code / 'sample_data/machine-plan.example.json'))['status'], 'QUEUED')
            module_cli('keel_machine', 'work', '--home', runtime_home, '--max-tasks', '1')
            shipped_example = module_cli('keel_machine', 'result', '--home', runtime_home,
                                         '--run-id', 'document-compare-1')
            self.assertEqual(shipped_example['status'], 'COMPLETED')
            self.assertEqual(shipped_example['report']['computed_nodes'], 3)
            self.assertFalse(shipped_example['report']['execution_authorized'])

            benchmark = module_cli('keel_next', 'benchmark', '--home', str(temp / 'pipeline-benchmark'),
                                   '--boards', '3', '--jobs-per-board', '8', '--max-new', '12')
            self.assertEqual(benchmark['status'], 'BENCHMARK_PASSED')
            self.assertTrue(benchmark['synthetic'])
            self.assertTrue(all(benchmark['checks'].values()))
            self.assertFalse(benchmark['execution_authorized'])
            self.assertFalse(benchmark['production_speedup_proven'])
            self.assertFalse(benchmark['memory']['process_rss_measured'])
            self.assertEqual(benchmark['counts']['workload_postings'], 24)
            self.assertEqual(benchmark['counts']['first_added'], 12)
            self.assertEqual(benchmark['counts']['verified'], 24)
            for metric in ('model_calls', 'network_calls', 'external_actions'):
                self.assertEqual(benchmark[metric], 0)



if __name__ == '__main__':
    unittest.main()
