"""Real source intake and durable cursor regressions, entirely offline."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import pipeline_service as pipeline
import queue_io
import source_scheduler as scheduler
from safe_io import atomic_json, file_lock, read_json


class SourceSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='keel-scheduler-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 1000
        self.calls = []
        self.queue = self.home / 'data/queues/standard-queue.json'
        for name in pipeline.QUEUES:
            atomic_json(self.home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        self.lock_patch = patch.object(queue_io, '_LOCK_PATH', str(self.home / 'queue.lock'))
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

    def sources(self, count):
        for index in range(count):
            pipeline.add_source(self.home, 'greenhouse:fixture-' + str(index).zfill(3))

    def fetch(self, url, timeout):
        self.calls.append(url)
        return {'jobs': [{'id': 1, 'title': 'Synthetic role', 'location': {'name': 'Synthetic'}}]}

    def run_batch(self, **kwargs):
        reader = kwargs.pop('reader', pipeline.PublicBoardReader(fetcher=self.fetch))
        return scheduler.scheduled_discover(self.home, reader=reader, clock=lambda: self.now, **kwargs)

    def test_more_boards_than_reader_budget_make_fair_progress(self):
        self.sources(64)
        baseline = pipeline.discover(self.home, reader=pipeline.PublicBoardReader(fetcher=self.fetch))
        self.assertEqual(baseline['status'], 'HELD_DEADLINE')
        self.assertEqual(baseline['added'], 0)
        reports = [self.run_batch(max_boards=16, max_new=16) for _ in range(4)]
        self.assertEqual([r['added'] for r in reports], [16] * 4)
        self.assertEqual(sum(r['requests'] for r in reports), 64)
        refs = [ref for report in reports for ref in report['attempted']]
        self.assertEqual(len(set(refs)), 64)
        self.assertEqual(len(read_json(self.queue)), 64)
        self.assertTrue(all(row['status'] == 'PARKED-PENDING-VERIFICATION' and
                            row['execution_authorized'] is False for row in read_json(self.queue)))

    def test_intake_allocation_cannot_starve_selected_peers(self):
        self.sources(6)
        first = self.run_batch(max_boards=4, max_new=3)
        second = self.run_batch(max_boards=4, max_new=3)
        self.assertEqual(first['added'], 3)
        self.assertEqual(second['added'], 3)
        self.assertEqual([row['allocation'] for row in first['sources']], [1, 1, 1])
        self.assertFalse(set(first['attempted']) & set(second['attempted']))

    def test_validated_subset_cannot_activate_disabled_or_unknown_source(self):
        self.sources(1)
        for refs in (['greenhouse:unregistered'], [], ['greenhouse:fixture-000'] * 2):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                pipeline.discover(self.home, reader=pipeline.PublicBoardReader(fetcher=self.fetch), source_refs=refs)
        config = read_json(self.home / 'data/sources.json')
        config['sources'][0]['enabled'] = False
        atomic_json(self.home / 'data/sources.json', config)
        with self.assertRaises(ValueError):
            pipeline.discover(self.home, reader=pipeline.PublicBoardReader(fetcher=self.fetch),
                              source_refs=['greenhouse:fixture-000'])
        self.assertEqual(self.calls, [])

    def test_rate_limit_stops_then_survives_reopen(self):
        self.sources(4)
        def limited(url, timeout):
            if '/fixture-001/' in url:
                self.calls.append(url)
                raise HTTPError(url, 429, 'fixture', {}, None)
            return self.fetch(url, timeout)
        result = self.run_batch(reader=pipeline.PublicBoardReader(fetcher=limited))
        self.assertEqual(result['status'], 'HELD_HTTP_429')
        self.assertEqual(result['added'], 1)
        self.assertEqual(len(self.calls), 2)
        held = self.run_batch()
        self.assertEqual(held['status'], 'HELD_HTTP_429')
        self.assertEqual(len(self.calls), 2)
        self.now += 301
        resumed = self.run_batch(max_boards=2)
        self.assertEqual(resumed['attempted'], ['greenhouse:fixture-002', 'greenhouse:fixture-003'])

    def test_shared_request_cap_returns_unattempted_turns(self):
        self.sources(4)
        first = self.run_batch(max_requests=1, reader=pipeline.PublicBoardReader(fetcher=self.fetch, max_requests=1))
        self.assertEqual(first['status'], 'HELD_REQUEST_BUDGET')
        self.assertEqual(first['requests'], 1)
        second = self.run_batch(max_boards=2)
        self.assertEqual(second['attempted'], ['greenhouse:fixture-001', 'greenhouse:fixture-002'])

    def test_unavailable_source_backoff_allows_other_sources(self):
        self.sources(3)
        def unavailable(url, timeout):
            if '/fixture-000/' in url:
                raise HTTPError(url, 503, 'fixture', {}, None)
            return self.fetch(url, timeout)
        report = self.run_batch(reader=pipeline.PublicBoardReader(fetcher=unavailable))
        self.assertEqual(report['status'], 'PARTIAL_SOURCES')
        self.assertEqual(report['added'], 2)
        self.assertEqual(self.run_batch()['status'], 'IDLE')
        self.now += 61
        self.assertEqual(self.run_batch()['attempted'], ['greenhouse:fixture-000'])

    def test_crash_after_commit_stays_unknown_until_deduplicated_recovery(self):
        self.sources(1)
        saved, count = scheduler._save, 0
        def crash(path, state, now):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('fixture process boundary after queue commit')
            return saved(path, state, now)
        with patch.object(scheduler, '_save', crash), self.assertRaises(OSError):
            self.run_batch()
        self.assertEqual(len(read_json(self.queue)), 1)
        self.assertEqual(self.run_batch()['status'], 'HELD_RECOVERY')
        before = self.queue.read_bytes()
        result = scheduler.recover_interrupted(self.home, clock=lambda: self.now)
        self.assertEqual(result['previous_outcome'], 'UNKNOWN')
        self.assertIsNone(result['previous_added'])
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(self.run_batch()['status'], 'HELD_HTTP_429')
        self.now += 301
        replay = self.run_batch()
        self.assertEqual(replay['added'], 0)
        self.assertEqual(replay['sources'][0]['result']['duplicate_observations'], 1)
        self.assertEqual(len(read_json(self.queue)), 1)

    def test_application_unknown_and_holds_are_never_reconciled_by_scheduler(self):
        self.sources(1)
        row = {'role_id': 'existing', 'application_url': 'https://job-boards.greenhouse.io/fixture-000/jobs/1',
               'status': 'UNKNOWN_OUTCOME', 'holds': ['unknown_application_attempt']}
        atomic_json(self.queue, [row])
        self.assertEqual(self.run_batch()['added'], 0)
        self.assertEqual(read_json(self.queue), [row])

    def test_two_controllers_cannot_overlap(self):
        self.sources(1)
        _, _, lock = scheduler._paths(self.home)
        with file_lock(lock, timeout=0), ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(TimeoutError):
                pool.submit(self.run_batch).result(timeout=3)
        self.assertEqual(self.calls, [])

    def test_clock_regression_refuses_reads(self):
        self.sources(1)
        self.run_batch()
        self.now -= 1
        with self.assertRaisesRegex(ValueError, 'clock'):
            self.run_batch()
        self.assertEqual(len(self.calls), 1)

    def test_tampered_cursor_fails_closed(self):
        self.sources(1)
        self.run_batch()
        path = self.home / 'data/source-scheduler.json'
        state = read_json(path)
        state['sources']['greenhouse:fixture-000']['last_attempt'] = state['sequence'] + 1
        atomic_json(path, state)
        with self.assertRaises(ValueError):
            self.run_batch()
        self.assertEqual(len(self.calls), 1)

    def test_reader_cannot_extend_scheduler_budget(self):
        self.sources(1)
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.run_batch(max_requests=2)
        self.assertEqual(self.calls, [])

    def test_fifo_state_is_rejected_without_blocking(self):
        import os
        import subprocess
        path=self.home/'data/source-scheduler.json';os.mkfifo(path,0o600)
        script="""import sys
from pathlib import Path
sys.path.insert(0,'engines')
from source_scheduler import _load
try:
    _load(Path(sys.argv[1]),1000)
except ValueError as error:
    assert 'regular file' in str(error)
else:
    raise AssertionError('FIFO was accepted')
"""
        completed=subprocess.run([sys.executable,'-B','-c',script,str(path)],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=3)
        self.assertEqual(completed.returncode,0,completed.stderr.decode())

    def test_offline_demo_includes_actual_process_recovery(self):
        from keel_next.source_scheduler_demo import run_demo
        report = run_demo(self.home / 'demo')
        self.assertEqual(report['status'], 'SOURCE_SCHEDULER_PASSED')
        self.assertTrue(all(report['checks'].values()))
        self.assertEqual(report['crash']['returncode'], -9)
        self.assertEqual(report['network_calls'], 0)


if __name__ == '__main__':
    unittest.main()
