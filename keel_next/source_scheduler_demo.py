"""Offline measurable source fairness and actual-process recovery exercise."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from unittest.mock import patch


def _fixture(url, timeout):
    if not url.startswith('https://boards-api.greenhouse.io/v1/boards/fixture-') or not 0 < timeout <= 20:
        raise ValueError('unexpected synthetic source request')
    return {'jobs': [{'id': 1, 'title': 'Synthetic scheduling fixture',
                      'location': {'name': 'Synthetic location'}}]}


def _forbidden(*args, **kwargs):
    raise RuntimeError('network forbidden in source scheduler demo')


def _load(home):
    root = Path(os.path.abspath(home))
    if root.parent.resolve(strict=True) != root.parent:
        raise ValueError('demo parent symlink forbidden')
    root.mkdir(mode=0o700, exist_ok=False)
    cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'keel.py'), run_name='keel_source_demo_cli')
    from safe_io import atomic_json
    cli['initialize'](root)
    atomic_json(root / 'DEMO_ONLY.json', {'synthetic': True, 'external_actions_allowed': False})
    return root


def run_demo(home):
    """Use a NEW home. Return deterministic checks plus descriptive timings."""
    root = _load(home)
    import pipeline_service as pipeline
    import queue_io
    import source_scheduler as scheduler
    from safe_io import atomic_json, read_json
    started = time.perf_counter_ns()
    with ExitStack() as stack:
        stack.enter_context(patch.object(queue_io, '_LOCK_PATH', str(root / 'queue.lock')))
        stack.enter_context(patch.object(pipeline, 'urlopen', _forbidden))
        stack.enter_context(patch('socket.socket', _forbidden))
        for index in range(64):
            pipeline.add_source(root, 'greenhouse:fixture-' + str(index).zfill(3))
        baseline = pipeline.discover(root, max_new=64, reader=pipeline.PublicBoardReader(fetcher=_fixture))
        rounds = [scheduler.scheduled_discover(root, max_boards=16, max_new=16,
                    reader=pipeline.PublicBoardReader(fetcher=_fixture), clock=lambda: 1000) for _ in range(4)]
        replay = [scheduler.scheduled_discover(root, max_boards=16, max_new=16,
                    reader=pipeline.PublicBoardReader(fetcher=_fixture), clock=lambda: 1301) for _ in range(4)]
        queue = read_json(root / 'data/queues/standard-queue.json')
        checks = {'default_full_scan_hits_budget': baseline['status'] == 'HELD_DEADLINE' and baseline['added'] == 0,
                  'every_source_served_once': len(set(ref for row in rounds for ref in row['attempted'])) == 64,
                  'bounded_rounds': all(row['requests'] == 16 and row['added'] == 16 for row in rounds),
                  'all_64_postings_admitted': len(queue) == 64,
                  'replay_deduplicates_all_postings': sum(row['added'] for row in replay) == 0,
                  'no_authority_promotion': all(row['status'] == 'PARKED-PENDING-VERIFICATION'
                       and row['execution_authorized'] is False for row in queue)}
        crash_home = root / 'process-crash'
        child = subprocess.run([sys.executable, '-B', '-m', 'keel_next._source_crash_worker', str(crash_home)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        checks['actual_sigkill'] = child.returncode == -signal.SIGKILL
        crash = {'returncode': child.returncode, 'diagnostic_sha256': hashlib.sha256(child.stderr).hexdigest()}
        if checks['actual_sigkill']:
            with patch.object(queue_io, '_LOCK_PATH', str(crash_home / 'queue.lock')):
                before = (crash_home / 'data/queues/standard-queue.json').read_bytes()
                held = scheduler.scheduled_discover(crash_home, reader=pipeline.PublicBoardReader(fetcher=_fixture),
                                                   clock=lambda: 1000)
                recovered = scheduler.recover_interrupted(crash_home, clock=lambda: 1000)
                stable = (crash_home / 'data/queues/standard-queue.json').read_bytes() == before
                resumed = scheduler.scheduled_discover(crash_home, reader=pipeline.PublicBoardReader(fetcher=_fixture),
                                                       clock=lambda: 1301)
                checks.update(interrupted_commit_held=held['status'] == 'HELD_RECOVERY',
                    recovery_keeps_outcome_unknown=recovered['previous_outcome'] == 'UNKNOWN' and stable,
                    crash_replay_no_duplicate=resumed['added'] == 0 and len(read_json(crash_home / 'data/queues/standard-queue.json')) == 1)
                crash.update(hold=held['status'], recovery=recovered['status'], replay_added=resumed['added'])
    report = {'schema': 'keel.source-scheduler.demo.v1', 'synthetic': True,
              'status': 'SOURCE_SCHEDULER_PASSED' if all(checks.values()) else 'SOURCE_SCHEDULER_FAILED',
              'checks': checks, 'baseline': {'requests': baseline['requests'], 'added': baseline['added']},
              'scheduled': {'rounds': 4, 'requests': sum(row['requests'] for row in rounds),
                            'added': sum(row['added'] for row in rounds), 'replay_added': sum(row['added'] for row in replay)},
              'crash': crash, 'elapsed_ms': (time.perf_counter_ns() - started) / 1_000_000,
              'timing_is_descriptive': True, 'network_calls': 0, 'external_actions': 0,
              'scope': 'Public-source local intake; no form, eligibility, acceptance or application authorization.',
              **scheduler.FLAGS}
    atomic_json(root / 'source-scheduler-report.json', report)
    return report
