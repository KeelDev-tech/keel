"""Safety guardrails for the Keel MCP server (hackathon build).

Non-negotiable invariants:
1. The server only reads data from an allowlisted directory (bundled samples by default).
   It will NEVER resolve paths under ~/workspace/job-pipeline, credentials/, or any
   real answer bank / queue.
2. No tool may perform an irreversible external action (no application submissions,
   no account creation, no outreach, no writes outside the data dir).
"""
from __future__ import annotations

import os
from pathlib import Path

# Directories that are permanently off-limits, matched by path prefix.
FORBIDDEN_PREFIXES = (
    "job-pipeline/credentials",
    "job-pipeline/queue",
    "credentials",
)

# File names that must never be opened, anywhere.
FORBIDDEN_BASENAMES = {
    "answer_bank.json",          # real bank (example copies are fine)
    "professional-references.md",
    ".env",
}


class SafetyError(RuntimeError):
    """Raised when a tool call violates a safety invariant."""


def resolve_data_dir(explicit: str | None = None) -> Path:
    """Resolve the server's data directory.

    Defaults to the bundled ``sample_data`` shipped with this package.
    Refuses any directory that looks like the operator's real pipeline workspace.
    """
    base = Path(explicit).expanduser().resolve() if explicit else (
        Path(__file__).resolve().parent / "sample_data"
    )
    home = Path.home().resolve()
    try:
        rel = base.relative_to(home)
    except ValueError:
        rel = None
    if rel is not None:
        rel_str = rel.as_posix()
        for prefix in FORBIDDEN_PREFIXES:
            if rel_str == prefix or rel_str.startswith(prefix + "/"):
                raise SafetyError(f"data dir '{base}' is in a forbidden area: {prefix}")
        # Belt-and-braces: never allow the live pipeline tree itself.
        if rel_str == "workspace/job-pipeline" or rel_str.startswith("workspace/job-pipeline/"):
            raise SafetyError(f"data dir '{base}' is the operator's live pipeline tree")
    return base


def assert_safe_basename(name: str) -> None:
    """Refuse to open files whose basename is on the forbidden list."""
    if os.path.basename(name) in FORBIDDEN_BASENAMES:
        raise SafetyError(f"refusing to open forbidden file: {name}")


def check_no_external_action(action: str) -> None:
    """Allowlist gate for external actions. Everything not listed is denied."""
    allowed = {"http-get", "read-file", "list-dir"}
    if action not in allowed:
        raise SafetyError(f"external action '{action}' is not permitted by this server")
