"""Real public-pipeline cycles, accounting and restart regression fixtures."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import keel_paths
import log_event
import pipeline_service as pipeline
import productivity_service as productivity
import queue_io
from safe_io import atomic_json, read_json
from keel_efficiency.ledger import ResourceLedger


class ProductivityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='keel-productivity-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 1800000000.0
        self.calls = []
        self.queue = self.home / 'data/queues/standard-queue.json'
        for name in pipeline.QUEUES:
            atomic_json(self.home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        atomic_json(self.home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        for context in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
                        patch.object(log_event, 'EVENTS', str(self.home / 'data/telemetry/events.jsonl')),
                        patch.object(keel_paths, 'HOME', str(self.home)),
                        patch.dict(os.environ, {'KEEL_HOME': str(self.home), 'JOB_PIPELINE_HTTP_COOLDOWN_DIR': str(self.home / 'hidden_files/http-cooldowns')})):
            context.start()
            self.addCleanup(context.stop)
        self.ledger = ResourceLedger(self.home / 'budget.sqlite3')
        self.ledger.create_scope('shared', {'calls': 100, 'compute_ms': 1000000})

    def fetch(self, url, timeout):
        self.calls.append(url)
        return {'jobs': [{'id': 1, 'title': 'Synthetic Operations Manager', 'location': {'name': 'Synthetic'}}]}

    def cycle(self, run_id, **kwargs):
        options = dict(run_id=run_id, live=True, ledger=self.ledger, scope_id='shared',
                       target_verified=1, fetcher=self.fetch, clock=lambda: self.now)
        options.update(kwargs)
        return productivity.run_once(self.home, **options)

    def source(self, ref='greenhouse:fixture'):
        pipeline.add_source(self.home, ref)

    def journal(self):
        return read_json(self.home / 'data/productivity/journal.json')

    def test_real_discovery_verification_then_idle(self):
        self.source()
        first = self.cycle('discovery')
        self.assertEqual((first['stage'], first['progress']['added_leads']), ('discover', 1))
        second = self.cycle('verification')
        self.assertEqual((second['stage'], second['progress']['committed_posting_presence']), ('verify', 1))
        third = self.cycle('idle')
        self.assertEqual((third['stage'], third['reason']), ('idle', 'verified_posting_target_met'))
        self.assertEqual(len(self.calls), 2)
        row = read_json(self.queue)[0]
        self.assertEqual(row['status'], 'PARKED-PENDING-VERIFICATION')
        self.assertFalse(row['execution_authorized'])
        self.assertFalse(row['posting_verification']['form_verified'])
        self.assertFalse(row['posting_verification']['execution_authorized'])
        self.assertIsNone(second['usage']['external_credit_micros'])
        self.assertEqual(self.ledger.snapshot('shared')['used']['calls'], 2)
        self.assertIsNone(second['application_completions_verified'])
        report = productivity.status(self.home, clock=lambda: self.now)
        self.assertEqual(report['metrics']['stages']['verify']['requests_per_stage_result'], 1)
        self.assertFalse(report['execution_authorized'])

    def test_replay_after_stage_changes_never_reads_again(self):
        self.source()
        first = self.cycle('same')
        before = self.queue.read_bytes()
        replay = self.cycle('same')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(replay['stage'], 'discover')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.queue.read_bytes(), before)
        for change in ({'max_requests': 7}, {'target_verified': 2}, {'scope_id': 'other'}):
            if 'scope_id' in change:
                self.ledger.create_scope('other', {'calls': 100, 'compute_ms': 1000000})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'binding_conflict'):
                self.cycle('same', **change)
        self.assertEqual(len(self.calls), 1)

    def test_status_and_dry_run_do_not_create_controller_or_read_network(self):
        self.source()
        queue = self.queue.read_bytes()
        first = productivity.status(self.home, clock=lambda: self.now)
        dry = productivity.run_once(self.home, fetcher=self.fetch, clock=lambda: self.now)
        self.assertEqual(first['next_stage'], 'discover')
        self.assertTrue(dry['dry_run'])
        self.assertEqual(dry['network_calls'], 0)
        self.assertFalse((self.home / 'data/productivity').exists())
        self.assertEqual(queue, self.queue.read_bytes())
        self.assertEqual(self.calls, [])

    def test_budget_refuses_before_source_reads(self):
        self.source()
        self.ledger.create_scope('tiny', {'calls': 1, 'compute_ms': 100000})
        result = self.cycle('limited', scope_id='tiny', max_requests=2)
        self.assertEqual(result['status'], 'HELD_BUDGET')
        self.assertEqual(self.calls, [])
        self.assertEqual(read_json(self.queue), [])
        self.assertEqual(self.ledger.snapshot('tiny')['used']['calls'], 0)

    def test_request_cap_holds_remaining_boards(self):
        self.source('greenhouse:fixture-a')
        self.source('greenhouse:fixture-b')
        result = self.cycle('bounded', max_requests=1)
        self.assertEqual(result['usage']['calls'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['status'], 'HELD_REQUEST_BUDGET')
        self.assertEqual(result['progress']['added_leads'], 1)

    def test_recorded_result_replays_settlement_without_read(self):
        self.source()
        with patch.object(self.ledger, 'settle', side_effect=OSError('fixture settlement interrupted')):
            with self.assertRaises(OSError):
                self.cycle('settlement')
        self.assertEqual(self.journal()['runs']['settlement']['phase'], 'RECORDED')
        other = self.cycle('other')
        self.assertEqual(other['status'], 'HELD_RECOVERY')
        replay = self.cycle('settlement')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['progress']['added_leads'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger.snapshot('shared')['used']['calls'], 1)
        self.assertEqual(self.journal()['runs']['settlement']['phase'], 'COMPLETE')

    def test_commit_before_result_crash_is_not_redispatched_or_refunded(self):
        self.source()
        save = productivity._save
        def interrupted(path, state, now):
            if any(row['phase'] == 'RECORDED' for row in state['runs'].values()):
                raise OSError('fixture durable receipt unavailable')
            return save(path, state, now)
        with patch.object(productivity, '_save', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.cycle('crash')
        self.assertEqual(len(read_json(self.queue)), 1)
        self.assertEqual(self.journal()['runs']['crash']['phase'], 'INTENT')
        self.assertEqual(self.cycle('crash')['status'], 'HELD_RECOVERY')
        self.assertEqual(self.cycle('different')['status'], 'HELD_RECOVERY')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger.snapshot('shared')['reserved']['calls'], 8)
        observed = productivity.status(self.home, clock=lambda: self.now)
        self.assertEqual(observed['metrics']['runs_without_measured_result'], 1)

    def test_stage_error_preserves_unknown_progress_and_blocks_new_runs(self):
        self.source()
        discover = pipeline.discover
        def uncertain(*args, **kwargs):
            discover(*args, **kwargs)
            raise OSError('fixture response lost after real queue commit')
        with patch.object(pipeline, 'discover', side_effect=uncertain):
            result = self.cycle('uncertain')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['progress']['added_leads'])
        self.assertEqual(result['usage']['calls'], 1)
        self.assertEqual(len(read_json(self.queue)), 1)
        self.assertEqual(self.cycle('next')['status'], 'HELD_RECOVERY')
        self.assertEqual(len(self.calls), 1)
        metrics = productivity.status(self.home, clock=lambda: self.now)['metrics']['stages']['discover']
        self.assertIsNone(metrics['requests_per_stage_result'])
        self.assertEqual(metrics['progress_unknown_runs'], 1)

    def test_zero_yield_uses_persistent_backoff(self):
        self.source()
        def empty(url, timeout):
            self.calls.append(url)
            return {'jobs': []}
        for index in range(3):
            result = self.cycle('empty-' + str(index), fetcher=empty)
            self.assertEqual(result['progress']['added_leads'], 0)
            if index < 2:
                self.now += 301
        paused = self.cycle('pause', fetcher=empty)
        self.assertEqual(paused['reason'], 'discover_low_yield_cooldown')
        self.assertEqual(paused['usage']['calls'], 0)
        self.assertEqual(len(self.calls), 3)
        self.now += 301
        self.cycle('resume', fetcher=empty)
        self.assertEqual(len(self.calls), 4)

    def test_rate_limit_and_unknown_application_are_not_bypassed(self):
        self.source()
        def limited(url, timeout):
            self.calls.append(url)
            raise HTTPError(url, 429, 'fixture', {}, None)
        first = self.cycle('rate', fetcher=limited)
        self.assertEqual(first['status'], 'HELD_HTTP_429')
        second = self.cycle('rate-again', fetcher=limited)
        self.assertEqual(second['usage']['calls'], 0)
        self.assertEqual(len(self.calls), 1)
        row = {'role_id': 'protected', 'status': 'UNKNOWN_OUTCOME', 'holds': ['unknown_application'],
               'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/1'}
        atomic_json(self.queue, [row])
        self.now += 301
        self.cycle('protected')
        self.assertEqual(read_json(self.queue), [row])
        self.assertIsNone(first['application_completions_verified'])

    def test_human_questions_and_backlog_cap_do_not_trigger_discovery(self):
        self.source()
        row = {'role_id': 'question', 'status': 'PARKED', 'unresolved': ['Applicant decision'],
               'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/1',
               'posting_verification': {'verdict': 'live', 'observed_at': pipeline.utc_now().isoformat(), 'identity': ['greenhouse', 'fixture', '1']}}
        atomic_json(self.queue, [row])
        report = self.cycle('question', target_verified=2)
        self.assertEqual(report['reason'], 'human_decisions_pending')
        self.assertEqual(report['after']['exact_question_groups'], 1)
        self.assertNotIn('Applicant decision', str(self.journal()))
        capped = self.cycle('cap', target_verified=2, backlog_limit=1)
        self.assertEqual(capped['reason'], 'queue_backlog_limit')
        self.assertEqual(self.calls, [])

    def test_runtime_path_mismatch_fails_before_mutation(self):
        self.source()
        with patch.object(queue_io, '_LOCK_PATH', str(self.home / 'wrong.lock')):
            with self.assertRaisesRegex(ValueError, 'runtime_binding_mismatch'):
                self.cycle('mismatch')
            with self.assertRaisesRegex(ValueError, 'runtime_binding_mismatch'):
                productivity.status(self.home)
        self.assertFalse((self.home / 'data/productivity').exists())
        self.assertEqual(self.calls, [])

    def test_journal_tamper_or_symlink_is_rejected(self):
        self.cycle('idle')
        path = self.home / 'data/productivity/journal.json'
        state = self.journal()
        state['runs']['idle']['result']['usage']['calls'] = 99
        atomic_json(path, state)
        with self.assertRaisesRegex(ValueError, 'receipt_invalid'):
            productivity.status(self.home, clock=lambda: self.now)
        path.unlink()
        path.symlink_to(self.queue)
        with self.assertRaises(OSError):
            productivity.status(self.home, clock=lambda: self.now)

    def test_initial_journal_failure_cancels_only_undispatched_reservation(self):
        self.source()
        with patch.object(productivity, '_save', side_effect=OSError('fixture no write')):
            with self.assertRaises(OSError):
                self.cycle('unwritten')
        self.assertFalse((self.home / 'data/productivity/journal.json').exists())
        self.assertEqual(self.ledger.snapshot('shared')['reserved']['calls'], 0)
        self.assertEqual(self.calls, [])
        self.cycle('new-id')
        self.assertEqual(len(self.calls), 1)

    def test_durable_predispatch_intent_can_be_cancelled_without_retry(self):
        self.source()
        save = productivity._save
        def after_replace(path, state, now):
            save(path, state, now)
            raise OSError('fixture directory sync uncertainty')
        with patch.object(productivity, '_save', side_effect=after_replace):
            with self.assertRaises(OSError):
                self.cycle('before-dispatch')
        self.assertEqual(self.journal()['runs']['before-dispatch']['phase'], 'INTENT')
        restored = productivity.recover(self.home, run_id='before-dispatch', ledger=self.ledger,
                                       scope_id='shared', clock=lambda: self.now)
        self.assertEqual(restored['status'], 'CANCELLED')
        self.assertEqual(self.cycle('before-dispatch')['status'], 'CANCELLED')
        self.assertEqual(self.calls, [])
        self.cycle('new-attempt')
        self.assertEqual(len(self.calls), 1)

    def test_unknown_recorded_progress_recovery_preserves_evidence(self):
        self.source()
        discover = pipeline.discover
        def uncertain(*args, **kwargs):
            discover(*args, **kwargs)
            raise OSError('fixture committed before response')
        with patch.object(pipeline, 'discover', side_effect=uncertain):
            first = self.cycle('unknown')
        usage = self.ledger.snapshot('shared')['used']
        result = productivity.recover(self.home, run_id='unknown', ledger=self.ledger,
                                      scope_id='shared', clock=lambda: self.now)
        self.assertEqual(result['status'], 'RECOVERED')
        self.assertIsNone(result['previous_progress']['added_leads'])
        self.assertEqual(result['receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(self.ledger.snapshot('shared')['used'], usage)
        self.assertEqual(self.journal()['runs']['unknown']['result']['status'], 'UNKNOWN')
        next_run = self.cycle('next')
        self.assertEqual(next_run['stage'], 'verify')
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(productivity.status(self.home, clock=lambda: self.now)['metrics']['stages']['discover']['requests_per_stage_result'])

    def test_dispatched_intent_recovery_retains_reservation(self):
        self.source()
        with patch.object(self.ledger, 'mark_dispatched', wraps=self.ledger.mark_dispatched) as dispatch:
            save = productivity._save
            def interrupted(path, state, now):
                if dispatch.call_count:
                    raise OSError('fixture crash after dispatch admission')
                save(path, state, now)
            with patch.object(productivity, '_save', side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.cycle('dispatched')
        result = productivity.recover(self.home, run_id='dispatched', ledger=self.ledger,
                                      scope_id='shared', clock=lambda: self.now)
        self.assertEqual(result['status'], 'HELD_RECOVERY')
        self.assertFalse(result['reservation_released'])
        self.assertEqual(self.ledger.snapshot('shared')['reserved']['calls'], 8)
        self.assertEqual(self.calls, [])

    def test_new_budget_window_partitions_measured_history(self):
        self.source()
        self.cycle('window-one')
        self.ledger.create_scope('window-two', {'calls': 100, 'compute_ms': 1000000})
        self.cycle('window-two', scope_id='window-two')
        first = productivity.status(self.home, ledger=self.ledger, scope_id='shared', clock=lambda: self.now)
        second = productivity.status(self.home, ledger=self.ledger, scope_id='window-two', clock=lambda: self.now)
        combined = productivity.status(self.home, clock=lambda: self.now)
        self.assertEqual(set(first['metrics']['stages']), {'discover'})
        self.assertEqual(set(second['metrics']['stages']), {'verify'})
        self.assertEqual(len(combined['metrics']['scope_coverage']), 2)

    def test_replaced_budget_database_cannot_reset_retained_spending(self):
        self.source()
        self.cycle('spent')
        Path(self.ledger.path).unlink()
        fresh = ResourceLedger(self.ledger.path)
        fresh.create_scope('shared', {'calls': 100, 'compute_ms': 1000000})
        for run_id in ('spent', 'new-id'):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                self.cycle(run_id, ledger=fresh)
        self.assertEqual(len(self.calls), 1)

    def test_recomputed_hash_does_not_accept_invalid_usage(self):
        from safe_io import digest
        self.cycle('idle')
        path = self.home / 'data/productivity/journal.json'
        state = self.journal()
        row = state['runs']['idle']
        row['result']['usage']['calls'] = -7
        row['receipt_sha256'] = digest(row['result'])
        atomic_json(path, state)
        with self.assertRaisesRegex(ValueError, 'usage_invalid'):
            productivity.status(self.home, clock=lambda: self.now)

    def test_invalid_input_and_missing_live_budget_cannot_dispatch(self):
        for options in ({'run_id': None}, {'max_requests': True}, {'timeout': float('nan')},
                        {'max_new': 0}, {'titles': ['']}, {'ledger': None, 'scope_id': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.cycle('invalid', **options) if 'run_id' not in options else productivity.run_once(self.home, live=True, **options)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
