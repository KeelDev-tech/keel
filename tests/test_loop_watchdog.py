"""Tests for loop_watchdog.py parsing: run footers + untriaged backlog counting."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "worker-charter"))
import loop_watchdog as lw


def _parse(text):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        path = f.name
    old = lw.PROPOSALS
    lw.PROPOSALS = path
    try:
        return lw.parse_proposals()
    finally:
        lw.PROPOSALS = old
        os.unlink(path)


def test_footer_parsed_and_latest_wins():
    text = (
        "# Charter-evaluator run 2026-09-14 ~20:00 PDT -- 10 proposals triaged\n"
        "# Charter-evaluator run 2026-09-15 ~20:05 PDT -- 107 proposals triaged\n"
    )
    last_run, untriaged, total = _parse(text)
    assert last_run is not None
    assert untriaged == 0
    # 2026-09-15 20:05 PDT = 2026-09-16 03:05 UTC
    from datetime import datetime, timezone
    got = datetime.fromtimestamp(last_run, timezone.utc).strftime("%Y-%m-%dT%H:%M")
    assert got == "2026-09-16T03:05", got


def test_untriaged_counts_only_drafts_without_verdict():
    text = (
        "## P-2026-09-15-x1 (DRAFT / PENDING TRIAGE): something\n"
        "- EVALUATOR 2026-09-15: PROMOTE -- text\n"
        "## P-2026-09-15-x2 (DRAFT / PENDING TRIAGE): other\n"
        "- some note, no verdict\n"
        "## P-2026-09-15-x3 (PROMOTED → IMPLEMENTED 2026-09-15): done\n"
    )
    last_run, untriaged, total = _parse(text)
    assert last_run is None
    assert untriaged == 1, (untriaged, total)
    assert total == 3


def test_missing_file_returns_sentinel():
    old = lw.PROPOSALS
    lw.PROPOSALS = "/nonexistent/learning-proposals.md"
    try:
        assert lw.parse_proposals() == (None, -1, 0)
    finally:
        lw.PROPOSALS = old
