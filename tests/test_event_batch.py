import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from engines import log_event as event


def request(key):
    return {'event_type':'verification_attempt','event_id':key,'role_id':'fixture',
            'source':'batch-test','details':{'verdict':'ambiguous'}}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'events.jsonl'
        p=patch.object(event,'EVENTS',str(self.path));p.start();self.addCleanup(p.stop)

    def records(self):return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_replay_preserves_exact_receipts(self):
        requests=[request(str(i)) for i in range(12)]
        first=event.log_batch(requests)
        self.assertEqual(event.log_batch(requests),first)
        self.assertEqual(len(self.records()),12)

    def test_legacy_and_batch_interoperate(self):
        first=event.log(**request('one'))
        self.assertEqual(event.log_batch([request('one'),request('two')])[0],first)
        self.assertEqual(event.log(**request('two')),self.records()[1])

    def test_late_conflict_appends_nothing(self):
        event.log(**request('existing'));before=self.path.read_bytes()
        conflict=request('existing');conflict['details']={'verdict':'different'}
        with self.assertRaises(ValueError):event.log_batch([request('new'),conflict])
        self.assertEqual(self.path.read_bytes(),before)

    def test_invalid_batch_writes_nothing(self):
        cases=[[],[request('same'),request('same')],[request(str(i)) for i in range(129)],
               [{'event_type':'verification_attempt','event_id':None}],
               [dict(request('large'),details={'text':'x'*(event.MAX_BATCH_BYTES+1)})]]
        for batch in cases:
            with self.subTest(size=len(batch)):
                with self.assertRaises(ValueError):event.log_batch(batch)
                self.assertFalse(self.path.exists())

    def test_unknown_partial_write_replays_without_duplicates(self):
        real=event._append_batch
        def fail(events):
            real(events[:2]);raise OSError('synthetic interrupted append')
        requests=[request(str(i)) for i in range(5)]
        with patch.object(event,'_append_batch',fail):
            with self.assertRaises(OSError):event.log_batch(requests)
        self.assertEqual(len(self.records()),2)
        self.assertEqual(len(event.log_batch(requests)),5)
        self.assertEqual(len(self.records()),5)

    def test_failed_sync_returns_no_receipts_replay_syncs_again(self):
        with patch.object(event,'_sync_event_directory',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):event.log_batch([request('one')])
        with patch.object(event,'_sync_event_directory',wraps=event._sync_event_directory) as sync:
            self.assertEqual(len(event.log_batch([request('one')])),1)
            self.assertEqual(sync.call_count,1)
        self.assertEqual(len(self.records()),1)

    def test_torn_history_is_held_not_repaired(self):
        self.path.write_text('{"event_id":')
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):event.log_batch([request('one')])
        self.assertEqual(self.path.read_bytes(),before)

    def test_conflicting_duplicate_history_is_rejected(self):
        event.log(**request('one'));data=self.path.read_bytes();self.path.write_bytes(data+data)
        with self.assertRaises(ValueError):event.log_batch([request('one')])

    def test_concurrent_replays_serialize(self):
        batch=[request(str(i)) for i in range(10)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:event.log_batch(batch),range(8)))
        self.assertTrue(all(r==results[0] for r in results))
        self.assertEqual(len(self.records()),10)


if __name__=='__main__':unittest.main()
