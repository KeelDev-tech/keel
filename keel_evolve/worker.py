"""Fixed evaluation worker with OS limits. This is not an untrusted-code sandbox."""
import json
import resource
import sys
from keel_eval.reliability import Runner, run_paired
from .interpreter import artifact_runner, baseline_runner

LIMIT = 2 * 1024 * 1024


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raw = sys.stdin.buffer.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('worker_input_limit')
    job = json.loads(raw)
    report = run_paired(job['plan'], job['dataset'],
        baseline=Runner(baseline_runner, job['baseline']),
        candidate=Runner(artifact_runner, job['candidate']),
        adjudication_validator=lambda plan, dataset: job['attested'] is True)
    report['worker_limits'] = {name:list(resource.getrlimit(getattr(resource, name)))
        for name in ('RLIMIT_CPU','RLIMIT_AS','RLIMIT_FSIZE','RLIMIT_NOFILE','RLIMIT_CORE')}
    encoded = json.dumps(report, allow_nan=False).encode()
    if len(encoded) > LIMIT:
        raise ValueError('worker_output_limit')
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.stderr.write('evolution_worker_failed\n')
        raise SystemExit(2)
