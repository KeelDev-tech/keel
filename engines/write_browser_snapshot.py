#!/usr/bin/env python3
"""Atomic writer for hidden_files/browser-task-snapshot.json (2026-09-17).

Root cause it fixes: the snapshot was written with a non-atomic file write
(truncate-then-fill). Any reader (lane_watchdog, parked_task_sweep,
octopus pulse arms) that opened the file mid-write saw truncated JSON --
pulse 244 caught it at char 9236 of ~27KB, which blocked
parked_task_sweep.py --repair-inflight (exit 2, fail loud).

Sanctioned write path (see hidden_files/parked-task-sweep-policy.md):
the lane owner dumps runtime.browser_tasks rows (via muse.db) to JSON, then
pipes them through this script instead of writing the snapshot file directly.
os.replace() is atomic on POSIX: readers either see the complete old file
or the complete new file, never a partial one.

Usage:
    <browser_tasks.json python3 write_browser_snapshot.py [--out PATH]
                                                         [--source LABEL]
                                                         [--in FILE]
    - input: JSON list of task dicts, or {"tasks": [...]} envelope
    - each task must carry task_id and status (fields the sweeper reads);
      anything else is warned about, not rejected
    - refuses to write unless the payload validates (never persists garbage)

Exit codes: 0 ok; 1 invalid input; 2 write failure.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from keel_paths import HOME  # noqa: E402 — repo path convention
DEFAULT_OUT = os.path.join(HOME, "hidden_files", "browser-task-snapshot.json")
DEFAULT_SOURCE = "muse.db runtime.browser_tasks"

REQUIRED_TASK_FIELDS = ("task_id", "status")


def _fail(msg):
    print(f"write_browser_snapshot: {msg}", file=sys.stderr)
    sys.exit(1)


def load_input(args):
    raw = None
    if args.get("in"):
        try:
            with open(args["in"]) as f:
                raw = f.read()
        except OSError as ex:
            _fail(f"cannot read input file {args['in']}: {ex}")
    else:
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as ex:
        _fail(f"input is not valid JSON: {ex}")
    if isinstance(payload, dict) and "tasks" in payload:
        tasks = payload["tasks"]
    elif isinstance(payload, list):
        tasks = payload
    else:
        _fail("input must be a JSON list of tasks or a {\"tasks\": [...]} envelope")
    if not isinstance(tasks, list):
        _fail("\"tasks\" must be a list")
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            _fail(f"task[{i}] is not an object")
        missing = [f for f in REQUIRED_TASK_FIELDS if f not in t]
        if missing:
            _fail(f"task[{i}] missing required fields: {missing}")
    return tasks


def atomic_write_json(path, obj):
    """Write obj as JSON to path atomically: temp file + fsync + rename."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv):
    args = {"in": None, "out": DEFAULT_OUT, "source": DEFAULT_SOURCE}
    i = 0
    while i < len(argv):
        if argv[i] == "--in" and i + 1 < len(argv):
            args["in"] = argv[i + 1]; i += 2
        elif argv[i] == "--out" and i + 1 < len(argv):
            args["out"] = argv[i + 1]; i += 2
        elif argv[i] == "--source" and i + 1 < len(argv):
            args["source"] = argv[i + 1]; i += 2
        else:
            _fail(f"unknown argument: {argv[i]}")
    tasks = load_input(args)
    envelope = {
        "dumped_at": datetime.now(timezone.utc).isoformat(),
        "source": args["source"],
        "tasks": tasks,
    }
    try:
        atomic_write_json(args["out"], envelope)
    except OSError as ex:
        print(f"write_browser_snapshot: write failed: {ex}", file=sys.stderr)
        sys.exit(2)
    # read-back validation: what is on disk must parse and match
    try:
        with open(args["out"]) as f:
            back = json.load(f)
        assert back["tasks"] == tasks, "read-back mismatch"
    except (OSError, json.JSONDecodeError, AssertionError, KeyError) as ex:
        print(f"write_browser_snapshot: read-back validation failed: {ex}",
              file=sys.stderr)
        sys.exit(2)
    print(f"write_browser_snapshot: wrote {len(tasks)} tasks -> {args['out']}")


if __name__ == "__main__":
    main(sys.argv[1:])
