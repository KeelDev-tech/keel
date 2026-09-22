"""Bridge between the MCP server and Keel's engine modules.

Imports are read-only and side-effect free: every engine module used here is
stdlib-only and does no I/O at import time. KEEL_HOME is pointed at this
package's work tree so any default path resolution can never land on
Trent's live pipeline.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_WORKTREE = _HERE.parent

# Harmless default root: nothing in the MCP server relies on default paths,
# this just guarantees an accidental default can never resolve to ~/keel
# or ~/workspace/job-pipeline.
os.environ.setdefault("KEEL_HOME", str(_WORKTREE / "keel_home_stub"))

# Engine modules live in the repo's engines/ directory (repo root inferred
# from this file's location: integrations/mcp-server/mcp-server → repo root).
# Fall back to the conventional ~/workspace/keel checkout when this copy is
# run outside the repo tree (e.g. a hackathon work tree).
_REPO_ROOT = _HERE.parent.parent.parent
_ENGINES = _REPO_ROOT / "engines"
if not _ENGINES.is_dir():
    _ENGINES = Path.home() / "workspace" / "keel" / "engines"
if str(_ENGINES) not in sys.path:
    sys.path.insert(0, str(_ENGINES))

# Engine modules (all stdlib-only, import-safe — verified 2026-09-16).
# resume_tailor needs reportlab (Keel's one documented third-party dep);
# only its pure truthfulness_check is wrapped — build() is never called.
import ats as ats_mod  # noqa: E402
import form_intel as form_intel_mod  # noqa: E402
import score_roles as score_mod  # noqa: E402
import verify_retry as verify_mod  # noqa: E402
import prescreen as prescreen_mod  # noqa: E402
import resume_tailor as tailor_mod  # noqa: E402

SAMPLE_DATA = _HERE / "sample_data"

# Optional operator override (set by server.py from --data-dir after safety
# validation). When set, fixtures load from the override directory instead
# of the bundled samples. Both paths go through the basename + containment
# checks in load_fixture.
_DATA_OVERRIDE: Path | None = None


def set_data_dir(path: Path) -> None:
    global _DATA_OVERRIDE
    _DATA_OVERRIDE = path


def _fixture_root() -> Path:
    return _DATA_OVERRIDE if _DATA_OVERRIDE is not None else SAMPLE_DATA


def load_fixture(name: str) -> object:
    """Load a sample fixture by filename (bundled samples or validated override)."""
    from safety import assert_safe_basename

    assert_safe_basename(name)
    root = _fixture_root().resolve()
    path = (root / name).resolve()
    if root not in path.parents:
        raise ValueError("fixture path escapes data directory")
    if not path.exists():
        # Fall back to the bundled samples when the override dir lacks a fixture.
        path = (SAMPLE_DATA.resolve() / name).resolve()
    text = path.read_text(encoding="utf-8")
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)
