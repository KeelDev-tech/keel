#!/usr/bin/env python3
"""Silent-defect sweep (2026-09-19) — interlaunch_metrics skipped-line count.

FIX 6b (engines/interlaunch_metrics.py:88): `except json.JSONDecodeError:
continue` dropped corrupt JSONL lines silently. repaired_launches now
counts them (repaired_launches returns a _LaunchRows list carrying
skipped_lines; list behavior unchanged) and summarize() reports the count
as "skipped_lines" (0 when the input list is a plain list, e.g. from
other callers).

Fixtures synthetic; the events file lives in tmp.
"""

import json
import os
import sys

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import interlaunch_metrics as ilm  # noqa: E402


def _launch_event(ts, role_id="ROLE-1"):
    return {"ts": ts, "event_type": "browser_launched", "role_id": role_id,
            "source": "test", "details": {}}


def _write_events(path, lines):
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")


def test_corrupt_lines_counted_and_reported(tmp_path):
    events = str(tmp_path / "events.jsonl")
    _write_events(events, [
        json.dumps(_launch_event("2026-09-19T00:00:00+00:00")),
        # Corrupt line that reaches the JSON parser: it must contain the
        # "browser_launched" scan-filter token, or it is skipped before
        # parsing (pre-existing filter behavior, not the defect).
        '{"event_type": "browser_launched", "ts": "broken',
        json.dumps(_launch_event("2026-09-19T01:00:00+00:00", "ROLE-2")),
    ])
    launches = ilm.repaired_launches(events_path=events)
    assert launches.skipped_lines == 1
    summary = ilm.summarize(launches)
    assert summary["skipped_lines"] == 1
    assert summary["n_launches"] == 2


def test_clean_file_reports_zero_skipped(tmp_path):
    events = str(tmp_path / "events.jsonl")
    _write_events(events, [
        json.dumps(_launch_event("2026-09-19T00:00:00+00:00")),
    ])
    launches = ilm.repaired_launches(events_path=events)
    assert launches.skipped_lines == 0
    assert ilm.summarize(launches)["skipped_lines"] == 0


def test_summarize_tolerates_plain_list():
    """Back-compat: summarize() on a plain list (no skipped_lines attr)
    reports 0 instead of raising."""
    launches = [(ilm.parse_ts("2026-09-19T00:00:00+00:00"), "R", "s")]
    assert ilm.summarize(list(launches))["skipped_lines"] == 0


def test_launch_rows_is_still_a_list(tmp_path):
    events = str(tmp_path / "events.jsonl")
    _write_events(events, [json.dumps(_launch_event("2026-09-19T00:00:00+00:00"))])
    launches = ilm.repaired_launches(events_path=events)
    assert isinstance(launches, list)
    assert len(launches) == 1
