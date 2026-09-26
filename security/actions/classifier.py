"""Action classification: every consequential call gets a risk class.

Deterministic keyword mapping from action name -> ActionClass. Unknown
action names fail closed to EXTERNAL_COMMUNICATION (approval-gated), never
to an auto-permitted class.
"""

from __future__ import annotations

from enum import Enum


class ActionClass(Enum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"
    CREDENTIAL = "CREDENTIAL"
    FINANCIAL = "FINANCIAL"
    SUBMISSION = "SUBMISSION"
    SYSTEM_CHANGE = "SYSTEM_CHANGE"
    DESTRUCTIVE = "DESTRUCTIVE"


# Ordered: first match wins. Keep specific actions before generic ones.
_CLASS_TABLE: list[tuple[ActionClass, tuple[str, ...]]] = [
    (ActionClass.SUBMISSION, ("record_submission", "submit_application",
                              "mark_submitted", "confirm_submission",
                              "browser_submit", "api_direct_write")),
    (ActionClass.DESTRUCTIVE, ("delete_", "drop_", "wipe", "destroy",
                                "purge", "truncate")),
    (ActionClass.CREDENTIAL, ("read_secret", "use_credential", "issue_token",
                              "rotate_secret", "decrypt")),
    (ActionClass.FINANCIAL, ("charge", "pay", "purchase", "spend", "refund",
                             "create_payment", "process_payment")),
    (ActionClass.SYSTEM_CHANGE, ("modify_policy", "deploy", "restart_service",
                                 "change_schedule", "update_cron",
                                 "rebaseline", "migrate",
                                 "keel_0_8_promotion")),
    (ActionClass.EXTERNAL_COMMUNICATION, ("send_email", "send_message",
                                          "http_post", "browser_navigate",
                                          "webhook", "publish", "post_")),
    (ActionClass.REVERSIBLE_WRITE, ("write_packet", "update_queue",
                                    "park_lead", "write_telemetry",
                                    "append_", "log_event", "stage_")),
    (ActionClass.READ_ONLY, ("read_", "search", "list_", "get_", "fetch_",
                             "classify", "scan", "check", "verify_")),
]


def classify(action_name: str) -> tuple[ActionClass, str]:
    """Classify an action name -> (ActionClass, reason).

    Unknown names fail closed to EXTERNAL_COMMUNICATION so they land in
    the approval-gated bucket instead of an auto-permitted one.
    """
    name = (action_name or "").strip().lower()
    if not name:
        return ActionClass.EXTERNAL_COMMUNICATION, \
            "empty action name — fail-closed to approval-gated class"
    for cls, prefixes in _CLASS_TABLE:
        for p in prefixes:
            if name == p or name.startswith(p):
                return cls, f"matched action-class table entry {p!r}"
    return ActionClass.EXTERNAL_COMMUNICATION, \
        f"unknown action {action_name!r} — fail-closed to approval-gated class"


def is_high_impact(cls: ActionClass) -> bool:
    return cls in (ActionClass.EXTERNAL_COMMUNICATION, ActionClass.CREDENTIAL,
                   ActionClass.FINANCIAL, ActionClass.SUBMISSION,
                   ActionClass.SYSTEM_CHANGE, ActionClass.DESTRUCTIVE)
