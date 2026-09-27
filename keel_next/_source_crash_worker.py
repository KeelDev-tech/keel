"""Fixed offline crash fixture; never a user-configurable executor."""
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from .source_scheduler_demo import _fixture, _forbidden, _load


def main(home):
    root = _load(home)
    import pipeline_service as pipeline
    import queue_io
    import source_scheduler as scheduler
    pipeline.add_source(root, 'greenhouse:fixture-000')
    original, calls = scheduler._save, 0
    def crash_after_queue_commit(path, state, now):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.kill(os.getpid(), signal.SIGKILL)
            raise RuntimeError('kill did not terminate process')
        return original(path, state, now)
    with patch.object(queue_io, '_LOCK_PATH', str(root / 'queue.lock')), \
         patch.object(pipeline, 'urlopen', _forbidden), patch('socket.socket', _forbidden), \
         patch.object(scheduler, '_save', crash_after_queue_commit):
        scheduler.scheduled_discover(root, reader=pipeline.PublicBoardReader(fetcher=_fixture), clock=lambda: 1000)
    raise RuntimeError('crash fixture unexpectedly returned')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(2)
    main(Path(sys.argv[1]))
