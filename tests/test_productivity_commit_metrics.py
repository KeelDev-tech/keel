"""Count only persisted posting evidence, never inferred completion totals."""
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import pipeline_service as service
import queue_io
from safe_io import atomic_json, read_json


def queued(job, *, board='fixture'):
    return {'role_id': f'{job:03d}-{board}',
            'application_url': f'https://job-boards.greenhouse.io/{board}/jobs/{job}',
            'status': 'PARKED-PENDING-VERIFICATION', 'holds': []}


class ProductivityCommitMetricsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-productivity-metrics-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.queue = self.home / 'data/queues/standard-queue.json'
        self.other_queue = self.home / 'data/queues/strategic-queue.json'
        self.ledger = self.home / 'data/application-ledger.json'
        for name in service.QUEUES:
            atomic_json(self.home / f'data/queues/{name}-queue.json', [])
        atomic_json(self.ledger, [])
        lock = patch.object(queue_io, '_LOCK_PATH', str(self.home / 'queue.lock'))
        lock.start()
        self.addCleanup(lock.stop)
        # Keep the durable outbox in each committed row; delivery is independent
        # of this metric, and must not perform unrelated writes during fixtures.
        flush = patch.object(service, 'flush_outbox', return_value={'emitted': 0, 'pending': 0})
        flush.start()
        self.addCleanup(flush.stop)

    def reader(self, *jobs):
        return service.PublicBoardReader(fetcher=lambda *_: {
            'jobs': [{'id': job, 'title': f'Synthetic role {job}'} for job in jobs]})

    def assert_conserved(self, report):
        self.assertEqual(sum(report['committed_verdicts'].values()), report['committed'])
        for verdict, count in report['committed_verdicts'].items():
            self.assertLessEqual(count, report['verdicts'][verdict])
        self.assertEqual(report['promoted_to_ready'], 0)
        self.assertFalse(report['submission_authorized'])

    def test_real_live_commits_are_counted_and_persisted(self):
        atomic_json(self.queue, [queued(1), queued(2)])
        report = service.verify(self.home, live=True, reader=self.reader(1, 2))
        self.assertEqual(report['committed_verdicts'], {'live': 2})
        self.assert_conserved(report)
        rows = read_json(self.queue)
        self.assertTrue(all(row['posting_verification']['verdict'] == 'live' for row in rows))
        stored = read_json(self.home / 'data/verification-runs' / (report['run_id'] + '.json'))
        self.assertEqual(stored['committed_verdicts'], report['committed_verdicts'])

    def test_dry_run_never_credits_observed_live_or_ambiguous_rows(self):
        before = [queued(1), queued(2)]
        atomic_json(self.queue, before)
        report = service.verify(self.home, reader=self.reader(1))
        self.assertEqual(report['verdicts'], {'live': 1, 'ambiguous': 1})
        self.assertEqual(report['committed_verdicts'], {})
        self.assert_conserved(report)
        self.assertEqual(read_json(self.queue), before)

    def test_ambiguous_commit_is_not_a_live_confirmation(self):
        atomic_json(self.queue, [queued(1)])
        report = service.verify(self.home, live=True, reader=self.reader())
        self.assertEqual(report['committed_verdicts'], {'ambiguous': 1})
        self.assert_conserved(report)
        self.assertEqual(read_json(self.queue)[0]['posting_verification']['verdict'], 'ambiguous')

    def test_concurrent_live_conflict_cannot_inflate_ambiguous_commit(self):
        before = [queued(1), queued(2)]
        atomic_json(self.queue, before)

        def fetch(*_):
            atomic_json(self.ledger, [{**before[0], 'role_id': 'other-ledger-id',
                                      'status': 'UNKNOWN_OUTCOME'}])
            return {'jobs': [{'id': 1, 'title': 'Synthetic role 1'}]}

        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=fetch))
        self.assertEqual(report['verdicts'], {'live': 1, 'ambiguous': 1})
        self.assertEqual(report['committed'], 1)
        self.assertEqual(report['committed_verdicts'], {'ambiguous': 1})
        self.assertEqual(report['concurrent_conflicts'], [before[0]['role_id']])
        self.assert_conserved(report)
        self.assertEqual(read_json(self.queue)[0], before[0])

    def test_failed_queue_write_has_no_completion_credit(self):
        before = [queued(1)]
        atomic_json(self.queue, before)

        def write(path, document):
            if Path(path) == self.queue:
                raise OSError('synthetic full disk')
            return atomic_json(path, document)

        with patch.object(service, 'atomic_json', side_effect=write):
            report = service.verify(self.home, live=True, reader=self.reader(1))
        self.assertEqual(report['verdicts'], {'live': 1})
        self.assertEqual(report['committed_verdicts'], {})
        self.assertEqual(len(report['write_errors']), 1)
        self.assert_conserved(report)
        self.assertEqual(read_json(self.queue), before)

    def test_mixed_queue_writes_count_only_each_successful_file(self):
        for failed, expected in ((self.queue, {'ambiguous': 1}),
                                 (self.other_queue, {'live': 1})):
            with self.subTest(failed=failed.name):
                atomic_json(self.queue, [queued(1)])
                atomic_json(self.other_queue, [queued(2)])

                def write(path, document):
                    if Path(path) == failed:
                        raise OSError('synthetic full disk')
                    return atomic_json(path, document)

                with patch.object(service, 'atomic_json', side_effect=write):
                    report = service.verify(self.home, live=True, reader=self.reader(1))
                self.assertEqual(report['verdicts'], {'live': 1, 'ambiguous': 1})
                self.assertEqual(report['committed_verdicts'], expected)
                self.assertEqual(report['committed'], 1)
                self.assertEqual(len(report['write_errors']), 1)
                self.assert_conserved(report)
                self.assertNotIn('posting_verification', read_json(failed)[0])

    def test_http_429_withheld_batch_never_credits_prior_live_evidence(self):
        atomic_json(self.queue, [queued(1, board='a'), queued(2, board='b')])

        def fetch(url, timeout):
            if '/b/' in url:
                raise HTTPError(url, 429, 'synthetic rate limit', {}, None)
            return {'jobs': [{'id': 1, 'title': 'Synthetic role 1'}]}

        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=fetch))
        self.assertTrue(report['rate_limit_hold'])
        self.assertEqual(report['committed_verdicts'], {'ambiguous': 2})
        self.assert_conserved(report)
        self.assertTrue(all(row['posting_verification']['reason'] == 'batch_withheld_after_http_429'
                            for row in read_json(self.queue)))

    def test_idle_run_has_no_completion_credit(self):
        atomic_json(self.queue, [{**queued(1), 'holds': ['consent_quarantine']}])
        report = service.verify(self.home, live=True, reader=self.reader(1))
        self.assertEqual(report['requests'], 0)
        self.assertEqual(report['committed_verdicts'], {})
        self.assert_conserved(report)

    def test_retargeted_posting_requires_new_observation_without_old_cooldown(self):
        for change_role_id in (False, True):
            with self.subTest(change_role_id=change_role_id):
                atomic_json(self.queue, [queued(1)])
                service.verify(self.home, live=True, reader=self.reader(1))
                current = read_json(self.queue)[0]
                # Represent normal outbox delivery before a subsequent row edit.
                current.pop('verification_event_pending')
                current['application_url'] = 'https://job-boards.greenhouse.io/other/jobs/2'
                if change_role_id:
                    current['role_id'] = 'retargeted-role'
                atomic_json(self.queue, [current])
                prior = current['posting_verification']
                self.assertEqual(prior['identity'], ['greenhouse', 'fixture', '1'])
                self.assertGreater(service.aware_time(prior['next_eligible_at']), service.utc_now())
                supply = service.supply_report(self.home)
                self.assertEqual(supply['mutually_exclusive_supply_states'], {'actionable_verification': 1})
                report = service.verify(self.home, live=True, reader=self.reader(2))
                self.assertEqual(report['requests'], 1)
                self.assertEqual(report['committed_verdicts'], {'live': 1})
                current = read_json(self.queue)[0]
                self.assertEqual(current['posting_verification']['identity'], ['greenhouse', 'other', '2'])
                self.assertEqual(current['status'], 'PARKED-PENDING-VERIFICATION')

    def test_absent_or_malformed_observation_identity_never_counts_as_fresh(self):
        invalid = [None, [], ['greenhouse', 'fixture'], ['greenhouse', 'fixture', 1],
                   ['greenhouse', 'fixture', '2'], 'greenhouse:fixture:1', {'job': '1'}]
        for present, identity in [(False, None)] + [(True, value) for value in invalid]:
            with self.subTest(present=present, identity=identity):
                observation = {'verdict': 'live', 'observed_at': service.utc_now().isoformat()}
                if present:
                    observation['identity'] = identity
                atomic_json(self.queue, [{**queued(1), 'posting_verification': observation}])
                supply = service.supply_report(self.home)
                self.assertEqual(supply['mutually_exclusive_supply_states'], {'actionable_verification': 1})

    def test_matching_identity_retains_fresh_presence_and_retry_cooldown(self):
        atomic_json(self.queue, [queued(1)])
        service.verify(self.home, live=True, reader=self.reader(1))
        current = read_json(self.queue)[0]
        current.pop('verification_event_pending')
        atomic_json(self.queue, [current])
        self.assertEqual(service.supply_report(self.home)['mutually_exclusive_supply_states'],
                         {'posting_verified_form_and_approval_separate': 1})
        report = service.verify(self.home, live=True, reader=self.reader(1))
        self.assertEqual(report['requests'], 0)
        self.assertEqual(report['skipped'], {'cooldown': 1})

    def test_legacy_cooldown_without_identity_remains_held_without_presence_credit(self):
        observation = {'verdict': 'live', 'observed_at': service.utc_now().isoformat(),
                       'next_eligible_at': (service.utc_now() + timedelta(minutes=5)).isoformat()}
        atomic_json(self.queue, [{**queued(1), 'posting_verification': observation}])
        self.assertEqual(service.supply_report(self.home)['mutually_exclusive_supply_states'],
                         {'verification_cooldown': 1})
        report = service.verify(self.home, live=True, reader=self.reader(1))
        self.assertEqual(report['requests'], 0)
        self.assertEqual(report['skipped'], {'cooldown': 1})

    def test_retargeted_row_still_obeys_persistent_host_rate_limit(self):
        atomic_json(self.queue, [queued(1)])
        service.verify(self.home, live=True, reader=self.reader(1))
        current = read_json(self.queue)[0]
        current.pop('verification_event_pending')
        current['application_url'] = queued(2)['application_url']
        atomic_json(self.queue, [current])

        def held(*_):
            raise service.HostRateLimited('synthetic persisted host cooldown')

        report = service.verify(self.home, live=True,
                                reader=service.PublicBoardReader(fetcher=held))
        self.assertTrue(report['rate_limit_hold'])
        self.assertEqual(report['committed_verdicts'], {})
        self.assertEqual(report['deferred_without_attempt'], {'persisted_http_429': 1})
        self.assertEqual(read_json(self.queue), [current])

    def test_retargeted_row_cannot_clear_human_hold(self):
        atomic_json(self.queue, [queued(1)])
        service.verify(self.home, live=True, reader=self.reader(1))
        current = read_json(self.queue)[0]
        current.pop('verification_event_pending')
        current['application_url'] = queued(2)['application_url']
        current['holds'] = ['consent_quarantine']
        atomic_json(self.queue, [current])
        self.assertEqual(service.supply_report(self.home)['mutually_exclusive_supply_states'],
                         {'held_active_or_terminal': 1})
        report = service.verify(self.home, live=True, reader=self.reader(2))
        self.assertEqual(report['requests'], 0)
        self.assertEqual(report['committed_verdicts'], {})
        self.assertEqual(read_json(self.queue), [current])


if __name__ == '__main__':
    unittest.main()
