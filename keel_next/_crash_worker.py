"""Fixed synthetic crash scenarios. Never accepts executable input."""
import json
import os
from pathlib import Path
import signal
import sys


def terminate():
    os.kill(os.getpid(), signal.SIGKILL)


def event_requests():
    return [{'event_type': 'error', 'event_id': 'qualification-' + str(i),
             'details': {'synthetic': True, 'sequence': i}} for i in range(8)]


def main():
    phase, home = sys.argv[1:]
    if phase not in ('before_dispatch', 'before_report', 'after_report', 'event_prefix'):
        raise ValueError('unknown fixed scenario')
    home = Path(home)
    if home.exists() or home.is_symlink():
        raise ValueError('requires new home')
    if phase == 'event_prefix':
        import runpy
        runpy.run_path(str(Path(__file__).resolve().parents[1] / 'keel.py'), run_name='keel_qualification_loader')
        import log_event
        home.mkdir(mode=0o700)
        log_event.EVENTS = str(home / 'events.jsonl')
        def prefix(events):
            with Path(log_event.EVENTS).open('a', encoding='utf-8') as stream:
                for event in events[:3]:
                    stream.write(json.dumps(event) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            terminate()
        log_event._append_batch = prefix
        log_event.log_batch(event_requests())
    else:
        from keel_machine.runtime import LocalGraphRuntime, Coordinator
        from keel_machine.builtins import demo_plan
        runtime = LocalGraphRuntime.create(home, 'qualification', 'synthetic-account')
        runtime.submit(demo_plan())
        if phase == 'before_dispatch':
            Coordinator.worker_once = lambda *args, **kwargs: terminate()
        else:
            original = runtime._record_result
            def record(report):
                if phase == 'after_report':
                    original(report)
                terminate()
            runtime._record_result = record
        runtime.work()
    raise RuntimeError('crash point was not reached')


if __name__ == '__main__':
    main()
