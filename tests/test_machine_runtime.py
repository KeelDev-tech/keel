"""Real local runtime lifecycle, interruption, holds and inert authority boundaries."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from keel_machine.builtins import demo_plan
from keel_machine.common import MachineError, canonical, digest
from keel_machine.runtime import LocalGraphRuntime, MAX_RUNS


class MachineRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / 'runtime'
        self.now = [100.0]
        self.runtime = LocalGraphRuntime.create(self.home, 'workspace', 'synthetic-account',
                                               clock=lambda: self.now[0])

    def reopen(self):
        return LocalGraphRuntime(self.home, clock=lambda: self.now[0])

    def test_durable_workflow_and_branch_cache_survive_process_objects(self):
        self.runtime.submit(demo_plan())
        self.assertEqual(self.reopen().result('demo')['status'], 'QUEUED')
        self.assertEqual(self.runtime.work()['status'], 'COMPLETED')
        result = self.reopen().result('demo')
        self.assertEqual(result['receipt_sha256'], digest(result['report']))
        self.assertEqual(result['report']['computed_nodes'], 3)
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['external_actions'], 0)
        self.runtime.submit(demo_plan('repeat'))
        self.runtime.submit(demo_plan('changed', 'Synthetic additional document analysis.'))
        self.assertEqual(self.reopen().work()['tasks_observed'], 2)
        self.assertEqual(self.runtime.result('repeat')['report']['computed_nodes'], 0)
        self.assertEqual(self.runtime.result('changed')['report']['computed_nodes'], 2)
        self.assertEqual(self.runtime.result('changed')['report']['cache_hits'], 1)
        self.assertEqual(self.runtime.work()['tasks_observed'], 0)

    def test_multiple_runs_in_same_scope_have_independent_source_pins(self):
        self.runtime.submit(demo_plan('a'))
        self.runtime.submit(demo_plan('b', 'Changed body.'))
        self.assertEqual(self.runtime.work()['tasks_observed'], 2)
        state = self.runtime._reader().snapshot()['state']
        self.assertEqual(set(state['sources']['synthetic-analysis']), {'graph_plan:a', 'graph_plan:b'})
        self.assertTrue(all(row['status'] == 'COMPLETED' for row in self.runtime.status()['runs']))

    def test_duplicate_submission_never_reinvokes(self):
        plan = demo_plan()
        self.runtime.submit(plan)
        self.assertEqual(self.runtime.submit(plan)['status'], 'DUPLICATE')
        self.runtime.work()
        self.assertEqual(self.runtime.submit(plan)['status'], 'DUPLICATE')
        self.assertEqual(self.runtime.work()['tasks_observed'], 0)
        plan['nodes'][0]['arguments']['text'] = 'different'
        with self.assertRaisesRegex(MachineError, 'runtime_run_id_conflict'):
            self.runtime.submit(plan)

    def test_passive_opens_submissions_and_holds_do_not_fence_controller(self):
        generation = self.runtime.status()['coordinator_generation']
        self.runtime.submit(demo_plan())
        self.reopen().status()
        self.reopen().hold('demo')
        self.assertEqual(self.runtime.status()['coordinator_generation'], generation)
        self.assertEqual(self.runtime.result('demo')['status'], 'HELD')

    def test_hold_before_work_is_sticky_and_never_releases_outputs(self):
        self.runtime.submit(demo_plan())
        self.runtime.hold('demo', 'first_reason')
        self.assertEqual(self.runtime.hold('demo', 'second_reason')['reason'], 'first_reason')
        self.assertEqual(self.runtime.work()['status'], 'BLOCKED')
        self.assertEqual(self.runtime.work()['tasks_observed'], 0)
        result = self.runtime.result('demo')
        self.assertEqual(result['status'], 'HELD')
        self.assertIsNone(result['report'])
        self.assertIsNone(result['receipt_sha256'])

    def test_hold_after_completion_retains_history_but_withholds_report(self):
        self.runtime.submit(demo_plan())
        self.runtime.work()
        previous = self.runtime.result('demo')['receipt_sha256']
        self.runtime.hold('demo')
        self.assertIsNone(self.runtime.result('demo')['report'])
        self.assertEqual(self.runtime.status()['runs'][0]['receipt_sha256'], previous)

    def test_coherent_final_guard_rechecks_live_hold(self):
        self.runtime.submit(demo_plan())
        guard = self.runtime._guard
        phases = []
        def changed(request, now):
            phases.append(request['phase'])
            if request['phase'] == 'graph-return':
                self.reopen().hold('demo', 'changed_input')
            return guard(request, now)
        with patch.object(self.runtime, '_guard', side_effect=changed):
            outcome = self.runtime.work()
        self.assertIn('graph-return', phases)
        self.assertEqual(outcome['status'], 'BLOCKED')
        self.assertIsNone(self.runtime.result('demo')['report'])
        with self.runtime._transaction() as (db, _):
            self.assertIsNone(db.execute('SELECT receipt_sha256 FROM runtime_runs').fetchone()[0])

    def test_hold_between_guard_and_report_commit_prevents_persistence(self):
        self.runtime.submit(demo_plan())
        writer = self.runtime._record_result
        def changed(report):
            self.reopen().hold('demo')
            return writer(report)
        with patch.object(self.runtime, '_record_result', side_effect=changed):
            self.assertEqual(self.runtime.work()['status'], 'UNKNOWN')
        with self.runtime._transaction() as (db, _):
            self.assertIsNone(db.execute('SELECT receipt_sha256 FROM runtime_runs').fetchone()[0])

    def test_lost_commit_reply_stays_unknown_and_locks_following_work(self):
        self.runtime.submit(demo_plan())
        writer = self.runtime._record_result
        def lost(report):
            writer(report)
            raise OSError('injected lost reply')
        with patch.object(self.runtime, '_record_result', side_effect=lost):
            self.assertEqual(self.runtime.work()['status'], 'UNKNOWN')
        self.runtime.submit(demo_plan('next'))
        self.assertEqual(self.reopen().work()['tasks_observed'], 0)
        result = self.runtime.result('demo')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['report'])
        self.assertEqual(self.runtime.result('next')['status'], 'READY')
        with self.runtime._transaction() as (db, _):
            self.assertIsNotNone(db.execute('SELECT receipt_sha256 FROM runtime_runs WHERE run_id=?', ('demo',)).fetchone()[0])

    def test_interrupted_controller_restart_keeps_uncertain_intent(self):
        self.runtime.submit(demo_plan())
        with patch.object(self.runtime, '_record_result', side_effect=SystemExit('injected interruption')):
            with self.assertRaises(SystemExit):
                self.runtime.work()
        self.assertEqual(self.runtime.result('demo')['status'], 'STARTED')
        self.assertEqual(self.reopen().work()['tasks_observed'], 0)
        self.assertEqual(self.runtime.result('demo')['status'], 'UNKNOWN')

    def test_stale_generation_during_callback_prevents_receipt(self):
        self.runtime.submit(demo_plan())
        guard = self.runtime._guard
        fired = [False]
        def stale(request, now):
            if not fired[0]:
                fired[0] = True
                from keel_muse.coordinator import Coordinator
                Coordinator(self.home / 'coordinator', 'workspace',
                    {'machine': lambda context: None}, lambda task: None,
                    clock=lambda: self.now[0], max_running=1, max_pending=64, max_calls=64)
            return guard(request, now)
        with patch.object(self.runtime, '_guard', side_effect=stale):
            self.assertEqual(self.runtime.work()['status'], 'UNKNOWN')
        self.assertIsNone(self.runtime.result('demo')['report'])

    def test_busy_worker_rejected_without_restarting_controller(self):
        before = self.runtime.status()
        with self.runtime._worker_lock():
            with self.assertRaisesRegex(MachineError, 'runtime_worker_busy'):
                self.reopen().work()
        after = self.runtime.status()
        self.assertEqual(before['coordinator_generation'], after['coordinator_generation'])
        self.assertEqual(before['work_sessions'], after['work_sessions'])

    def test_invalid_account_operation_and_extra_authority_rejected(self):
        mutations = [lambda p: p.update(account_id='someone-else'),
                     lambda p: p['nodes'][0].update(operation='shell'),
                     lambda p: p.update(approval_current=True)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                plan = demo_plan(); mutate(plan)
                with self.assertRaises(ValueError):
                    self.runtime.submit(plan)
        self.assertEqual(self.runtime.status()['runs'], [])

    def test_existing_coordinator_payload_cap_preserved(self):
        plan = demo_plan(left_text='x' * 9000)
        with self.assertRaisesRegex(ValueError, 'task_payload_invalid'):
            self.runtime.submit(plan)
        self.assertEqual(self.runtime.status()['runs'], [])

    def test_work_budget_validated_and_preserved(self):
        for value in (0, 65, True, 1.5):
            with self.assertRaisesRegex(MachineError, 'runtime_work_budget_invalid'):
                self.runtime.work(value)
        for value in ('a', 'b', 'c'):
            self.runtime.submit(demo_plan(value))
        self.assertEqual(self.runtime.work(1)['tasks_observed'], 1)
        self.assertEqual(self.runtime.work(2)['tasks_observed'], 2)

    def test_runtime_lifetime_capacity_is_bounded(self):
        for index in range(MAX_RUNS):
            self.runtime.submit(demo_plan('run-' + str(index)))
        with self.assertRaisesRegex(MachineError, 'runtime_capacity_exhausted'):
            self.runtime.submit(demo_plan('too-many'))
        self.assertEqual(len(self.runtime.status()['runs']), MAX_RUNS)

    def test_runtime_session_capacity_is_bounded(self):
        with self.runtime._transaction() as (db, _):
            db.execute('UPDATE runtime_meta SET work_sessions=512')
        with self.assertRaisesRegex(MachineError, 'runtime_session_capacity_exhausted'):
            self.runtime.work()

    def test_clock_regression_and_permissions_fail_closed(self):
        self.now[0] = 99
        with self.assertRaisesRegex(MachineError, 'runtime_clock_regressed'):
            self.runtime.submit(demo_plan())
        self.now[0] = 100
        os.chmod(self.home / 'runtime.sqlite3', 0o644)
        with self.assertRaisesRegex(MachineError, 'storage_not_private'):
            self.reopen()

    def test_missing_cache_does_not_silently_reprovision(self):
        (self.home / 'computation.sqlite3').unlink()
        with self.assertRaisesRegex(MachineError, 'runtime_incomplete'):
            self.reopen()
        self.assertFalse((self.home / 'computation.sqlite3').exists())

    def test_unknown_runs_and_existing_home_rejected(self):
        with self.assertRaisesRegex(MachineError, 'runtime_run_missing'):
            self.runtime.result('missing')
        with self.assertRaisesRegex(MachineError, 'runtime_run_missing'):
            self.runtime.hold('missing')
        with self.assertRaises(FileExistsError):
            LocalGraphRuntime.create(self.home, 'workspace', 'synthetic-account')

    def test_report_bytes_checked_against_coordinator_receipt(self):
        self.runtime.submit(demo_plan())
        self.runtime.work()
        report = self.runtime.result('demo')['report']
        report['outputs']['summary']['left_words'] += 1
        with self.runtime._transaction() as (db, _):
            db.execute('UPDATE runtime_runs SET report=?', (canonical(report),))
        with self.assertRaisesRegex(MachineError, 'runtime_receipt_mismatch'):
            self.runtime.result('demo')
        with self.assertRaisesRegex(MachineError, 'runtime_receipt_mismatch'):
            self.runtime.status()

    def test_status_rejects_receipt_corruption(self):
        self.runtime.submit(demo_plan())
        self.runtime.work()
        with self.runtime._transaction() as (db, _):
            db.execute('UPDATE runtime_runs SET receipt_sha256=?', ('0' * 64,))
        with self.assertRaisesRegex(MachineError, 'runtime_receipt_mismatch'):
            self.runtime.status()

    def test_open_object_rejects_namespace_mutation(self):
        with self.runtime.db.transaction() as db:
            db.execute('UPDATE runtime_meta SET account_id=?', ('changed',))
        with self.assertRaisesRegex(MachineError, 'runtime_namespace_changed'):
            self.runtime.status()

    def test_live_object_does_not_reprovision_missing_cache(self):
        self.runtime.submit(demo_plan())
        (self.home / 'computation.sqlite3').unlink()
        with self.assertRaisesRegex(MachineError, 'runtime_incomplete'):
            self.runtime.work()
        self.assertFalse((self.home / 'computation.sqlite3').exists())

    def test_live_object_does_not_reprovision_missing_coordinator(self):
        self.runtime.submit(demo_plan())
        (self.home / 'coordinator/coordinator.sqlite3').unlink()
        with self.assertRaisesRegex(MachineError, 'runtime_incomplete'):
            self.runtime.work()
        self.assertFalse((self.home / 'coordinator/coordinator.sqlite3').exists())

    def test_live_object_pins_lock_identity(self):
        lock = self.home / 'worker.lock'
        with self.runtime._worker_lock():
            lock.rename(self.home / 'old-lock')
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            with self.assertRaisesRegex(MachineError, 'runtime_storage_replaced'):
                self.runtime.work()


if __name__ == '__main__':
    unittest.main()
