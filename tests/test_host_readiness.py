"""Offline host observations must preserve all durable application state."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import host_readiness
import keel_paths
import log_event
import pipeline_service as pipeline
import productivity_service as productivity
import queue_io
import source_scheduler
from keel_efficiency.ledger import ResourceLedger
from safe_io import atomic_json


class HostReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='keel-host-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 1800000000.0
        self.queue = self.home / 'data/queues/standard-queue.json'
        for name in pipeline.QUEUES:
            atomic_json(self.home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        atomic_json(self.home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        for context in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
                        patch.object(log_event, 'EVENTS', str(self.home / 'data/telemetry/events.jsonl')),
                        patch.object(keel_paths, 'HOME', str(self.home)),
                        patch.dict(os.environ, {'KEEL_HOME': str(self.home),
                            'JOB_PIPELINE_HTTP_COOLDOWN_DIR': str(self.home / 'hidden_files/http-cooldowns')})):
            self.context.enter_context(context)
        self.ledger = ResourceLedger(self.home / 'budget.sqlite3')
        self.ledger.create_scope('shared', {'calls': 100, 'compute_ms': 1000000})

    def inspect(self, **kwargs):
        options = {'budget_ledger': self.ledger.path, 'budget_scope': 'shared', 'clock': lambda: self.now}
        options.update(kwargs)
        return host_readiness.inspect(self.home, **options)

    def check(self, report, name):
        return next(item for item in report['checks'] if item['id'] == name)

    def business_files(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob('*') if path.is_file()
                and not path.name.endswith(('-wal', '-shm'))}

    def source(self):
        atomic_json(self.home / 'data/sources.json', {'sources': [
            {'ref': 'greenhouse:synthetic', 'company_label': 'Synthetic Example', 'enabled': True}]})

    def row(self, number=1):
        return {'role_id': 'synthetic-' + str(number), 'status': 'PARKED-PENDING-VERIFICATION',
                'ats_url': 'https://boards.greenhouse.io/synthetic/jobs/' + str(number)}

    def cycle(self, name='retained'):
        return productivity.run_once(self.home, run_id=name, ledger=self.ledger, scope_id='shared',
                                     live=True, clock=lambda: self.now)

    def test_empty_explicit_documents_pass_without_creating_controller_or_locks(self):
        before = self.business_files()
        with (patch.object(pipeline, 'supply_report', side_effect=AssertionError('writer lock forbidden')),
              patch.object(productivity, 'status', side_effect=AssertionError('writer lock forbidden'))):
            report = self.inspect()
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['local_checks_passed'])
        self.assertEqual(report['suggested_stage']['stage'], 'idle')
        self.assertEqual(report['suggested_stage']['reason'], 'no_registered_sources')
        self.assertEqual(before, self.business_files())
        self.assertFalse((self.home / 'data/productivity').exists())
        self.assertFalse((self.home / 'hidden_files').exists())
        self.assertFalse(report['coherent_cross_store_snapshot'])
        self.assertFalse(report['production_readiness_established'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['submission_authorized'])
        self.assertTrue(all(row['status'] == 'UNKNOWN' for row in report['host_qualifications']))

    def test_registered_source_observation_does_not_dispatch(self):
        self.source()
        with patch.object(pipeline.PublicBoardReader, '_json', side_effect=AssertionError('no dispatch')):
            report = self.inspect()
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['suggested_stage']['stage'], 'discover')
        self.assertFalse(report['suggested_stage']['dispatch_authorized'])
        self.assertTrue(report['suggested_stage']['admission_recheck_required'])

    def test_supported_posting_can_suggest_verify_but_holds_remain_held(self):
        held = {**self.row(2), 'human_hold': True}
        atomic_json(self.queue, [self.row(), held])
        before = self.queue.read_bytes()
        report = self.inspect()
        self.assertEqual(report['suggested_stage']['stage'], 'verify')
        self.assertEqual(report['supply']['states']['held_active_or_terminal'], 1)
        self.assertEqual(self.queue.read_bytes(), before)

    def test_missing_workspace_does_not_create_it(self):
        missing = self.home / 'absent'
        report = host_readiness.inspect(missing)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertFalse(missing.exists())

    def test_missing_required_documents_are_not_silently_empty(self):
        paths = [self.home / 'data/queues' / (name + '-queue.json') for name in pipeline.QUEUES]
        paths += [self.home / 'data/application-ledger.json', self.home / 'data/sources.json']
        for path in paths:
            with self.subTest(document=path.name):
                saved = path.read_bytes()
                path.unlink()
                report = self.inspect()
                self.assertEqual(self.check(report, 'input_documents')['status'], 'BLOCKED')
                self.assertFalse(path.exists())
                path.write_bytes(saved)

    def test_invalid_json_rejected_without_exception_content(self):
        for payload in (b'{"PRIVATE-SEED":1,"PRIVATE-SEED":2}', b'[NaN]', b'{"rows":"PRIVATE-SEED"}'):
            with self.subTest(payload=payload):
                self.queue.write_bytes(payload)
                report = self.inspect()
                self.assertEqual(self.check(report, 'input_documents')['status'], 'BLOCKED')
                self.assertNotIn('PRIVATE-SEED', json.dumps(report))

    def test_symlinked_input_is_rejected_before_external_read(self):
        external = self.home / 'secret.json'
        external.write_text('PRIVATE-SEED')
        self.queue.unlink()
        self.queue.symlink_to(external)
        report = self.inspect()
        self.assertEqual(self.check(report, 'input_documents')['status'], 'BLOCKED')
        self.assertNotIn('PRIVATE-SEED', json.dumps(report))

    def test_symlinked_parent_directory_rejected(self):
        directory = self.home / 'data/queues'
        actual = self.home / 'queue-store'
        directory.rename(actual)
        directory.symlink_to(actual, target_is_directory=True)
        report = self.inspect()
        self.assertEqual(self.check(report, 'input_documents')['status'], 'BLOCKED')

    def test_hardlinked_and_nonregular_inputs_rejected(self):
        extra = self.home / 'alias.json'
        os.link(self.queue, extra)
        self.assertEqual(self.check(self.inspect(), 'input_documents')['status'], 'BLOCKED')
        self.queue.unlink()
        os.mkfifo(self.queue)
        self.assertEqual(self.check(self.inspect(), 'input_documents')['status'], 'BLOCKED')

    def test_traversal_workspace_and_budget_are_rejected(self):
        report = host_readiness.inspect(str(self.home) + '/data/../')
        self.assertEqual(self.check(report, 'workspace')['status'], 'BLOCKED')
        report = self.inspect(budget_ledger='data/../budget.sqlite3')
        self.assertEqual(self.check(report, 'resource_ledger')['status'], 'BLOCKED')

    def test_duplicate_queue_roles_and_postings_block_even_on_held_rows(self):
        for rows_ in ([self.row(), {**self.row(2), 'role_id': self.row()['role_id']}],
                      [self.row(), {**self.row(), 'role_id': 'other', 'human_hold': True}]):
            with self.subTest(rows=rows_):
                atomic_json(self.queue, rows_)
                report = self.inspect()
                self.assertEqual(self.check(report, 'queue_identities')['status'], 'BLOCKED')

    def test_missing_and_nonhashable_roles_are_sanitized_blockers(self):
        for row in ({'ats_url': self.row()['ats_url']}, {'role_id': ['PRIVATE-SEED']}):
            with self.subTest(row=row):
                atomic_json(self.queue, [row])
                report = self.inspect()
                self.assertFalse(report['local_checks_passed'])
                self.assertNotIn('PRIVATE-SEED', json.dumps(report))

    def test_duplicate_or_malformed_source_registry_blocks(self):
        source = {'ref': 'greenhouse:synthetic', 'enabled': True}
        for sources in ([source, source], [{**source, 'enabled': 1}], [{'ref': '../PRIVATE-SEED'}]):
            with self.subTest(sources=sources):
                atomic_json(self.home / 'data/sources.json', {'sources': sources})
                self.assertEqual(self.check(self.inspect(), 'input_documents')['status'], 'BLOCKED')

    def test_input_byte_and_row_limits_fail_closed(self):
        atomic_json(self.queue, [self.row()])
        with patch.object(host_readiness, 'MAX_DOCUMENT_BYTES', 1):
            # The read helper default is deliberately stable; total budget also bounds every read.
            with patch.object(host_readiness, 'MAX_TOTAL_BYTES', 1):
                self.assertFalse(self.inspect()['local_checks_passed'])
        with patch.object(host_readiness, 'MAX_ROWS', 0):
            self.assertFalse(self.inspect()['local_checks_passed'])

    def test_runtime_binding_mismatch_and_demo_boundary_block(self):
        with patch.dict(os.environ, {'KEEL_HOME': str(self.home / 'other')}):
            self.assertEqual(self.check(self.inspect(), 'runtime_binding')['status'], 'BLOCKED')
        atomic_json(self.home / 'DEMO_ONLY.json', {})
        self.assertEqual(self.check(self.inspect(), 'demo_boundary')['status'], 'BLOCKED')

    def test_missing_budget_and_partial_pairs_never_initialize_a_ledger(self):
        missing = self.home / 'uncreated/ledger.sqlite3'
        for ledger, scope in ((missing, 'shared'), (None, None), (None, 'shared'), (self.ledger, None)):
            with self.subTest(scope=scope):
                report = self.inspect(budget_ledger=ledger, budget_scope=scope)
                self.assertEqual(self.check(report, 'resource_ledger')['status'], 'BLOCKED')
        self.assertFalse(missing.parent.exists())

    def test_existing_ledger_object_is_reopened_without_reservation(self):
        before = self.ledger.snapshot()
        with patch.object(ResourceLedger, '__init__', side_effect=AssertionError('initializer forbidden')):
            report = self.inspect(budget_ledger=self.ledger)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(before, self.ledger.snapshot())
        self.assertFalse(report['budget']['reservation_created'])

    def test_ancestor_budget_can_block_a_well_funded_child(self):
        self.ledger.create_scope('small-root', {'calls': 1, 'compute_ms': 1000000})
        self.ledger.create_scope('child', {'calls': 100, 'compute_ms': 1000000}, parent_id='small-root')
        report = self.inspect(budget_scope='child')
        self.assertEqual(report['budget']['available']['calls'], 1)
        self.assertEqual(report['budget']['ancestor_count'], 1)
        self.assertEqual(self.check(report, 'default_cycle_budget')['status'], 'BLOCKED')

    def test_requested_limits_use_exact_reservation_and_preserve_default_diagnostics(self):
        self.ledger.create_scope('small', {'calls': 1, 'compute_ms': 1001})
        before = self.business_files()
        report = self.inspect(budget_scope='small', max_requests=1, timeout=1.0001)
        self.assertTrue(report['local_checks_passed'])
        self.assertEqual(report['budget']['requested_cycle_estimate']['compute_ms'], 1001)
        self.assertTrue(report['budget']['requested_cycle_fits_observed_budget'])
        self.assertFalse(report['budget']['default_cycle_fits_observed_budget'])
        self.assertEqual(report['budget']['default_cycle_estimate']['calls'], 8)
        self.assertEqual(self.check(report, 'requested_cycle_budget')['status'], 'PASS')
        self.assertEqual(self.business_files(), before)
        larger = self.inspect(budget_scope='small', max_requests=1, timeout=1.0011)
        self.assertFalse(larger['local_checks_passed'])
        self.assertEqual(larger['budget']['requested_cycle_estimate']['compute_ms'], 1002)

    def test_requested_policy_changes_observed_choice_without_queue_mutation(self):
        self.source()
        row = {**self.row(), 'human_hold': True}
        atomic_json(self.queue, [row])
        before = self.business_files()
        self.assertEqual(self.inspect()['suggested_stage']['stage'], 'discover')
        report = self.inspect(backlog_limit=1, target_verified=7)
        self.assertEqual(report['suggested_stage']['stage'], 'idle')
        self.assertEqual(report['suggested_stage']['reason'], 'queue_backlog_limit')
        self.assertEqual(report['suggested_stage']['policy'], {'backlog_limit': 1, 'target_verified': 7})
        self.assertEqual(self.business_files(), before)

    def test_invalid_requested_options_block_before_reading_workspace(self):
        invalid = ({'max_requests': True}, {'max_requests': 0}, {'max_requests': 1001},
                   {'timeout': 0}, {'timeout': float('nan')}, {'timeout': 901},
                   {'target_verified': 0}, {'target_verified': 10001}, {'backlog_limit': False})
        for options in invalid:
            with self.subTest(options=options), patch.object(host_readiness, '_root',
                    side_effect=AssertionError('workspace read before option validation')):
                report = self.inspect(**options)
                self.assertFalse(report['local_checks_passed'])
                self.assertIsNone(report['suggested_stage'])
                self.assertEqual(self.check(report, 'requested_policy')['status'], 'BLOCKED')

    def test_unknown_scope_and_invalid_stored_vectors_block(self):
        self.assertEqual(self.check(self.inspect(budget_scope='absent'), 'resource_ledger')['status'], 'BLOCKED')
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('UPDATE efficiency_scopes SET used_json=? WHERE scope_id=?',
                       (json.dumps({name: -1 for name in host_readiness.RESOURCES}), 'shared'))
        self.assertEqual(self.check(self.inspect(), 'resource_ledger')['status'], 'BLOCKED')

    def test_legacy_history_is_observed_without_migration(self):
        path = self.home / 'data/productivity/journal.json'
        atomic_json(path, productivity._blank(self.now))
        before = self.business_files()
        report = self.inspect()
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['history']['storage'], 'legacy_requires_migration')
        self.assertEqual(before, self.business_files())
        self.assertFalse(path.with_name('history.sqlite3').exists())

    def test_existing_history_and_accounting_checked_without_changing_them(self):
        self.cycle()
        before = self.business_files()
        report = self.inspect()
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['history']['retained_runs'], 1)
        self.assertEqual(self.check(report, 'history_accounting')['status'], 'PASS')
        self.assertEqual(before, self.business_files())

    def test_pending_controller_run_blocks_and_is_not_recovered(self):
        save = productivity._save
        def stop_before_receipt(path, state, now):
            if any(row['phase'] == 'RECORDED' for row in state['runs'].values()):
                raise OSError('PRIVATE-SEED')
            return save(path, state, now)
        with patch.object(productivity, '_save', side_effect=stop_before_receipt):
            with self.assertRaises(OSError):
                self.cycle()
        before = self.business_files()
        report = self.inspect()
        self.assertEqual(report['history']['pending_runs'], 1)
        self.assertEqual(self.check(report, 'productivity_recovery')['status'], 'BLOCKED')
        self.assertEqual(before, self.business_files())

    def test_replaced_ledger_is_detected_by_existing_history_anchor(self):
        self.cycle()
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('UPDATE efficiency_meta SET instance_id=?', ('a' * 32,))
        report = self.inspect()
        self.assertEqual(self.check(report, 'history_accounting')['status'], 'BLOCKED')

    def test_history_missing_manifest_or_database_never_bootstraps(self):
        self.cycle()
        folder = self.home / 'data/productivity'
        for name in ('history.json', 'history.sqlite3'):
            with self.subTest(missing=name):
                saved = (folder / name).read_bytes()
                (folder / name).unlink()
                report = self.inspect()
                self.assertEqual(self.check(report, 'productivity_history')['status'], 'BLOCKED')
                self.assertFalse((folder / name).exists())
                (folder / name).write_bytes(saved)

    def test_optional_history_and_scheduler_symlinks_block(self):
        outside = self.home / 'outside.json'
        outside.write_text('PRIVATE-SEED')
        scheduler = self.home / 'data/source-scheduler.json'
        scheduler.symlink_to(outside)
        self.assertEqual(self.check(self.inspect(), 'source_scheduler')['status'], 'BLOCKED')
        scheduler.unlink()
        folder = self.home / 'data/productivity'
        folder.mkdir()
        (folder / 'journal.json').symlink_to(outside)
        self.assertEqual(self.check(self.inspect(), 'productivity_history')['status'], 'BLOCKED')

    def test_scheduler_inflight_is_not_recovered(self):
        path = self.home / 'data/source-scheduler.json'
        state = source_scheduler._load(path, self.now)
        state['sources']['greenhouse:synthetic'] = {'last_attempt': 0, 'next_at': 0,
                                                    'failures': 0, 'last_status': None, 'last_added': 0}
        state['inflight'] = {'run_id': 'a' * 32, 'selected': ['greenhouse:synthetic'],
                             'completed': [], 'binding': 'b' * 64, 'started_at': self.now}
        atomic_json(path, state)
        before = path.read_bytes()
        self.assertEqual(self.check(self.inspect(), 'source_scheduler')['status'], 'BLOCKED')
        self.assertEqual(before, path.read_bytes())

    def test_malformed_clock_and_history_clock_regression_block(self):
        for stamp in (-1, True, float('nan')):
            self.assertEqual(self.check(self.inspect(clock=lambda: stamp), 'clock')['status'], 'BLOCKED')
        atomic_json(self.home / 'data/productivity/journal.json', productivity._blank(self.now + 10))
        self.assertEqual(self.check(self.inspect(), 'productivity_history')['status'], 'BLOCKED')

    def test_private_strings_and_exceptions_never_enter_report(self):
        private = 'PRIVATE-SEED@example.invalid'
        row = {**self.row(), 'role_id': private, 'applicant_name': private,
               'unresolved': [private], 'unused_url': 'https://example.invalid/' + private}
        atomic_json(self.queue, [row])
        atomic_json(self.home / 'data/sources.json', {'sources': [
            {'ref': 'greenhouse:synthetic', 'enabled': True, 'company_label': private}]})
        with patch.object(productivity, '_bound_workspace', side_effect=ValueError(private)):
            serialized = json.dumps(self.inspect())
        self.assertNotIn(private, serialized)
        self.assertNotIn(str(self.home), serialized)
        self.assertNotIn('https://', serialized)

    def test_network_audit_trap_and_application_write_trap_in_fresh_process(self):
        self.source()
        script = '''
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'engines'))
import host_readiness
def guard(event, args):
    if event.startswith('socket.') or event.startswith('subprocess.'):
        raise AssertionError('network/process forbidden')
    if event == 'open':
        path, mode, flags = args
        if flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR):
            raise AssertionError('application write forbidden')
sys.addaudithook(guard)
result = host_readiness.inspect(os.environ['KEEL_HOME'],
    budget_ledger=str(Path(os.environ['KEEL_HOME']) / 'budget.sqlite3'),
    budget_scope='shared', clock=lambda: 1800000000.0)
assert result['status'] == 'PASS', result
print(json.dumps(result))
'''
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-B', '-c', script], env=env,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['network_calls'], 0)


if __name__ == '__main__':
    unittest.main()
