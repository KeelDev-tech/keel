"""Offline regressions for the real verifier and discovery scheduler hot paths."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import pipeline_service as service
import queue_io
import source_scheduler
from safe_io import atomic_json, read_json, aware_time


def queued(board, job, role=None):
    return {'role_id': role or f'{job:03d}-{board}',
            'application_url': f'https://job-boards.greenhouse.io/{board}/jobs/{job}',
            'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': None, 'holds': []}


def response(count=3):
    return {'jobs': [{'id': i, 'title': f'Synthetic role {i}'} for i in range(count)]}


class PipelineOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-optimization-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.queue = self.home / 'data/queues/standard-queue.json'
        self.ledger = self.home / 'data/application-ledger.json'
        for name in service.QUEUES:
            atomic_json(self.home / f'data/queues/{name}-queue.json', [])
        atomic_json(self.ledger, [])
        lock = patch.object(queue_io, '_LOCK_PATH', str(self.home / 'queue.lock'))
        lock.start(); self.addCleanup(lock.stop)
        # Leave each real durable event in its row to inspect receipt ordering.
        flush = patch.object(service, 'flush_outbox', return_value={'emitted': 0, 'pending': 0})
        flush.start(); self.addCleanup(flush.stop)
        self.calls = []

    def fetch(self, url, timeout):
        self.calls.append(url)
        return response()

    def reader(self, **kwargs):
        return service.PublicBoardReader(fetcher=self.fetch, **kwargs)

    def interleaved(self):
        rows = [queued(board, i) for i in range(3) for board in ('a', 'b', 'c')]
        atomic_json(self.queue, rows)
        return rows

    def test_fixed_cohort_fetches_each_board_once_under_lru_pressure(self):
        before = self.interleaved()
        with patch.object(service, 'MAX_CACHE_RECORDS', 3):
            report = service.verify(self.home, limit=8, live=True, reader=self.reader())
        self.assertEqual(report['selected'], 8)
        self.assertEqual(report['deferred_by_limit'], 1)
        self.assertEqual(report['requests'], 3)
        self.assertEqual(report['board_reads'], 3)
        self.assertEqual(report['verdicts'], {'live': 8})
        after = read_json(self.queue)
        self.assertEqual(after[-1], before[-1])
        self.assertEqual([r['role_id'] for r in after], [r['role_id'] for r in before])
        self.assertTrue(all(r['status'] == 'PARKED-PENDING-VERIFICATION' for r in after))
        self.assertTrue(all(r['fit_score'] is None for r in after))
        self.assertEqual(report['promoted_to_ready'], 0)

    def test_identity_is_parsed_once_per_snapshot_row(self):
        before = self.interleaved()
        with patch.object(service, '_key', wraps=service._key) as key:
            service.verify(self.home, reader=self.reader())
        self.assertEqual(key.call_count, len(before))
        with patch.object(service, '_key', wraps=service._key) as key:
            service.supply_report(self.home)
        self.assertEqual(key.call_count, len(before))

    def test_live_identity_is_recomputed_once_at_commit(self):
        before = self.interleaved()
        with patch.object(service, '_key', wraps=service._key) as key:
            service.verify(self.home, live=True, reader=self.reader())
        self.assertEqual(key.call_count, 2 * len(before))

    def test_idle_run_does_not_open_empty_commit_or_repeat_outbox_flush(self):
        target = {**queued('a', 0), 'holds': ['consent_quarantine']}
        atomic_json(self.queue, [target])
        receipt = {'emitted': 2, 'pending': 1, 'errors': []}
        with patch.object(service, 'flush_outbox', return_value=receipt) as flush:
            with patch.object(service, '_documents', wraps=service._documents) as documents:
                report = service.verify(self.home, live=True, reader=self.reader())
        self.assertEqual(report['observed'], 0)
        self.assertEqual(report['telemetry'], receipt)
        self.assertEqual(documents.call_count, 1)
        self.assertEqual(flush.call_count, 1)
        self.assertEqual(read_json(self.queue), [target])

    def test_request_cap_leaves_unattempted_rows_and_cooldowns_untouched(self):
        before = self.interleaved()
        report = service.verify(self.home, live=True, reader=self.reader(max_requests=1))
        self.assertEqual(report['requests'], 1)
        self.assertEqual(report['observed'], 3)
        self.assertEqual(report['deferred_without_attempt'], {'request_budget': 6})
        self.assertEqual(report['committed'], 3)
        after = read_json(self.queue)
        for old, current in zip(before, after):
            if old['role_id'].endswith('-a'):
                self.assertEqual(current['posting_verification']['verdict'], 'live')
            else:
                self.assertEqual(current, old)

    def test_budget_limited_following_run_services_previously_deferred_board(self):
        self.interleaved()
        first = service.verify(self.home, live=True, reader=self.reader(max_requests=1))
        second = service.verify(self.home, live=True, reader=self.reader(max_requests=1))
        self.assertEqual((first['committed'], second['committed']), (3, 3))
        self.assertIn('/a/', self.calls[0])
        self.assertIn('/b/', self.calls[1])

    def test_cached_board_remains_usable_at_request_cap(self):
        atomic_json(self.queue, [queued('a', 0), queued('b', 0)])
        reader = self.reader(max_requests=1)
        reader.read('greenhouse:b')
        report = service.verify(self.home, live=True, reader=reader)
        self.assertEqual(report['verdicts'], {'live': 1})
        self.assertEqual(report['deferred_without_attempt'], {'request_budget': 1})
        after = read_json(self.queue)
        self.assertNotIn('posting_verification', after[0])
        self.assertEqual(after[1]['posting_verification']['identity'], ['greenhouse', 'b', '0'])

    def test_rate_limit_preserves_unattempted_rows_and_actual_prior_live_evidence(self):
        before = self.interleaved()
        def fetch(url, timeout):
            self.calls.append(url)
            if '/b/' in url:
                raise HTTPError(url, 429, 'synthetic', {}, None)
            return response()
        reader = service.PublicBoardReader(fetcher=fetch)
        report = service.verify(self.home, live=True, reader=reader)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(report['rate_limit_hold'])
        self.assertEqual(report['deferred_without_attempt'], {'http_429': 3})
        self.assertEqual(report['verdicts'], {'live': 3, 'none': 3})
        for old, current in zip(before, read_json(self.queue)):
            if old['role_id'].endswith('-c'):
                self.assertEqual(old, current)
            elif old['role_id'].endswith('-a'):
                self.assertEqual(current['posting_verification']['verdict'], 'live')
            else:
                self.assertNotIn('posting_verification', current)
                attempt = current['verification_attempt']
                self.assertEqual(attempt['signal'], 'NONE')
                self.assertEqual(attempt['transport_class'], 'HTTP_429')
                self.assertEqual((aware_time(attempt['next_eligible_at']) -
                                  aware_time(attempt['observed_at'])).total_seconds(), 300)

    def test_partial_lever_page_attempt_contains_no_lead_signal(self):
        atomic_json(self.queue, [{'role_id': 'lever-1', 'application_url': 'https://jobs.lever.co/a/one',
                                 'status': 'PARKED-PENDING-VERIFICATION'}])
        page = [{'id': str(i), 'text': f'Synthetic {i}'} for i in range(service.PAGE_SIZE)]
        reader = service.PublicBoardReader(max_requests=1, fetcher=lambda *_: page)
        report = service.verify(self.home, live=True, reader=reader)
        self.assertEqual(report['observed'], 1)
        self.assertEqual(report['verdicts'], {'none': 1})
        self.assertEqual(report['transport_classes'], {'REQUEST_BUDGET': 1})
        self.assertEqual(report['deferred_without_attempt'], {})

    def test_persisted_http_hold_stops_without_restamping_any_posting(self):
        before = self.interleaved()
        def held(url, timeout):
            self.calls.append(url)
            raise service.HostRateLimited('synthetic persisted host cooldown')
        reader = service.PublicBoardReader(fetcher=held)
        report = service.verify(self.home, live=True, reader=reader)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report['observed'], 0)
        self.assertEqual(report['committed'], 0)
        self.assertEqual(report['deferred_without_attempt'], {'persisted_http_429': 3, 'http_429': 6})
        self.assertTrue(reader.stopped)
        self.assertEqual(read_json(self.queue), before)

    def test_failed_board_is_attempted_once_and_all_cohort_rows_have_no_signal(self):
        self.interleaved()
        def fetch(url, timeout):
            self.calls.append(url)
            if '/a/' in url:
                raise HTTPError(url, 503, 'synthetic', {}, None)
            return response()
        report = service.verify(self.home, reader=service.PublicBoardReader(fetcher=fetch))
        self.assertEqual(report['requests'], 3)
        self.assertEqual(report['verdicts'], {'none': 3, 'live': 6})

    def test_terminal_ledger_exact_identity_blocks_different_role_id(self):
        target = queued('a', 0)
        atomic_json(self.queue, [target])
        for status in sorted(service.FINAL_OR_ACTIVE):
            with self.subTest(status=status):
                ledger = {**target, 'role_id': 'other-id', 'status': status,
                    'application_url': 'https://boards.greenhouse.io/a/jobs/0?tracking=ignored'}
                atomic_json(self.ledger, [ledger])
                report = service.verify(self.home, reader=self.reader())
                self.assertEqual(report['selected'], 0)
                self.assertEqual(report['skipped'], {'held_or_active_or_terminal': 1})
                self.assertEqual(service.supply_report(self.home)['mutually_exclusive_supply_states'],
                                 {'held_active_or_terminal': 1})
        self.assertEqual(self.calls, [])

    def test_nonterminal_ledger_record_does_not_create_terminal_hold(self):
        target = queued('a', 0)
        atomic_json(self.queue, [target])
        atomic_json(self.ledger, [{**target, 'role_id': 'other-id', 'status': 'PARKED-PENDING-VERIFICATION'}])
        report = service.verify(self.home, reader=self.reader())
        self.assertEqual(report['verdicts'], {'live': 1})

    def test_concurrent_ledger_exact_identity_blocks_commit(self):
        target = queued('a', 0)
        atomic_json(self.queue, [target])
        def fetch(*_):
            atomic_json(self.ledger, [{**target, 'role_id': 'new-ledger-id', 'status': 'UNKNOWN_OUTCOME'}])
            return response()
        report = service.verify(self.home, live=True, reader=service.PublicBoardReader(fetcher=fetch))
        self.assertEqual(report['committed'], 0)
        self.assertEqual(report['concurrent_conflicts'], [target['role_id']])
        self.assertEqual(read_json(self.queue), [target])

    def test_cross_queue_duplicate_identity_blocks_both_rows(self):
        atomic_json(self.queue, [queued('a', 0, 'first')])
        atomic_json(self.home / 'data/queues/strategic-queue.json', [queued('a', 0, 'second')])
        report = service.verify(self.home, reader=self.reader())
        self.assertEqual(report['selected'], 0)
        self.assertEqual(report['skipped'], {'duplicate_exact_posting': 2})
        self.assertEqual(self.calls, [])

    def test_new_duplicate_identity_is_detected_at_commit(self):
        target = queued('a', 0)
        atomic_json(self.queue, [target])
        def fetch(*_):
            atomic_json(self.home / 'data/queues/needs_input-queue.json', [queued('a', 0, 'new-copy')])
            return response()
        report = service.verify(self.home, live=True, reader=service.PublicBoardReader(fetcher=fetch))
        self.assertEqual(report['committed'], 0)
        self.assertEqual(read_json(self.queue), [target])

    def test_changed_target_and_human_holds_cannot_use_prior_identity_index(self):
        for mutation in ({'application_url': 'https://job-boards.greenhouse.io/b/jobs/0'},
                         {'holds': ['consent_quarantine']}, {'human_hold': True},
                         {'structurally_blocked': True}, {'revoked': True}):
            with self.subTest(mutation=mutation):
                target = queued('a', 0)
                atomic_json(self.queue, [target])
                def fetch(*_):
                    atomic_json(self.queue, [{**target, **mutation}])
                    return response()
                report = service.verify(self.home, live=True, reader=service.PublicBoardReader(fetcher=fetch))
                self.assertEqual(report['committed'], 0)
                self.assertEqual(read_json(self.queue), [{**target, **mutation}])

    def test_scheduler_does_not_increment_failure_or_consume_unattempted_turn(self):
        for board in ('a', 'b', 'c'):
            service.add_source(self.home, 'greenhouse:' + board)
        report = source_scheduler.scheduled_discover(self.home, max_requests=1,
            reader=self.reader(max_requests=1), clock=lambda: 1000)
        self.assertEqual(report['status'], 'HELD_REQUEST_BUDGET')
        self.assertEqual(report['attempted'], ['greenhouse:a'])
        state = read_json(self.home / 'data/source-scheduler.json')
        for board in ('b', 'c'):
            row = state['sources']['greenhouse:' + board]
            self.assertEqual(row, {'last_attempt': 0, 'next_at': 0, 'failures': 0,
                                   'last_status': None, 'last_added': 0})
        follow = source_scheduler.scheduled_discover(self.home, max_boards=1,
            reader=self.reader(), clock=lambda: 1000)
        self.assertEqual(follow['attempted'], ['greenhouse:b'])

    def test_scheduler_persists_host_hold_without_consuming_a_source_turn(self):
        for board in ('a', 'b'):
            service.add_source(self.home, 'greenhouse:' + board)
        def held(*_):
            raise service.HostRateLimited('synthetic persisted host cooldown')
        report = source_scheduler.scheduled_discover(self.home,
            reader=service.PublicBoardReader(fetcher=held), clock=lambda: 1000)
        self.assertEqual(report['status'], 'HELD_HTTP_429')
        self.assertEqual(report['attempted'], [])
        state = read_json(self.home / 'data/source-scheduler.json')
        self.assertEqual(state['cooldown_until'], 1300)
        for row in state['sources'].values():
            self.assertEqual((row['failures'], row['next_at'], row['last_attempt']), (0, 0, 0))
        later = source_scheduler.scheduled_discover(self.home, reader=self.reader(), clock=lambda: 1001)
        self.assertEqual(later['status'], 'HELD_HTTP_429')
        self.assertEqual(self.calls, [])

    def test_scheduler_expired_deadline_preserves_unattempted_turns(self):
        for board in ('a', 'b'):
            service.add_source(self.home, 'greenhouse:' + board)
        reader = self.reader()
        reader.deadline = 0
        report = source_scheduler.scheduled_discover(self.home, reader=reader, clock=lambda: 1000)
        self.assertEqual(report['status'], 'HELD_DEADLINE')
        self.assertEqual(report['attempted'], [])
        self.assertEqual(report['requests'], 0)
        state = read_json(self.home / 'data/source-scheduler.json')
        self.assertIsNone(state['inflight'])
        for row in state['sources'].values():
            self.assertEqual(row, {'last_attempt': 0, 'next_at': 0, 'failures': 0,
                                   'last_status': None, 'last_added': 0})
        self.assertEqual(self.calls, [])

    def test_scheduler_deadline_race_before_read_does_not_back_off_source(self):
        service.add_source(self.home, 'greenhouse:a')
        reader = self.reader()
        discover = service.discover
        def expire_before_read(*args, **kwargs):
            reader.deadline = 0
            return discover(*args, **kwargs)
        with patch.object(service, 'discover', side_effect=expire_before_read):
            report = source_scheduler.scheduled_discover(self.home, reader=reader, clock=lambda: 1000)
        self.assertEqual(report['status'], 'HELD_DEADLINE')
        self.assertEqual(report['attempted'], [])
        self.assertEqual(report['requests'], 0)
        state = read_json(self.home / 'data/source-scheduler.json')
        self.assertEqual(state['sources']['greenhouse:a'],
                         {'last_attempt': 0, 'next_at': 0, 'failures': 0,
                          'last_status': None, 'last_added': 0})
        follow = source_scheduler.scheduled_discover(self.home, reader=self.reader(), clock=lambda: 1000)
        self.assertEqual(follow['attempted'], ['greenhouse:a'])

    def test_scheduler_deadline_after_dispatch_retains_real_attempt_backoff(self):
        for board in ('a', 'b'):
            service.add_source(self.home, 'greenhouse:' + board)
        reader = None
        def expire_after_dispatch(url, timeout):
            self.calls.append(url)
            reader.deadline = 0
            return response()
        reader = service.PublicBoardReader(fetcher=expire_after_dispatch)
        report = source_scheduler.scheduled_discover(self.home, reader=reader, clock=lambda: 1000)
        self.assertEqual(report['status'], 'HELD_DEADLINE')
        self.assertEqual(report['attempted'], ['greenhouse:a'])
        self.assertEqual(report['requests'], 1)
        state = read_json(self.home / 'data/source-scheduler.json')
        attempted = state['sources']['greenhouse:a']
        self.assertEqual((attempted['failures'], attempted['next_at'], attempted['last_status']),
                         (1, 1060, 'HELD_DEADLINE'))
        self.assertGreater(attempted['last_attempt'], 0)
        deferred = state['sources']['greenhouse:b']
        self.assertEqual((deferred['last_attempt'], deferred['failures'], deferred['next_at']), (0, 0, 0))
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
