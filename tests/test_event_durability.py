"""A successful telemetry receipt must cross the local fsync boundary."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Live-tree convention (cf. tests/test_pipeline_scale.py): engines/ must be
# importable for keel_paths; the candidate's harness supplied this via PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))

from engines import log_event


class EventDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'events.jsonl'
        patcher=patch.object(log_event,'EVENTS',str(self.path))
        patcher.start();self.addCleanup(patcher.stop)

    def emit(self):
        return log_event.log('verification_attempt',role_id='synthetic',
                             source='durability-test',details={'verdict':'ambiguous'},
                             event_id='synthetic-event')

    def test_append_and_replay_sync_file_and_directory(self):
        with patch.object(log_event.os,'fsync',wraps=log_event.os.fsync) as sync:
            first=self.emit()
            self.assertGreaterEqual(sync.call_count,2)
            before=sync.call_count
            self.assertEqual(self.emit(),first)
            self.assertGreaterEqual(sync.call_count-before,2)
        self.assertEqual(len(self.path.read_text().splitlines()),1)

    def test_failed_file_sync_never_returns_receipt_and_replay_recovers(self):
        with patch.object(log_event.os,'fsync',side_effect=OSError('synthetic sync failure')):
            with self.assertRaises(OSError):self.emit()
        # The append may exist despite the uncertain return; replay must not
        # append a duplicate and must retry the persistence barrier.
        with patch.object(log_event.os,'fsync',wraps=log_event.os.fsync) as sync:
            receipt=self.emit()
            self.assertGreaterEqual(sync.call_count,2)
        self.assertEqual(receipt['event_id'],'synthetic-event')
        self.assertEqual(len(self.path.read_text().splitlines()),1)

    def test_failed_directory_sync_never_returns_receipt(self):
        with patch.object(log_event,'_sync_event_directory',side_effect=OSError('synthetic directory failure')):
            with self.assertRaises(OSError):self.emit()
        receipt=self.emit()
        self.assertEqual(json.loads(self.path.read_text())['event_id'],receipt['event_id'])

    def test_conflicting_replay_still_fails(self):
        self.emit()
        with self.assertRaises(ValueError):
            log_event.log('verification_attempt',role_id='other',source='durability-test',
                          details={'verdict':'ambiguous'},event_id='synthetic-event')
        self.assertEqual(len(self.path.read_text().splitlines()),1)

    def test_first_use_and_replay_sync_new_directory_ancestors(self):
        nested=Path(self.temp.name)/'new-data'/'new-telemetry'/'events.jsonl'
        with patch.object(log_event,'EVENTS',str(nested)):
            for _ in range(2):
                with patch.object(log_event.os,'open',wraps=log_event.os.open) as opened:
                    self.emit()
                synchronized={str(call.args[0]) for call in opened.call_args_list
                              if call.args[1] & log_event.os.O_DIRECTORY}
                self.assertTrue({str(nested.parent),str(nested.parent.parent),self.temp.name}
                                .issubset(synchronized))


if __name__=='__main__':unittest.main()
