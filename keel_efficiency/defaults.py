"""Conservative local allowance; operator-created scopes provide other limits.

This allowance is cumulative, never silently reset by process restart or date.
Use one explicit ledger and parent scope across hosts/workers on the same host.
SQLite on a network filesystem is not a distributed budget service.
"""
from pathlib import Path
from contextlib import contextmanager
import os

from .ledger import ResourceLedger

DEFAULT_LIMITS = {
    "calls": 64,
    "input_tokens": 2_000_000,
    "output_tokens": 524_288,
    "compute_ms": 3_600_000,
    "external_credit_micros": 0,
}
DEFAULT_SCOPE = "local-default-v1"


def open_default_budget(home):
    configured, scope = configured_budget(os.environ.get("KEEL_BUDGET_LEDGER"),
                                          os.environ.get("KEEL_BUDGET_SCOPE"))
    if configured is not None:
        return configured, scope
    ledger = ResourceLedger(Path(home) / "resource-budget.sqlite3")
    ledger.create_scope(DEFAULT_SCOPE, DEFAULT_LIMITS)
    return ledger, DEFAULT_SCOPE


def configured_budget(path, scope_id, *, readonly=False):
    """Open an explicit existing scope; inspection can refuse all state writes."""
    if (path is None) != (scope_id is None):
        raise ValueError("budget ledger and scope must be supplied together")
    if path is None:
        return None, None
    if not path or not scope_id:
        raise ValueError("budget ledger and scope cannot be empty")
    ledger = ResourceLedger.open_readonly(path) if readonly else ResourceLedger(path)
    ledger.snapshot(scope_id)  # Existing operator-created scope required.
    return ledger, scope_id


@contextmanager
def budget_environment(path, scope_id):
    """CLI-only scoped configuration; concurrent hosts pass objects explicitly."""
    configured_budget(path, scope_id)
    if not path:
        raise ValueError("explicit resource ledger and scope required for local evaluation")
    names = ("KEEL_BUDGET_LEDGER", "KEEL_BUDGET_SCOPE")
    prior = {key: os.environ.get(key) for key in names}
    os.environ[names[0]], os.environ[names[1]] = str(Path(path).absolute()), scope_id
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
