"""Bounded synthetic benchmark of Keel's actual discovery/verification pipeline.

Standard library only. No sockets, provider accounts, model calls or submissions.
Timing is descriptive on this machine, never a statistical speedup claim.
"""
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import runpy
import sys
import time
import tracemalloc
from urllib.error import HTTPError
from unittest.mock import patch


def _integer(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(label + ' outside benchmark bounds')
    return value


def run_benchmark(home, *, boards=3, jobs_per_board=100, max_new=150):
    """Run fixed local fixtures in a NEW home; return a JSON-safe result.

    Runs in a dedicated process: test transport and legacy telemetry paths are
    temporarily scoped to this synthetic home. Existing homes are refused.
    """
    _integer(boards, 'boards', 10)
    _integer(jobs_per_board, 'jobs_per_board', 200)
    _integer(max_new, 'max_new', 2000)
    total = boards * jobs_per_board
    if total > 1000:
        raise ValueError('benchmark workload exceeds 1000 postings')
    root = Path(os.path.abspath(home))
    if root.parent.resolve(strict=True) != root.parent:
        raise ValueError('benchmark parent must not be a symlink')
    if tracemalloc.is_tracing():
        raise ValueError('benchmark needs a dedicated process without active tracing')
    root.mkdir(mode=0o700, exist_ok=False)
    # Load the shipped CLI explicitly: legacy plugins/tests may provide a
    # namespace-only module named "keel" without the operator interface.
    # Do not overwrite that namespace or select code from input data.
    cli = runpy.run_path(str(Path(__file__).resolve().parents[1]/'keel.py'),
                        run_name='keel_benchmark_cli')
    import pipeline_service as service
    import queue_io
    import log_event
    from safe_io import atomic_json, read_json, rows, digest

    workload = {'schema': 'keel.pipeline.workload.v1', 'synthetic': True,
                'boards': boards, 'jobs_per_board': jobs_per_board, 'max_new': max_new,
                'provider': 'greenhouse', 'ordering': 'registered-board-then-numeric-id'}
    endpoints = {f'https://boards-api.greenhouse.io/v1/boards/benchmark-{b}/jobs?content=true': b
                 for b in range(boards)}
    calls = []
    def fetch(url, timeout):
        if url not in endpoints or not 0 < timeout <= 20:
            raise ValueError('unexpected benchmark transport request')
        calls.append(url)
        board = endpoints[url]
        return {'jobs': [{'id': i + 1, 'title': f'Synthetic role {board}-{i}',
                          'location': {'name': 'Synthetic location'},
                          'content': 'Artificial benchmark fixture; not a real vacancy.'}
                         for i in range(jobs_per_board)]}
    def rate_limited(url, timeout):
        if endpoints[url] == boards - 1:
            raise HTTPError(url, 429, 'synthetic rate-limit fixture', {}, None)
        return fetch(url, timeout)
    def forbidden_network(*args, **kwargs):
        raise RuntimeError('network prohibited in benchmark')
    timings = {}
    def measured(name, callback):
        started = time.perf_counter_ns()
        result = callback()
        timings[name] = (time.perf_counter_ns() - started) / 1_000_000
        return result
    readers = []
    def reader(fetcher=fetch):
        value = service.PublicBoardReader(timeout=120, fetcher=fetcher)
        readers.append(value)
        return value
    expected_ids = ['SRC-' + hashlib.sha256(f'greenhouse:benchmark-{b}:{i+1}'.encode()).hexdigest()[:32]
                    for b in range(boards) for i in range(jobs_per_board)]
    started = time.perf_counter_ns()
    tracemalloc.start()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'KEEL_HOME': str(root)}))
            stack.enter_context(patch.object(queue_io, '_LOCK_PATH', str(root/'queue.lock')))
            stack.enter_context(patch.object(log_event, 'EVENTS', str(root/'data/telemetry/events.jsonl')))
            stack.enter_context(patch.object(service, 'urlopen', forbidden_network))
            # Deny accidental socket construction by any code on the fixture path.
            stack.enter_context(patch('socket.socket', forbidden_network))
            atomic_json(root/'DEMO_ONLY.json', {'synthetic': True, 'external_actions_allowed': False})
            measured('initialize_ms', lambda: cli['initialize'](root))
            for b in range(boards):
                service.add_source(root, f'greenhouse:benchmark-{b}', 'Synthetic employer')
            first = measured('bounded_discovery_ms', lambda: service.discover(root, max_new=max_new, reader=reader()))
            remaining = measured('remaining_discovery_ms', lambda: service.discover(root, max_new=2000, reader=reader()))
            replay = measured('duplicate_replay_ms', lambda: service.discover(root, max_new=2000, reader=reader()))
            before_hold = digest(read_json(root/'data/queues/standard-queue.json'))
            held = measured('rate_limit_hold_ms', lambda: service.discover(root, reader=reader(rate_limited)))
            after_hold = digest(read_json(root/'data/queues/standard-queue.json'))
            verification = measured('verification_ms', lambda: service.verify(root, limit=1000, live=True, reader=reader()))
            cooldown = measured('cooldown_replay_ms', lambda: service.verify(root, limit=1000, live=True, reader=reader()))
            flushed = measured('outbox_replay_ms', lambda: service.flush_outbox(root))
            supply = measured('supply_snapshot_ms', lambda: service.supply_report(root))
            queue = rows(read_json(root/'data/queues/standard-queue.json'))
            events_path = root/'data/telemetry/events.jsonl'
            events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
            checks = {
                'first_batch_exact_source_order': first['role_ids'] == expected_ids[:max_new],
                'remaining_batch_exact_source_order': remaining['role_ids'] == expected_ids[min(max_new,total):],
                'replay_adds_zero': replay['added'] == 0 and replay['duplicate_observations'] == total,
                'unique_queue_identities': [row['role_id'] for row in queue] == expected_ids,
                'rate_limit_holds_entire_batch': held['status'] == 'HELD_HTTP_429' and before_hold == after_hold,
                'one_read_per_verification_board': verification['requests'] == boards,
                'all_postings_verified': verification['committed'] == total and verification['verdicts'] == {'live': total},
                'cooldown_replay_reads_zero': cooldown['selected'] == 0 and cooldown['requests'] == 0,
                'outbox_exactly_once_in_fixture': len(events) == total and len({event['event_id'] for event in events}) == total
                    and flushed['emitted'] == 0 and flushed['pending'] == 0,
                'supply_conservation': supply['queue_rows'] == total and supply['conservation_ok'],
                'no_ready_or_execution_promotion': all(row['status'] == 'PARKED-PENDING-VERIFICATION'
                    and row['execution_authorized'] is False for row in queue),
                'retained_cache_within_bounds': all(r.peak_cache_bytes <= service.MAX_CACHE_BYTES
                    and r.peak_cache_records <= service.MAX_CACHE_RECORDS for r in readers),
                'spool_within_bounds': all(r['candidate_spool_bytes'] <= service.MAX_CANDIDATE_BYTES
                    and r['candidate_records'] <= service.MAX_CANDIDATE_RECORDS for r in (first,remaining,replay)),
            }
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    source_root = Path(__file__).resolve().parents[1]
    source_files = ('keel_next/benchmark.py','keel.py','engines/pipeline_service.py',
                    'engines/safe_io.py','engines/queue_io.py','engines/log_event.py','engines/posting_identity.py')
    report = {'schema': 'keel.pipeline.benchmark.v1',
              'status': 'BENCHMARK_PASSED' if all(checks.values()) else 'BENCHMARK_FAILED',
              'synthetic': True, 'workload': workload, 'workload_sha256': digest(workload),
              'runtime': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                          'platform': platform.platform()},
              'source_sha256': {name: hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in source_files},
              'checks': checks, 'timings_ms': timings,
              'elapsed_ms': (time.perf_counter_ns()-started)/1_000_000,
              'memory': {'measurement': 'tracemalloc_python_allocations_after_imports',
                         'current_bytes': current_bytes, 'peak_bytes': peak_bytes,
                         'process_rss_measured': False},
              'counts': {'workload_postings': total, 'first_added': first['added'], 'remaining_added': remaining['added'],
                         'replay_added': replay['added'], 'verified': verification['committed'],
                         'fixture_fetches': len(calls), 'source_requests_including_injected_429': sum(r.requests for r in readers),
                         'events': len(events), 'queue_rows': len(queue),
                         'peak_cache_records': max(r.peak_cache_records for r in readers),
                         'peak_cache_bytes': max(r.peak_cache_bytes for r in readers),
                         'peak_candidate_spool_bytes': max(r['candidate_spool_bytes'] for r in (first,remaining,replay))},
              'timing_is_descriptive': True, 'production_speedup_proven': False,
              'network_calls': 0, 'model_calls': 0, 'external_actions': 0, 'execution_authorized': False,
              'paid_services_required': False}
    atomic_json(root/'benchmark-report.json', report)
    return report
