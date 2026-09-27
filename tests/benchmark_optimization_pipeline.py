"""Matched-input, offline operation-count comparison against a supplied baseline.

Run from the repository root:
    python tests/benchmark_optimization_pipeline.py --baseline /path/to/pipeline_service.py

The cache is intentionally reduced to eight records in BOTH implementations to
exercise LRU pressure without downloading large boards. Counts describe this
synthetic workload, not real-world latency, credit spend, or application yield.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engines'))
import pipeline_service as candidate
import queue_io
from safe_io import atomic_json


def load_baseline(path):
    path = Path(path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('keel_pipeline_benchmark_baseline', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(module, request_cap):
    boards, per_board = 12, 8
    calls = []
    rows = [{'role_id': f'{job:03d}-board-{board:03d}',
             'application_url': f'https://job-boards.greenhouse.io/board-{board:03d}/jobs/{job}',
             'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': None, 'holds': []}
            for job in range(per_board) for board in range(boards)]
    def fetch(url, timeout):
        calls.append(url)
        return {'jobs': [{'id': job, 'title': f'Synthetic role {job}'} for job in range(per_board)]}
    with tempfile.TemporaryDirectory(prefix='keel-benchmark-') as folder:
        home = Path(folder)
        for queue in module.QUEUES:
            atomic_json(home / f'data/queues/{queue}-queue.json', rows if queue == 'standard' else [])
        atomic_json(home / 'data/application-ledger.json', [])
        atomic_json(home / 'data/sources.json', {'sources': []})
        with patch.object(queue_io, '_LOCK_PATH', str(home / 'queue.lock')):
            with patch.object(module, 'MAX_CACHE_RECORDS', per_board):
                reader = module.PublicBoardReader(fetcher=fetch, max_requests=request_cap)
                with patch.object(module, '_key', wraps=module._key) as parsed:
                    report = module.verify(home, limit=len(rows), reader=reader)
                verification_identity_parses = parsed.call_count
            with patch.object(module, '_key', wraps=module._key) as parsed:
                supply = module.supply_report(home)
                supply_identity_parses = parsed.call_count
    return {'selected': report['selected'], 'requests': len(calls),
            'verdicts': report['verdicts'], 'observations': sum(report['verdicts'].values()),
            'deferred_without_attempt': report.get('deferred_without_attempt', {}),
            'verification_identity_parses': verification_identity_parses,
            'supply_identity_parses': supply_identity_parses,
            'supply_states': supply['mutually_exclusive_supply_states'],
            'peak_cache_records': reader.peak_cache_records,
            'promoted_to_ready': report['promoted_to_ready'],
            'submission_authorized': report['submission_authorized']}


def compare(path):
    baseline = load_baseline(path)
    comparisons = {}
    for cap in (1000, 12):
        before, after = measure(baseline, cap), measure(candidate, cap)
        assert before['selected'] == after['selected'] == 96
        assert before['supply_states'] == after['supply_states']
        assert after['peak_cache_records'] <= 8
        assert not after['submission_authorized'] and after['promoted_to_ready'] == 0
        assert after['requests'] <= cap
        assert after['verdicts'] == {'live': 96}
        if cap == 1000:
            assert before['verdicts'] == after['verdicts']
            assert before['requests'] == 96 and after['requests'] == 12
        comparisons[str(cap)] = {'baseline': before, 'candidate': after}
    return {'schema': 'keel.pipeline-optimization-benchmark.v1',
            'baseline_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'candidate_sha256': hashlib.sha256((ROOT / 'engines/pipeline_service.py').read_bytes()).hexdigest(),
            'workload': {'boards': 12, 'postings_per_board': 8, 'selected_rows': 96,
                         'cache_record_cap_both_versions': 8, 'network_calls': 0,
                         'selection_order': 'interleaved board identities, same queue and limit'},
            'comparisons_by_request_cap': comparisons,
            'scope': 'offline operation counts; public posting presence only; not production performance or credit savings'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline), indent=2, sort_keys=True))
