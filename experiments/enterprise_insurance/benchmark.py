"""Reproducible bounded synthetic benchmark. No real-market inference or network."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import platform
import tempfile
import time


def measure(count, batched, now):
    source = Path(__file__).with_name('pipeline.py')
    spec = importlib.util.spec_from_file_location('enterprise_benchmark_target', source)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    with tempfile.TemporaryDirectory(prefix='keel-enterprise-bench-') as tmp:
        lane = m.Lane(Path(tmp)/'lab', exclude_keel_home=Path(tmp)/'applicant', enabled=True, now=now)
        lane.init()
        packets = [m.fixture(f'account-{i}', now, missing='need' if i % 5 == 0 else None) for i in range(count)]
        start = time.perf_counter()
        if batched:
            for i in range(0, count, 100):
                lane.ingest_batch(packets[i:i+100])
        else:
            for packet in packets:
                lane.ingest(packet)
        intake = time.perf_counter() - start
        start = time.perf_counter()
        processed = calls = 0
        while True:
            result = lane.run(100)
            calls += 1
            processed += result['count']
            if not result['count']:
                break
        qualification = time.perf_counter() - start
        start = time.perf_counter()
        report = lane.report()
        inspection = time.perf_counter() - start
        expected_parked = (count + 4) // 5
        assert report['unique_accounts'] == count == processed
        assert report['state_counts'] == {'PARKED': expected_parked, 'QUALIFIED_FOR_DISCOVERY': count-expected_parked}
        assert report['audit_events'] == 1 + 3 * count
        assert report['outreach_authorized'] is False
        return {'intake_mode': 'batch_100' if batched else 'individual', 'fixture_accounts': count,
                'intake_seconds': round(intake, 4), 'qualification_seconds': round(qualification, 4),
                'readonly_report_seconds': round(inspection, 4), 'processed': processed,
                'batch_calls_including_empty': calls, 'state_counts': report['state_counts'],
                'audit_events': report['audit_events']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=200, choices=range(5, 1001))
    parser.add_argument('--compare-individual', action='store_true')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    results = [measure(args.count, True, now)]
    if args.compare_individual:
        results.append(measure(args.count, False, now))
    print(json.dumps({'as_of': now.isoformat(), 'python': platform.python_version(), 'platform': platform.system(),
                      'results': results, 'commercial_evidence': False,
                      'limitations': 'One local synthetic rehearsal, not a load test, held-out accuracy study or production scale claim. Full audit scan per transaction remains O(history).'}, indent=2))


if __name__ == '__main__':
    main()
