"""Pure browser-task liveness checks; no runtime or browser operations.

A caller supplies an authoritative row for the exact requested task.
Missing, malformed, or unrecognized rows are UNKNOWN, never proof that a
task ended. A terminal task is not evidence of a submitted application.
"""
from collections.abc import Mapping

LIVE = "LIVE"
TERMINAL = "TERMINAL"
UNKNOWN = "UNKNOWN"

TERMINAL_STATUSES = frozenset({"completed", "failed", "superseded"})
TERMINAL_OUTCOMES = frozenset({"completed", "failed"})
LIVE_STATUSES = frozenset({
    "pending", "queued", "running", "in_progress", "started", "ready",
    "needs_user", "parked_outcome",
})


def _label(value):
    return value.strip().lower() if isinstance(value, str) else ""


def classify_task(task, task_id=None):
    """Return LIVE, TERMINAL, or UNKNOWN for a read-only runtime row.

    When the row provides an identity it must match ``task_id``. Providers
    may return an identity-free row because they were queried by exact ID;
    they must never use title/company matching to supply that row.
    """
    if not isinstance(task, Mapping):
        return UNKNOWN
    if task_id is not None:
        for key in ("task_id", "browser_task_id", "id"):
            if key in task and task[key] != task_id:
                return UNKNOWN
    status = _label(task.get("status"))
    outcome = _label(task.get("outcome_status"))
    reason = task.get("terminal_reason")
    # A blank or malformed reason is not positive terminal evidence.
    if ((isinstance(reason, str) and bool(reason.strip()))
            or status in TERMINAL_STATUSES
            or outcome in TERMINAL_OUTCOMES):
        return TERMINAL
    if reason is not None or (outcome and outcome not in {
            "running", "pending", "needs_user", "parked_outcome"}):
        return UNKNOWN
    if status in LIVE_STATUSES:
        return LIVE
    return UNKNOWN


def is_live(task):
    return classify_task(task) == LIVE


def is_terminal(task):
    return classify_task(task) == TERMINAL
