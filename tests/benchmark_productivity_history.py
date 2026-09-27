"""10,000 real durable controller cycles on a bounded, empty offline workspace.

This measures retained-history scaling, not application throughput or model
quality. Use --cycles to shorten local development; release gate uses 10,000.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'engines'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles', type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 100000:
        parser.error('cycles must be 1..100000')
    with tempfile.TemporaryDirectory(prefix='keel-history-scale-') as temporary:
        home = Path(temporary)
        os.environ['KEEL_HOME'] = str(home)
        os.environ['JOB_PIPELINE_HTTP_COOLDOWN_DIR'] = str(home / 'hidden_files/http-cooldowns')
        from safe_io import atomic_json
        import pipeline_service as pipeline
        import productivity_history as history
        import productivity_service as service
        from keel_efficiency.ledger import ResourceLedger
        for name in pipeline.QUEUES:
            atomic_json(home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(home / 'data/application-ledger.json', [])
        atomic_json(home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        ledger = ResourceLedger(home / 'budget.sqlite3')
        ledger.create_scope('scale', {'calls': 1, 'compute_ms': 10**9})
        measured = {'peak_cached_run_rows': 0, 'fetch_calls': 0, 'request_lookups': 0}
        original_get, original_set = history.RunMap.get, history.RunMap.__setitem__
        def track_get(instance, *arguments, **keywords):
            result = original_get(instance, *arguments, **keywords)
            measured['peak_cached_run_rows'] = max(measured['peak_cached_run_rows'], len(instance.cache))
            return result
        def track_set(instance, *arguments, **keywords):
            result = original_set(instance, *arguments, **keywords)
            measured['peak_cached_run_rows'] = max(measured['peak_cached_run_rows'], len(instance.cache))
            return result
        history.RunMap.get, history.RunMap.__setitem__ = track_get, track_set
        original_request = ledger.request
        def request(identifier):
            measured['request_lookups'] += 1
            return original_request(identifier)
        ledger.request = request
        def fetcher(url, timeout):
            measured['fetch_calls'] += 1
            raise AssertionError('empty registry must never dispatch')
        options = dict(live=True, ledger=ledger, scope_id='scale', max_requests=1,
                       timeout=1, fetcher=fetcher, clock=lambda: 1800000000.0)
        start = time.monotonic()
        for index in range(args.cycles):
            result = service.run_once(home, run_id='scale-%05d' % index, **options)
            assert result['status'] == 'IDLE' and result['usage']['calls'] == 0
        elapsed = time.monotonic() - start
        lookup_count_before_replay = measured['request_lookups']
        prior_budget = ledger.snapshot('scale')['used']
        oldest = service.run_once(home, run_id='scale-00000', **options)
        assert oldest['replayed'] and ledger.snapshot('scale')['used'] == prior_budget
        report = service.status(home, ledger=ledger, scope_id='scale', clock=lambda: 1800000000.0)
        assert report['metrics']['retained_runs'] == args.cycles
        assert report['metrics']['stages']['idle']['runs'] == args.cycles
        cursor, count, page_peak = 0, 0, 0
        while True:
            page = service.receipts(home, after_sequence=cursor, limit=100, clock=lambda: 1800000000.0)
            count += len(page['receipts'])
            page_peak = max(page_peak, len(page['receipts']))
            cursor = page['next_sequence']
            if not page['has_more']:
                break
        assert count == args.cycles and measured['fetch_calls'] == 0
        assert measured['peak_cached_run_rows'] <= 1 and lookup_count_before_replay == 0
        print(json.dumps({'schema': 'keel.productivity.history-scale.v1', 'cycles': args.cycles,
            'real_controller_cycles': True, 'workload': 'empty_registry_idle_cycles',
            'retained_run_ids': count, 'oldest_run_replayed': oldest['replayed'],
            'replay_additional_dispatches': 0, 'peak_cached_run_rows': measured['peak_cached_run_rows'],
            'maximum_receipt_page_rows': page_peak, 'historical_request_lookups_during_new_cycles': lookup_count_before_replay,
            'oldest_replay_request_lookups': measured['request_lookups'] - lookup_count_before_replay,
            'elapsed_seconds_observed': round(elapsed, 3),
            'history_database_bytes': (home / 'data/productivity/history.sqlite3').stat().st_size,
            'network_calls': 0, 'model_calls': 0, 'actual_credit_savings': None,
            'production_performance_claim': False}, sort_keys=True))


if __name__ == '__main__':
    main()
