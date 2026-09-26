"""Reproducible synthetic scale exercise; not a live throughput guarantee.

Run: PYTHONPATH=engines python3 -B tests/benchmark_identity_index.py --output /tmp/identity-benchmark.json
"""
import argparse
import json
from pathlib import Path
import platform
import tempfile
import time

from dedupe_index import DedupeIndex, Source


def run(sizes=(10_000, 100_000), batch_size=1000):
    results = []
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix="keel-identity-bench-") as temp:
            root = Path(temp)
            rows = [{"role_id": f"role-{n}", "company": f"Company {n % 100}",
                     "title": "Operations Manager", "status": "READY",
                     "posting_url": f"https://jobs.lever.co/company{n % 100}/posting-{n}"}
                    for n in range(size)]
            source = root / "queue.json"
            source.write_text(json.dumps(rows, separators=(",", ":")))
            index = DedupeIndex(root / "index.sqlite3", [Source("synthetic", str(source))])
            started = time.perf_counter()
            status = index.refresh()
            build_seconds = time.perf_counter() - started
            if not status["complete"] or status["rows"] != size:
                raise AssertionError(status)
            candidates = rows[-min(batch_size, size):]
            started = time.perf_counter()
            checked = index.check_batch(candidates)
            batch_seconds = time.perf_counter() - started
            if not all(verdict == "duplicate" for verdict, _ in checked):
                raise AssertionError("synthetic exact identity recall failed")
            other = {"company": "new", "title": "new", "posting_url": "https://jobs.lever.co/never/posting-new"}
            if index.check_batch([other])[0][0] != "fresh":
                raise AssertionError("synthetic distinct identity suppressed")
            results.append({"rows": size, "source_bytes": source.stat().st_size,
                            "database_bytes": index.path.stat().st_size, "build_seconds": round(build_seconds, 6),
                            "batch_candidates": len(checked), "batch_seconds": round(batch_seconds, 6),
                            "exact_duplicates": len(checked), "distinct_candidate_preserved": True})
    return {"synthetic": True, "python": platform.python_version(), "platform": platform.system(),
            "freshness": "full source hashes before and after each batch", "results": results,
            "execution_authorized": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = run()
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
