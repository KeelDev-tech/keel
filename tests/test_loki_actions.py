import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from keel_loki.actions import ActionError, ActionGateway
from keel_loki.forms import FormError, demo_fixture


def setup():
    f = demo_fixture()
    gateway = ActionGateway(f["contract"], f["host_snapshot"], f["approvals"])
    grant = gateway.issue(f["values"], now=f["now"])
    return f, gateway, grant


def test_complete_preparation_has_no_submission_action_or_authority():
    f, gateway, grant = setup()
    assert len(grant["actions"]) == 11
    assert not any("submit" in action["operation"] for action in grant["actions"])
    for action in grant["actions"]:
        assert gateway.consume(grant["capability"], action, now=f["now"]) == action
    status = gateway.status(grant["capability"]["capability_id"])
    assert status["consumed"] == status["total"] == 11
    assert status["submission_authorized"] is False
    with pytest.raises(ActionError):
        gateway.consume(grant["capability"], grant["actions"][-1], now=f["now"])


@pytest.mark.parametrize("flag,value", [("consent", False), ("approval_current", False), ("no_ai", True),
                                       ("holds", ["dedupe_hold"]), ("unknown_attempt", True), ("rate_limited", True)])
def test_host_gates_checked_at_issue_and_every_effect(flag, value):
    f, gateway, grant = setup()
    snapshot = copy.deepcopy(f["host_snapshot"])
    snapshot[flag] = value
    gateway.update_snapshot(snapshot)
    with pytest.raises(ActionError):
        gateway.consume(grant["capability"], grant["actions"][0], now=f["now"])
    with pytest.raises(ActionError):
        gateway.issue(f["values"], now=f["now"])
    assert gateway.status(grant["capability"]["capability_id"])["consumed"] == 0


@pytest.mark.parametrize("change", ["origin", "account_id", "contract_sha256", "revisions", "revoked_approval_ids"])
def test_scope_revision_and_approval_revocations_invalidate_capabilities(change):
    f, gateway, grant = setup()
    snapshot = copy.deepcopy(f["host_snapshot"])
    if change == "origin":
        snapshot[change] = "https://example.org"
    elif change == "account_id":
        snapshot[change] = "another_account"
    elif change == "contract_sha256":
        snapshot[change] = "a" * 64
    elif change == "revisions":
        snapshot[change]["approval"] = "a" * 64
    else:
        snapshot[change] = [grant["actions"][0]["approval_id"]]
    gateway.update_snapshot(snapshot)
    with pytest.raises(ActionError):
        gateway.consume(grant["capability"], grant["actions"][0], now=f["now"])


def test_model_proposal_has_no_authority_to_approve_changed_value():
    f, gateway, _ = setup()
    f["values"]["full_name"]["value"] = "Invented Qualification"
    with pytest.raises(FormError):
        gateway.issue(f["values"], now=f["now"])


@pytest.mark.parametrize("change", ["submit", "shell", "extra", "value", "field", "plan_pin", "out_of_order"])
def test_unknown_or_modified_actions_rejected_without_consuming(change):
    f, gateway, grant = setup()
    action = copy.deepcopy(grant["actions"][0])
    if change in {"submit", "shell"}:
        action["operation"] = change
    elif change == "extra":
        action["script"] = "alert(1)"
    elif change == "value":
        action["value"] = "Different value"
    elif change == "field":
        action["field_id"] = "other"
    elif change == "plan_pin":
        action["plan_sha256"] = "0" * 64
    else:
        action = grant["actions"][1]
    with pytest.raises(ActionError):
        gateway.consume(grant["capability"], action, now=f["now"])
    assert gateway.status(grant["capability"]["capability_id"])["consumed"] == 0


def test_replay_and_concurrent_replay_are_rejected():
    f, gateway, grant = setup()
    def consume():
        try:
            gateway.consume(grant["capability"], grant["actions"][0], now=f["now"])
            return "accepted"
        except ActionError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == ["accepted", "rejected"]
    assert gateway.status(grant["capability"]["capability_id"])["consumed"] == 1


@pytest.mark.parametrize("kind", ["expired", "future", "revoked", "signature", "scope", "restart"])
def test_capability_expiry_revocation_signature_and_restart(kind):
    f, gateway, grant = setup()
    now = f["now"]
    if kind == "expired":
        now = grant["capability"]["expires_at"]
    elif kind == "future":
        now -= 1
    elif kind == "revoked":
        gateway.revoke(grant["capability"]["capability_id"])
    elif kind == "signature":
        grant["capability"]["signature"] = "0" * 64
    elif kind == "scope":
        grant["capability"]["operation_scope"] = "submit"
    else:
        gateway = ActionGateway(f["contract"], f["host_snapshot"], f["approvals"])
    with pytest.raises(ActionError):
        gateway.consume(grant["capability"], grant["actions"][0], now=now)


def test_approval_expiration_bounds_capability_lifetime():
    f = demo_fixture()
    f["approvals"][0]["expires_at"] = f["now"] + 2
    gateway = ActionGateway(f["contract"], f["host_snapshot"], f["approvals"])
    grant = gateway.issue(f["values"], now=f["now"], ttl=120)
    assert grant["capability"]["expires_at"] == f["now"] + 2
    with pytest.raises(ActionError):
        gateway.consume(grant["capability"], grant["actions"][0], now=f["now"] + 2)


def test_nonboolean_policy_and_backdated_snapshot_are_rejected():
    f, gateway, _ = setup()
    f["host_snapshot"]["consent"] = 1
    with pytest.raises(FormError):
        gateway.update_snapshot(f["host_snapshot"])
    f["host_snapshot"]["consent"] = True
    f["host_snapshot"]["issued_at"] -= 1
    with pytest.raises(ActionError):
        gateway.update_snapshot(f["host_snapshot"])
