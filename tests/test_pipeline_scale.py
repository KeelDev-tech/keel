"""Regression checks against real pipeline storage with synthetic sources only."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'engines'))
import pipeline_service as service
import queue_io
from safe_io import atomic_json, read_json


def job(i, **kwargs):
    return {'id': i, 'title': f'Synthetic role {i}', 'location': {'name': 'Synthetic'}, **kwargs}


def row(i):
    return {'role_id': 'existing-'+str(i), 'application_url': f'https://job-boards.greenhouse.io/fixture/jobs/{i}',
            'status': 'PARKED-PENDING-VERIFICATION'}


class PipelineScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-pipeline-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.lock = patch.object(queue_io, '_LOCK_PATH', str(self.home/'queue.lock'))
        self.lock.start(); self.addCleanup(self.lock.stop)
        for name in service.QUEUES:
            atomic_json(self.home/'data/queues'/f'{name}-queue.json', [])
        atomic_json(self.home/'data/application-ledger.json', [])
        service.add_source(self.home, 'greenhouse:fixture', 'Fixture')
        self.queue = self.home/'data/queues/standard-queue.json'

    def reader(self, jobs):
        return service.PublicBoardReader(fetcher=lambda *_: {'jobs': jobs})

    def test_default_batch_outbox_keeps_pending_until_directory_sync(self):
        import log_event
        import json
        items = []
        for i in range(4):
            item = row(i)
            item['verification_event_pending'] = {
                'event_type': 'lead_verified', 'event_id': 'batch-outbox-' + str(i),
                'source': 'synthetic', 'details': {'synthetic': True}}
            items.append(item)
        atomic_json(self.queue, items)
        events = self.home / 'events.jsonl'
        with patch.object(log_event, 'EVENTS', str(events)):
            with patch.object(log_event, '_sync_event_directory', side_effect=OSError('fixture')):
                failed = service.flush_outbox(self.home)
            self.assertEqual(failed['emitted'], 0)
            self.assertEqual(failed['pending'], 4)
            self.assertTrue(all(item.get('verification_event_pending') for item in read_json(self.queue)))
            recovered = service.flush_outbox(self.home)
            self.assertEqual(recovered['emitted'], 4)
            self.assertEqual(recovered['pending'], 0)
            self.assertEqual(service.flush_outbox(self.home)['emitted'], 0)
        records = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(len(records), 4)
        self.assertEqual(len({record['event_id'] for record in records}), 4)

    def test_selection_uses_current_identities_and_fills_exact_limit(self):
        atomic_json(self.queue, [row(1)])
        def fetch(*_):
            with queue_io.queue_lock(timeout=2, owner='synthetic-concurrent-writer'):
                atomic_json(self.queue, [row(1), row(3)])
            return {'jobs': [job(i) for i in range(1, 7)]}
        result = service.discover(self.home, max_new=3, reader=service.PublicBoardReader(fetcher=fetch))
        self.assertEqual(result['added'], 3)
        self.assertEqual([r['source_identity'][2] for r in read_json(self.queue)[2:]], ['2','4','5'])
        self.assertEqual(result['candidate_records'], 6)

    def test_spool_record_overflow_never_commits_prefix(self):
        before = self.queue.read_bytes()
        with patch.object(service, 'MAX_CANDIDATE_RECORDS', 2):
            result = service.discover(self.home, max_new=1, reader=self.reader([job(i) for i in range(3)]))
        self.assertEqual(result['status'], 'HELD_CAPACITY')
        self.assertEqual(self.queue.read_bytes(), before)

    def test_spool_byte_overflow_never_commits_prefix(self):
        with patch.object(service, 'MAX_CANDIDATE_BYTES', 100):
            result = service.discover(self.home, reader=self.reader([job(1)]))
        self.assertEqual(result['status'], 'HELD_CAPACITY')
        self.assertEqual(read_json(self.queue), [])

    def test_spool_closed_after_hold(self):
        real, captured = tempfile.TemporaryFile, []
        def temporary(*args, **kwargs):
            value = real(*args, **kwargs); captured.append(value); return value
        with patch.object(service.tempfile, 'TemporaryFile', temporary), patch.object(service, 'MAX_CANDIDATE_RECORDS', 1):
            service.discover(self.home, reader=self.reader([job(1), job(2)]))
        self.assertTrue(captured[0].closed)

    def test_storage_failure_does_not_commit_prefix(self):
        class Broken:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def write(self, data): raise OSError('synthetic full disk')
        with patch.object(service.tempfile, 'TemporaryFile', return_value=Broken()):
            result = service.discover(self.home, reader=self.reader([job(1)]))
        self.assertEqual(result['status'], 'HELD_STORAGE')
        self.assertEqual(read_json(self.queue), [])

    def test_429_after_valid_board_holds_whole_run(self):
        service.add_source(self.home, 'greenhouse:second')
        def fetch(url, timeout):
            if '/second/' in url: raise HTTPError(url, 429, 'fixture', {}, None)
            return {'jobs': [job(1)]}
        result = service.discover(self.home, reader=service.PublicBoardReader(fetcher=fetch))
        self.assertEqual(result['status'], 'HELD_HTTP_429')
        self.assertEqual(read_json(self.queue), [])

    def test_existing_ledger_identity_is_not_reingested(self):
        atomic_json(self.home/'data/application-ledger.json', [row(1)])
        result = service.discover(self.home, max_new=1, reader=self.reader([job(1),job(2)]))
        self.assertEqual(result['added'], 1)
        self.assertEqual(read_json(self.queue)[0]['source_identity'][2], '2')

    def test_lru_cache_evicts_whole_boards_and_refetches(self):
        reader = self.reader([job(1), job(2)])
        with patch.object(service, 'MAX_CACHE_RECORDS', 2):
            reader.read('greenhouse:a'); reader.read('greenhouse:b')
            self.assertEqual(list(reader.cache), ['greenhouse:b'])
            self.assertEqual(reader.cache_records, 2)
            reader.read('greenhouse:a')
        self.assertEqual(reader.requests, 3)
        self.assertEqual(reader.peak_cache_records, 2)

    def test_board_capacity_rejects_entire_board(self):
        reader = self.reader([job(1), job(2)])
        with patch.object(service, 'MAX_BOARD_BYTES', 1):
            with self.assertRaises(service.SourceCapacityError): reader.read('greenhouse:a')
        self.assertFalse(reader.cache)

    def test_cached_board_cannot_ignore_expired_deadline(self):
        reader = self.reader([job(1)])
        reader.read('greenhouse:a'); reader.deadline = 0
        with self.assertRaises(TimeoutError): reader.read('greenhouse:a')
        self.assertEqual(reader.requests, 1)

    def test_fetch_return_after_deadline_is_rejected(self):
        reader = None
        def slow(*_):
            reader.deadline = 0
            return {'jobs': [job(1)]}
        reader = service.PublicBoardReader(fetcher=slow)
        with self.assertRaises(TimeoutError): reader.read('greenhouse:a')
        self.assertFalse(reader.cache)

    def test_normalization_checks_cooperative_deadline(self):
        reader = self.reader([job(i) for i in range(64)])
        original, count = service.utc_now, 0
        def stamp():
            nonlocal count
            count += 1
            if count == 1: reader.deadline = 0
            return original()
        with patch.object(service, 'utc_now', stamp):
            with self.assertRaises(TimeoutError): reader.read('greenhouse:a')
        self.assertLessEqual(count, 32)
        self.assertFalse(reader.cache)

    def test_discovery_expiration_holds_without_writing(self):
        reader = self.reader([job(1)]); reader.deadline = 0
        self.assertEqual(service.discover(self.home, reader=reader)['status'], 'HELD_DEADLINE')
        self.assertEqual(read_json(self.queue), [])

    def test_bad_payload_field_types_reject_complete_board(self):
        for malformed in ({'isListed':'false'}, {'isListed':0}, {'location':[]},
                          {'location':'Somewhere'}, {'location':{'name':False}},
                          {'absolute_url':17}, {'absolute_url':False}, {'hostedUrl':0}, {'title':''}, {'location':{'name':'x'*2001}}):
            with self.subTest(malformed=malformed):
                reader = self.reader([job(1), job(2, **malformed)])
                with self.assertRaises(ValueError): reader.read('greenhouse:a')
                self.assertFalse(reader.cache)

    def test_invalid_enabled_registry_flag_rejected(self):
        document = read_json(self.home/'data/sources.json')
        document['sources'][0]['enabled'] = 'true'
        atomic_json(self.home/'data/sources.json', document)
        with self.assertRaises(ValueError): service.discover(self.home, reader=self.reader([job(1)]))

    def test_unlisted_ashby_direct_link_remains_verifiable(self):
        atomic_json(self.home/'data/sources.json', {'sources':[{'ref':'ashby:fixture','enabled':True}]})
        url = 'https://jobs.ashbyhq.com/fixture/12345678-abcd-1234-abcd-123456789abc'
        fixture = {'jobs':[{'jobUrl':url, 'title':'Synthetic direct link', 'location':'Synthetic', 'isListed':False}]}
        result = service.discover(self.home, reader=service.PublicBoardReader(fetcher=lambda *_: fixture))
        self.assertEqual(result['added'], 0)
        atomic_json(self.queue, [{'role_id':'direct-link','application_url':url,'status':'PARKED-PENDING-VERIFICATION'}])
        verified = service.verify(self.home, reader=service.PublicBoardReader(fetcher=lambda *_: fixture))
        self.assertEqual(verified['verdicts'], {'live':1})

    def test_verification_deadline_rechecked_after_commit_lock(self):
        atomic_json(self.queue, [row(1)])
        before = self.queue.read_bytes()
        reader = self.reader([job(1)])
        original = service.queue_lock
        @contextmanager
        def late_lock(*args, **kwargs):
            with original(*args, **kwargs):
                if kwargs.get('owner') == 'public-verify:commit': reader.deadline = 0
                yield
        with patch.object(service, 'queue_lock', late_lock):
            with self.assertRaises(TimeoutError): service.verify(self.home, live=True, reader=reader)
        self.assertEqual(self.queue.read_bytes(), before)

    def test_verification_later_file_deadline_reports_partial_commit(self):
        import log_event
        atomic_json(self.queue, [row(1)])
        second = self.home/'data/queues/strategic-queue.json'
        atomic_json(second, [row(2)])
        before = second.read_bytes()
        reader = self.reader([job(1),job(2)])
        original = service.atomic_json
        def write(path, document):
            original(path, document)
            if path == self.queue: reader.deadline = 0
        with patch.object(service, 'atomic_json', write), patch.object(log_event, 'EVENTS', str(self.home/'events.jsonl')):
            result = service.verify(self.home, live=True, reader=reader)
        self.assertEqual(result['committed'], 1)
        self.assertEqual(len(result['write_errors']), 1)
        self.assertEqual(second.read_bytes(), before)

    def test_duplicate_queue_role_does_not_commit_verification(self):
        atomic_json(self.queue, [row(1), row(1)])
        result = service.verify(self.home, reader=self.reader([job(1)]))
        self.assertEqual(result['selected'], 0)
        self.assertEqual(result['skipped']['duplicate_or_invalid_role_id'], 2)


if __name__ == '__main__': unittest.main()
