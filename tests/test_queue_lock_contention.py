"""Real flock contention with bounded, injected metadata and clock failures."""
from contextlib import ExitStack
import errno
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import queue_io


class QueueLockContentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-lock-contention-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / 'queue.lock'
        self.meta = str(self.path) + '.meta'
        Path(self.meta).write_text(json.dumps({'pid': 123456, 'owner': 'synthetic-holder', 'heartbeat': 1}))
        self.queue, self.ledger = self.home / 'queue.json', self.home / 'ledger.json'
        self.queue.write_text('[]'); self.ledger.write_text('[]')
        self.before = (self.queue.read_bytes(), self.ledger.read_bytes())
        self.flock = queue_io.fcntl.flock
        self.remove = queue_io.os.remove
        self.holder = self.path.open('a+')
        self.addCleanup(self.holder.close)
        self.flock(self.holder, queue_io.fcntl.LOCK_EX | queue_io.fcntl.LOCK_NB)
        self.now, self.attempts, self.cleanup_attempts = 0.0, 0, 0
        self.sleeps, self.release_on_sleep = [], False
        stack = ExitStack(); self.addCleanup(stack.close)
        for context in (
            patch.object(queue_io, '_LOCK_PATH', str(self.path)),
            patch.object(queue_io, '_pid_alive', return_value=False),
            patch.object(queue_io.os, 'remove', side_effect=self.deny_metadata_cleanup),
            patch.object(queue_io.fcntl, 'flock', side_effect=self.observe_flock),
            patch.object(queue_io.time, 'monotonic', side_effect=lambda: self.now),
            patch.object(queue_io.time, 'sleep', side_effect=self.sleep),
        ):
            stack.enter_context(context)

    def deny_metadata_cleanup(self, path):
        if str(path) == self.meta:
            self.cleanup_attempts += 1
            raise PermissionError(errno.EACCES, 'synthetic metadata cleanup denial')
        return self.remove(path)

    def observe_flock(self, descriptor, operation):
        if operation & queue_io.fcntl.LOCK_NB:
            self.attempts += 1
            if self.attempts > 16:
                raise RuntimeError('bounded test stopped unpaced lock retries')
        return self.flock(descriptor, operation)

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay
        if self.release_on_sleep:
            self.flock(self.holder, queue_io.fcntl.LOCK_UN)
            self.release_on_sleep = False

    def assert_inputs_unchanged(self):
        self.assertEqual((self.queue.read_bytes(), self.ledger.read_bytes()), self.before)
        self.assertTrue(Path(self.meta).exists())

    def test_stale_cleanup_failure_respects_zero_timeout(self):
        with self.assertRaises(queue_io.QueueLockTimeout) as caught:
            with queue_io.queue_lock(timeout=0):
                self.fail('contender entered protected body')
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(caught.exception.holder_owner, 'synthetic-holder')
        self.assert_inputs_unchanged()

    def test_stale_cleanup_failure_is_paced_and_bounded(self):
        with patch.object(queue_io, '_LOCK_POLL_S', .25):
            with self.assertRaises(queue_io.QueueLockTimeout) as caught:
                with queue_io.queue_lock(timeout=.6):
                    self.fail('contender entered protected body')
        self.assertAlmostEqual(caught.exception.waited_s, .6)
        self.assertTrue(self.sleeps and all(0 < delay <= .25 for delay in self.sleeps))
        self.assertLessEqual(self.attempts, 4)
        self.assert_inputs_unchanged()

    def test_readonly_contender_does_not_clean_metadata_or_recover(self):
        with patch.object(queue_io, '_recover_transactions_locked', side_effect=AssertionError('recovery')):
            with self.assertRaises(queue_io.QueueLockTimeout):
                with queue_io.queue_lock(timeout=0, recover=False):
                    self.fail('contender entered protected body')
        self.assertEqual(self.cleanup_attempts, 0)
        self.assert_inputs_unchanged()

    def test_short_timeout_does_not_sleep_past_remaining_budget(self):
        with self.assertRaises(queue_io.QueueLockTimeout) as caught:
            with queue_io.queue_lock(timeout=.1, recover=False):
                self.fail('contender entered protected body')
        self.assertLessEqual(caught.exception.waited_s, .1)
        self.assert_inputs_unchanged()

    def test_eventual_kernel_release_allows_entry_despite_metadata_failure(self):
        self.release_on_sleep = True
        with queue_io.queue_lock(timeout=1):
            with self.path.open('a+') as contender:
                with self.assertRaises(BlockingIOError):
                    self.flock(contender, queue_io.fcntl.LOCK_EX | queue_io.fcntl.LOCK_NB)
        self.assertEqual(self.attempts, 2)
        self.assertTrue(self.sleeps)
        self.assert_inputs_unchanged()


if __name__ == '__main__':
    unittest.main()
