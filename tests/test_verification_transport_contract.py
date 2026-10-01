"""Posting evidence survives transport failures; scheduling still moves on."""
from datetime import timedelta
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import pipeline_service as service
import queue_io
from safe_io import atomic_json, read_json, canonical


def queued(job, *, board='fixture', fit=75):
    return {'role_id': f'{job:03d}-{board}', 'company': 'Same employer',
            'application_url': f'https://job-boards.greenhouse.io/{board}/jobs/{job}',
            'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': fit,
            'last_verify_source': 'board_api_72h',
            'recovery_track': {'source': 'board_api_72h', 'checks': ['prior-decisive']},
            'ambiguity_streak': 2, 'quarantine_streak': 3}


class VerificationTransportContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-verify-transport-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.queue = self.home / 'data/queues/standard-queue.json'
        for name in service.QUEUES:
            atomic_json(self.home / f'data/queues/{name}-queue.json', [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        for item in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'queue.lock')),
                     patch.object(service, 'flush_outbox', return_value={'emitted': 0, 'pending': 0})):
            item.start()
            self.addCleanup(item.stop)

    def queue_and_ledger_bytes(self):
        paths = [self.home / f'data/queues/{name}-queue.json' for name in service.QUEUES]
        paths.append(self.home / 'data/application-ledger.json')
        return {path: path.read_bytes() for path in paths}

    def test_closed_queue_states_never_dispatch_or_change_queue_and_ledger(self):
        for status in ('CLOSED', 'CLOSED-EXPIRED', 'closed', 'Closed-Expired', 'DEAD'):
            for live in (False, True):
                with self.subTest(status=status, live=live):
                    atomic_json(self.queue, [{**queued(1), 'status': status}])
                    before = self.queue_and_ledger_bytes()
                    report = service.verify(self.home, live=live,
                        reader=service.PublicBoardReader(fetcher=lambda *_: self.fail('terminal dispatch')))
                    self.assertEqual(report['requests'], 0)
                    self.assertEqual(report['skipped'], {'held_or_active_or_terminal': 1})
                    self.assertFalse(report['submission_authorized'])
                    self.assertEqual(before, self.queue_and_ledger_bytes())
                    self.assertEqual(service.supply_report(self.home)['mutually_exclusive_supply_states'],
                                     {'held_active_or_terminal': 1})

    def test_closed_ledger_holds_active_queue_by_role_or_exact_posting(self):
        for status in ('CLOSED', 'CLOSED-EXPIRED', 'closed', 'Closed-Expired'):
            for binding in ('role', 'posting'):
                with self.subTest(status=status, binding=binding):
                    active = queued(1)
                    terminal = {**active, 'status': status}
                    if binding == 'role':
                        terminal['application_url'] = queued(2)['application_url']
                    else:
                        terminal['role_id'] = 'historical-role'
                    atomic_json(self.queue, [active])
                    atomic_json(self.home / 'data/application-ledger.json', [terminal])
                    before = self.queue_and_ledger_bytes()
                    report = service.verify(self.home, live=True,
                        reader=service.PublicBoardReader(fetcher=lambda *_: self.fail('terminal ledger dispatch')))
                    self.assertEqual(report['requests'], 0)
                    self.assertEqual(report['skipped'], {'held_or_active_or_terminal': 1})
                    self.assertEqual(before, self.queue_and_ledger_bytes())

    def test_existing_active_states_remain_scannable_without_reopening_closed_rows(self):
        for status in ('PARKED-PENDING-VERIFICATION', 'REPOST-WATCH'):
            with self.subTest(status=status):
                atomic_json(self.queue, [{**queued(1), 'status': status}])
                before = self.queue.read_bytes()
                calls = []
                report = service.verify(self.home, reader=service.PublicBoardReader(
                    fetcher=lambda *_: calls.append(1) or {'jobs': []}))
                self.assertEqual(report['requests'], 1)
                self.assertEqual(calls, [1])
                self.assertEqual(self.queue.read_bytes(), before)

    def test_terminal_ledger_added_during_read_prevents_verification_commit(self):
        for status in ('CLOSED', 'CLOSED-EXPIRED'):
            with self.subTest(status=status):
                active = queued(1)
                atomic_json(self.queue, [active])
                ledger = self.home / 'data/application-ledger.json'
                atomic_json(ledger, [])
                before = self.queue.read_bytes()
                def fetch(*_):
                    atomic_json(ledger, [{**active, 'status': status}])
                    return {'jobs': []}
                report = service.verify(self.home, live=True,
                    reader=service.PublicBoardReader(fetcher=fetch))
                self.assertEqual(report['requests'], 1)
                self.assertEqual(report['committed_verdicts'], {})
                self.assertEqual(self.queue.read_bytes(), before)
                self.assertEqual(read_json(ledger), [{**active, 'status': status}])

    def test_first_failed_attempt_honors_its_own_cooldown(self):
        for with_old_target in (False, True):
            with self.subTest(with_old_target=with_old_target):
                row = queued(1)
                if with_old_target:
                    row = self.prior_live(row)
                    row['posting_verification']['identity'] = ['greenhouse', 'old', '1']
                _, after, _ = self.run_failure(TimeoutError('fixture'), [row])
                after[0].pop('verification_event_pending')
                atomic_json(self.queue, after)
                before = self.queue.read_bytes()
                report = service.verify(self.home, live=True,
                    reader=service.PublicBoardReader(fetcher=lambda *_: self.fail('cooldown dispatched')))
                self.assertEqual(report['requests'], 0)
                self.assertEqual(report['skipped'], {'cooldown': 1})
                self.assertFalse(report['submission_authorized'])
                self.assertEqual(self.queue.read_bytes(), before)
                after[0]['verification_attempt']['next_eligible_at'] = (
                    service.utc_now()-timedelta(seconds=1)).isoformat()
                atomic_json(self.queue, after)
                expired = service.verify(self.home,
                    reader=service.PublicBoardReader(fetcher=lambda *_: {'jobs': []}))
                self.assertEqual(expired['requests'], 1)

    def test_old_target_attempt_cannot_borrow_current_decisive_identity(self):
        row = self.prior_live(queued(1))
        row['verification_attempt'] = {
            'identity': ['greenhouse', 'old', '1'],
            'next_eligible_at': (service.utc_now()+timedelta(days=1)).isoformat()}
        self.assertIsNone(service._next_time(row, service._key(row)))

    def test_legacy_and_malformed_retry_timestamps_remain_conservative(self):
        row = queued(1)
        future = (service.utc_now()+timedelta(days=1)).isoformat()
        row['verification_attempt'] = {'next_eligible_at': future}
        self.assertEqual(service._next_time(row, service._key(row)), service.aware_time(future))
        row['verification_attempt'] = {'identity': list(service._key(row)),
                                       'next_eligible_at': 'invalid'}
        self.assertEqual(service._next_time(row, service._key(row)), 'invalid')

    def prior_live(self, row):
        observed = service.utc_now() - timedelta(seconds=1)
        row['posting_verification'] = {
            'schema_version': 1, 'identity': ['greenhouse', row['role_id'].split('-')[-1],
                                                row['role_id'].split('-')[0].lstrip('0') or '0'],
            'verdict': 'live', 'reason': 'prior_decisive_evidence',
            'observed_at': observed.isoformat(),
            'next_eligible_at': (observed - timedelta(seconds=1)).isoformat()}
        return row

    def run_failure(self, exc, rows=None, *, retry=False):
        before = rows or [self.prior_live(queued(1))]
        atomic_json(self.queue, before)
        def fail(*_):
            raise exc
        reader = service.PublicBoardReader(fetcher=fail, retry_timeouts=retry)
        report = service.verify(self.home, limit=max(1, len(before)), live=True, reader=reader)
        return before, read_json(self.queue), report

    def test_transport_classes_preserve_decisive_record_and_recovery_fields(self):
        examples = [(TimeoutError('fixture'), 'TIMEOUT'),
                    (URLError(socket.gaierror('fixture')), 'DNS_FAILURE'),
                    (ConnectionResetError('fixture'), 'CONNECTION_RESET'),
                    (HTTPError('https://boards-api.greenhouse.io/', 429, 'fixture', {}, None), 'HTTP_429'),
                    (HTTPError('https://boards-api.greenhouse.io/', 503, 'fixture', {}, None), 'HTTP_5XX')]
        for exc, expected in examples:
            with self.subTest(expected=expected):
                before, after, report = self.run_failure(exc)
                current = after[0]
                self.assertEqual(canonical(current['posting_verification']),
                                 canonical(before[0]['posting_verification']))
                for key in ('last_verify_source', 'recovery_track', 'ambiguity_streak',
                            'quarantine_streak', 'status', 'fit_score'):
                    self.assertEqual(current[key], before[0][key])
                attempt = current['verification_attempt']
                self.assertEqual((attempt['signal'], attempt['transport_class']), ('NONE', expected))
                self.assertFalse(attempt['lead_attributed'])
                self.assertFalse(attempt['quarantine_admission'])
                self.assertEqual(attempt['recheck_route']['kind'], 'public_board')
                self.assertFalse(service.posting_is_current(current))
                self.assertEqual(report['committed_verdicts'], {'none': 1})
                self.assertEqual(report['promoted_to_ready'], 0)

    def test_81_and_169_failure_replay_preserves_every_prior_track(self):
        for size in (81, 169):
            with self.subTest(size=size):
                before = [self.prior_live(queued(i)) for i in range(1, size+1)]
                _, after, report = self.run_failure(TimeoutError('fixture'), before)
                self.assertEqual(report['requests'], 1)
                self.assertEqual(report['signals'], {'NONE': size})
                self.assertEqual(len(report['transport_cohorts']), 1)
                self.assertEqual(report['transport_cohorts'][0]['role_attempt_count'], size)
                self.assertEqual(len({r['verification_attempt']['transport_cohort_id'] for r in after}), 1)
                for old, current in zip(before, after):
                    for key, value in old.items():
                        self.assertEqual(canonical(current[key]), canonical(value))
                    self.assertFalse(current['verification_attempt']['quarantine_admission'])

    def test_one_escalated_timeout_retry_persists_both_dispatches(self):
        timeouts = []
        def fetch(url, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise TimeoutError('fixture')
            return {'jobs': [{'id': 1, 'title': 'Fixture'}]}
        atomic_json(self.queue, [queued(1)])
        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=fetch, retry_timeouts=True))
        self.assertEqual(timeouts, [10, 20])
        attempt = read_json(self.queue)[0]['verification_attempt']
        self.assertEqual(attempt['signal'], 'LIVE')
        self.assertEqual([x['transport_class'] for x in attempt['evidence']['request_attempts']], ['TIMEOUT', 'OK'])
        self.assertEqual(report['requests'], 2)

    def test_second_timeout_and_request_budget_are_absolute_retry_bounds(self):
        before, after, report = self.run_failure(TimeoutError('fixture'), retry=True)
        self.assertEqual(report['requests'], 2)
        self.assertEqual(len(after[0]['verification_attempt']['evidence']['request_attempts']), 2)
        atomic_json(self.queue, before)
        reader = service.PublicBoardReader(fetcher=lambda *_: (_ for _ in ()).throw(TimeoutError('fixture')),
                                           retry_timeouts=True, max_requests=1)
        report = service.verify(self.home, live=True, reader=reader)
        self.assertEqual(report['requests'], 1)

    def test_rate_limit_never_retries_or_dispatches_following_board(self):
        called = []
        def fetch(url, timeout):
            called.append(url)
            raise HTTPError(url, 429, 'fixture', {}, None)
        atomic_json(self.queue, [queued(1, board='a'), queued(2, board='b')])
        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=fetch, retry_timeouts=True))
        self.assertEqual(len(called), 1)
        self.assertEqual(report['deferred_without_attempt'], {'http_429': 1})
        self.assertNotIn('verification_attempt', read_json(self.queue)[1])

    def test_retry_cooldown_is_separate_from_preserved_live_evidence(self):
        _, after, _ = self.run_failure(TimeoutError('fixture'))
        current = after[0]
        current.pop('verification_event_pending')
        atomic_json(self.queue, [current])
        self.assertFalse(service.posting_is_current(current))
        supply = service.supply_report(self.home)
        self.assertEqual(supply['mutually_exclusive_supply_states'], {'verification_cooldown': 1})
        report = service.verify(self.home, reader=service.PublicBoardReader(fetcher=lambda *_: {'jobs': []}))
        self.assertEqual(report['requests'], 0)
        self.assertEqual(report['skipped'], {'cooldown': 1})

    def test_complete_board_absence_is_lead_ambiguity_with_supported_recheck(self):
        atomic_json(self.queue, [self.prior_live(queued(1))])
        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=lambda *_: {'jobs': []}))
        current = read_json(self.queue)[0]
        self.assertEqual(current['verification_attempt']['signal'], 'AMBIGUOUS')
        self.assertTrue(current['verification_attempt']['lead_attributed'])
        self.assertEqual(current['posting_verification']['verdict'], 'ambiguous')
        self.assertEqual(report['verdicts'], {'ambiguous': 1})
        self.assertEqual(current['status'], 'PARKED-PENDING-VERIFICATION')

    def test_board_404_or_410_cannot_become_posting_death(self):
        for status in (404, 410):
            with self.subTest(status=status):
                _, after, report = self.run_failure(HTTPError('https://boards-api.greenhouse.io/', status,
                                                              'fixture', {}, None))
                self.assertEqual(after[0]['verification_attempt']['signal'], 'NONE')
                self.assertEqual(report['verdicts'], {'none': 1})
                self.assertEqual(after[0]['status'], 'PARKED-PENDING-VERIFICATION')

    def test_held_duplicate_role_does_not_block_other_role_at_same_company(self):
        first, second = queued(1), queued(2)
        atomic_json(self.queue, [first, second])
        atomic_json(self.home / 'data/application-ledger.json', [{**first, 'status': 'SUBMITTED'}])
        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=lambda *_: {
                                    'jobs': [{'id': 1, 'title': 'First'}, {'id': 2, 'title': 'Second'}]}))
        self.assertEqual(report['selected'], 1)
        self.assertEqual(read_json(self.queue)[0], first)
        self.assertEqual(read_json(self.queue)[1]['verification_attempt']['signal'], 'LIVE')

    def test_failed_retry_does_not_starve_never_attempted_tail_after_cooldown(self):
        _, after, _ = self.run_failure(TimeoutError('fixture'))
        current = after[0]
        current.pop('verification_event_pending')
        current['verification_attempt']['next_eligible_at'] = (service.utc_now()-timedelta(seconds=1)).isoformat()
        atomic_json(self.queue, [current, queued(2, board='other')])
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            return {'jobs': [{'id': 2, 'title': 'Tail'}]}
        report = service.verify(self.home, limit=1, reader=service.PublicBoardReader(fetcher=fetch))
        self.assertIn('/other/', calls[0])
        self.assertEqual(report['candidate_diagnostics']['scannable'], 2)
        self.assertEqual(report['deferred_by_limit'], 1)

    def test_candidate_diagnostics_separate_fit_floor_without_changing_scan_policy(self):
        atomic_json(self.queue, [queued(1, fit=75), queued(2, fit=74),
                                 queued(3, fit=None), queued(4, fit=True)])
        report = service.verify(self.home, reader=service.PublicBoardReader(fetcher=lambda *_: {'jobs': []}))
        diagnostics = report['candidate_diagnostics']
        self.assertEqual(diagnostics['scannable'], 4)
        self.assertEqual(diagnostics['scannable_at_main_floor'], 1)
        self.assertEqual(diagnostics['scannable_below_main_floor'], 1)
        self.assertEqual(diagnostics['unknown_fit'], 2)
        self.assertEqual(diagnostics['main_fit_floor'], 75)
        self.assertEqual(report['observed'], 4)

    def selected_board(self, observations, *, limit=1):
        entries = []
        for index, (board, observation) in enumerate(observations, 1):
            row = queued(index, board=board)
            if observation is not None:
                row['posting_verification'] = observation
            entries.append(row)
        atomic_json(self.queue, entries)
        calls = []
        def fetch(url, timeout):
            calls.append(url.split('/boards/')[1].split('/')[0])
            return {'jobs': []}
        report = service.verify(self.home, limit=limit,
                                reader=service.PublicBoardReader(fetcher=fetch))
        self.assertTrue(report['dry_run'])
        self.assertFalse(report['submission_authorized'])
        self.assertEqual(read_json(self.queue), entries)
        return calls, report

    def test_oldest_first_compares_instants_across_offsets(self):
        calls, report = self.selected_board([
            ('newer', {'observed_at': '2026-01-01T09:00:00+00:00'}),
            ('older', {'observed_at': '2026-01-01T10:00:00+02:00'}),
        ])
        self.assertEqual(calls, ['older'])
        self.assertEqual(report['deferred_by_limit'], 1)
        self.assertEqual(report['requests'], 1)

    def test_equivalent_instants_keep_stable_role_id_tie(self):
        for stamp in ('2026-01-01T10:00:00+02:00', '2026-01-01T08:00:00Z',
                      '2026-01-01T08:00:00.000000+00:00'):
            with self.subTest(stamp=stamp):
                calls, _ = self.selected_board([
                    ('first', {'observed_at': stamp}),
                    ('second', {'observed_at': '2026-01-01T08:00:00+00:00'}),
                ])
                self.assertEqual(calls, ['first'])

    def test_never_attempted_and_missing_observation_keep_priority(self):
        for observation in (None, {}):
            with self.subTest(observation=observation):
                calls, _ = self.selected_board([
                    ('dated', {'observed_at': '2026-01-01T08:00:00+00:00'}),
                    ('unobserved', observation),
                ])
                self.assertEqual(calls, ['unobserved'])

    def test_malformed_observation_fallback_preserves_cooldown_exclusions(self):
        for stamp in (None, 42, 'not-a-date', '2026-01-01T08:00:00',
                      '0001-01-01T00:00:00+01:00'):
            with self.subTest(stamp=stamp):
                calls, report = self.selected_board([
                    ('eligible', {'observed_at': stamp}),
                    ('invalid', {'observed_at': stamp, 'next_eligible_at': 'invalid'}),
                    ('cooldown', {'observed_at': stamp, 'next_eligible_at':
                                  (service.utc_now()+timedelta(days=1)).isoformat()}),
                ], limit=3)
                self.assertEqual(calls, ['eligible'])
                self.assertEqual(report['skipped'], {'invalid_cooldown_state': 1, 'cooldown': 1})

    def test_overflowing_cooldown_holds_only_the_invalid_row(self):
        for stamp in ('0001-01-01T00:00:00+01:00', '9999-12-31T23:59:59-01:00'):
            with self.subTest(stamp=stamp):
                invalid, eligible = queued(1), queued(2)
                invalid['posting_verification'] = {'next_eligible_at': stamp}
                atomic_json(self.queue, [invalid, eligible])
                report = service.verify(self.home, live=True,
                    reader=service.PublicBoardReader(fetcher=lambda *_: {
                        'jobs': [{'id': 2, 'title': 'Synthetic eligible posting'}]}))
                self.assertEqual(report['skipped'], {'invalid_cooldown_state': 1})
                self.assertEqual(report['requests'], 1)
                self.assertEqual(report['committed'], 1)
                self.assertFalse(report['submission_authorized'])
                after = read_json(self.queue)
                self.assertEqual(canonical(after[0]), canonical(invalid))
                self.assertEqual(after[1]['verification_attempt']['signal'], 'LIVE')
                self.assertFalse(after[1]['verification_attempt']['execution_authorized'])

    def test_dry_run_and_supply_refuse_pending_recovery_without_writes(self):
        atomic_json(self.queue, [queued(1)])
        destination = self.home / 'data/queues/strategic-queue.json'
        def stop(step, journal):
            if step == 'write:0':
                raise OSError('injected interruption')
        with patch.object(queue_io, '_transaction_step', side_effect=stop):
            with self.assertRaises(OSError):
                queue_io.move_entry_atomic('001-fixture', str(self.queue), str(destination))
        paths = [self.queue, destination, *Path(queue_io._transaction_dir()).glob('*.json')]
        before = {str(path): path.read_bytes() for path in paths}
        with self.assertRaises(queue_io.QueueRecoveryRequired):
            service.verify(self.home, reader=service.PublicBoardReader(fetcher=lambda *_: {'jobs': []}))
        with self.assertRaises(queue_io.QueueRecoveryRequired):
            service.supply_report(self.home)
        self.assertEqual({str(path): path.read_bytes() for path in paths}, before)


if __name__ == '__main__':
    unittest.main()
