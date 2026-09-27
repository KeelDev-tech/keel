"""Offline integration: shared budgets, shipped CLI and component composition."""
import json

import pytest

from keel_efficiency.__main__ import main
from keel_efficiency.defaults import configured_budget, open_default_budget, budget_environment
from keel_efficiency.demo import run_demo
from keel_efficiency.ledger import ResourceLedger, BudgetExceeded


def test_component_demo_exercises_accounting_reuse_routing_and_qualification():
    result = run_demo()
    assert result["status"] == "PASS"
    assert all(result["checks"].values())
    assert result["provider_calls"] == result["paid_services"] == 0
    assert result["execution_authorized"] is False


def test_budget_cli_parent_is_shared_and_paid_execution_refused(tmp_path, capsys):
    path = str(tmp_path / "budget.sqlite3")
    config = tmp_path / "limits.json"
    config.write_text(json.dumps({"limits": {"calls": 1}}))
    for scope, parent in (("day", []), ("worker-a", ["--parent", "day"]), ("worker-b", ["--parent", "day"])):
        assert main(["budget-create", "--ledger", path, "--scope", scope, "--input", str(config), *parent]) == 0
    ledger = ResourceLedger(path)
    ledger.reserve("first", "worker-a", {"calls": 1})
    with pytest.raises(BudgetExceeded):
        ledger.reserve("second", "worker-b", {"calls": 1})
    config.write_text(json.dumps({"limits": {"external_credit_micros": 1}}))
    assert main(["budget-create", "--ledger", path, "--scope", "paid", "--input", str(config)]) == 2


@pytest.mark.parametrize("path,scope", [("", ""), (None, "scope"), ("path", None), ("", "scope")])
def test_explicit_invalid_budget_never_falls_back(path, scope):
    with pytest.raises(ValueError):
        configured_budget(path, scope)


def test_configured_environment_budget_dominates_default_and_restores(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEL_BUDGET_LEDGER", raising=False)
    monkeypatch.delenv("KEEL_BUDGET_SCOPE", raising=False)
    path = tmp_path / "shared.sqlite3"
    ResourceLedger(path).create_scope("shared", {"calls": 1})
    home = tmp_path / "unused"
    with budget_environment(path, "shared"):
        ledger, scope = open_default_budget(home)
        assert scope == "shared"
        assert ledger.snapshot(scope)["limits"]["calls"] == 1
        assert not home.exists()
    import os
    assert "KEEL_BUDGET_LEDGER" not in os.environ


def test_context_cli_holds_required_material_instead_of_truncating(tmp_path, capsys):
    request = tmp_path / "context.json"
    request.write_text(json.dumps({"task": "extract", "sources": [],
        "required_constraints": ["Preserve required approvals"], "max_bytes": 1, "current_revisions": {}}))
    assert main(["context", "--input", str(request)]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "REQUIRED_CONTEXT_EXCEEDS_BUDGET"


def test_outputs_are_exclusive_and_demo_is_runnable(tmp_path):
    output = tmp_path / "demo.json"
    assert main(["demo", "--out", str(output)]) == 0
    original = output.read_bytes()
    assert main(["demo", "--out", str(output)]) == 2
    assert output.read_bytes() == original
