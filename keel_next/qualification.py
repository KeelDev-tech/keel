"""Offline process-crash qualification using fixed synthetic workloads."""
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

from ._crash_worker import event_requests


def qualify(home):
    home = Path(os.path.abspath(home))
    if home.parent.resolve(strict=True) != home.parent:
        raise ValueError('symlink parent')
    home.mkdir(mode=0o700, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    scenarios = {}
    for phase in ('before_dispatch', 'before_report', 'after_report', 'event_prefix'):
        target = home / phase
        child = subprocess.run([sys.executable, '-B', '-m', 'keel_next._crash_worker', phase, str(target)],
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        checks = {'actual_sigkill': child.returncode == -signal.SIGKILL}
        if checks['actual_sigkill']:
            if phase == 'event_prefix':
                import runpy
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'keel.py'), run_name='keel_qualification_loader')
                import log_event
                old = log_event.EVENTS
                try:
                    log_event.EVENTS = str(target / 'events.jsonl')
                    before = Path(log_event.EVENTS).read_text().splitlines()
                    receipts = log_event.log_batch(event_requests())
                    repeated = log_event.log_batch(event_requests())
                    records = [json.loads(line) for line in Path(log_event.EVENTS).read_text().splitlines()]
                    checks.update(prefix_survived=len(before) == 3,
                                  exactly_eight_records=len(records) == 8,
                                  unique_ids=len({row['event_id'] for row in records}) == 8,
                                  replay_receipts_stable=receipts == repeated)
                finally:
                    log_event.EVENTS = old
            else:
                from keel_machine.runtime import LocalGraphRuntime
                runtime = LocalGraphRuntime(target)
                runtime.work()
                result = runtime.result('demo')
                if phase == 'before_dispatch':
                    checks['queued_work_completes'] = result['status'] == 'COMPLETED'
                    checks['report_available'] = result['report'] is not None
                else:
                    checks['uncertain_work_held'] = result['status'] == 'UNKNOWN'
                    checks['report_withheld'] = result['report'] is None and result['receipt_sha256'] is None
                runtime.work()
                checks['repeated_restart_stable'] = runtime.result('demo') == result
        scenarios[phase] = {'returncode': child.returncode, 'checks': checks,
                            'diagnostic_sha256': hashlib.sha256(child.stderr).hexdigest()}
    passed = all(all(row['checks'].values()) for row in scenarios.values())
    return {'schema': 'keel.crash-qualification.v1',
            'status': 'QUALIFICATION_PASSED' if passed else 'QUALIFICATION_FAILED',
            'scenarios': scenarios, 'synthetic': True, 'platform': platform.platform(),
            'python': platform.python_version(), 'execution_authorized': False,
            'paid_services_required': False, 'production_deployed': False,
            'scope': 'Local process termination; does not establish power-loss or distributed-system safety.'}
