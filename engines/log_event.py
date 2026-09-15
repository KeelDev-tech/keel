"""Telemetry event logger — append-only JSONL event log for the pipeline.

Every engine and subagent logs with one call:

    python3 log_event.py gate_encountered --role-id X --company "Acme" \\
        --ats greenhouse --source discovery-sweep --details '{"gate": "captcha"}'

Event types (keep this list stable; the analyzer depends on it):
    lead_discovered   a new lead entered the system (discovery sweeps)
    lead_verified     posting confirmed live + accepting applications
    lead_dead         posting expired / employer dead / scam-flagged (no application)
    brief_built       a browser-task brief was generated
    browser_launched  a browser application task was spawned
    gate_encountered  a blocker appeared mid-flow (captcha, email code, account,
                      attestation, travel, wording, login...)
    gate_cleared      a gate was passed (gate name in details; never the secret value)
    gate_blocked      a gate could not be passed -> lead parked / discarded
    account_created   a job-application account was created (name only, never creds)
    submitted         application submitted with explicit confirmation
    employer_response any employer reply: rejection, interview invite, message, assessment
    error             unexpected failure (tool crash, malformed data, ...)

Never log secret values: passwords, verification codes, tokens, API keys.
Log gate TYPES and outcomes only. The helper refuses details keys that look
like secrets and drops them with a warning instead of writing them.

Backfilled rows carry "backfilled": true inside details and must only encode
events evidenced by the ledger/queues — never invented.
"""

import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
from keel_paths import HOME as PIPE  # noqa: E402
EVENTS = os.path.join(PIPE, "data", "telemetry", "events.jsonl")

EVENT_TYPES = {
    "lead_discovered", "lead_verified", "lead_dead", "brief_built",
    "browser_launched", "gate_encountered", "gate_cleared", "gate_blocked",
    "account_created", "submitted", "employer_response", "error",
}

# Stable gate vocabulary for details["gate"] on gate_encountered/gate_blocked.
# "needs_input" is reserved for items that genuinely need the applicant's own
# words/answers/choices. Agent-doable verification goes to
# "pending_verification"; fit rejections to "low_fit". Unknown values are
# warned on (stderr) but still logged — the append flow must never break.
GATE_TYPES = {
    "needs_input", "pending_verification", "unverifiable", "low_fit",
    "essay", "wording", "travel", "attest", "account", "captcha",
    "location", "reference", "salary", "degree", "relocation",
    "login", "eligibility", "technique_blocked", "submit_failed",
    "compensation_fail", "resume_s3_upload", "recaptcha_enterprise",
    "email_security_code", "answer_mapping", "discarded_by_applicant",
    "unclassified_parked", "fabrication", "lever_preflight", "lever_blocked",
    "edge_flip", "new_ats_detected", "ashby_spam_flag",
}

# Detail keys that must never reach the log (values are secrets by definition).
SECRET_KEYS = {
    "password", "passwd", "pass", "token", "secret", "api_key", "apikey",
    "verification_code", "verify_code", "otp", "one_time_code", "code",
    "credential", "credentials", "session", "cookie", "auth",
}


def scrub(details: dict) -> dict:
    """Drop secret-looking detail keys. Returns a cleaned copy."""
    clean = {}
    for k, v in details.items():
        if str(k).lower() in SECRET_KEYS:
            print(f"  telemetry: dropped secret-looking detail key '{k}' (not logged)",
                  file=sys.stderr)
            continue
        clean[k] = v
    return clean


def log(event_type: str, role_id: str = "", company: str = "", ats: str = "",
        source: str = "", details: dict = None) -> dict:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type '{event_type}'. "
                         f"valid: {sorted(EVENT_TYPES)}")
    details = scrub(details or {})
    gate = details.get("gate")
    if gate and gate not in GATE_TYPES:
        print(f"  telemetry: unknown gate '{gate}' — not in GATE_TYPES; "
              f"logged anyway, consider adding it to the vocabulary",
              file=sys.stderr)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "role_id": role_id or "",
        "company": company or "",
        "ats": ats or "",
        "source": source or "",
        "details": details,
    }
    os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
    with open(EVENTS, "a") as f:
        f.write(json.dumps(event) + "\n")
    return event


def main(argv):
    if not argv or argv[0] not in EVENT_TYPES:
        sys.exit(
            "usage: log_event.py <event_type> [--role-id ID] [--company NAME] "
            "[--ats ATS] [--source SRC] [--details '{\"k\":\"v\"}']\n"
            f"event types: {sorted(EVENT_TYPES)}"
        )
    event_type = argv[0]
    role_id = company = ats = source = ""
    details = {}
    i = 1
    while i < len(argv):
        if argv[i] == "--role-id" and i + 1 < len(argv):
            role_id = argv[i + 1]; i += 2
        elif argv[i] == "--company" and i + 1 < len(argv):
            company = argv[i + 1]; i += 2
        elif argv[i] == "--ats" and i + 1 < len(argv):
            ats = argv[i + 1]; i += 2
        elif argv[i] == "--source" and i + 1 < len(argv):
            source = argv[i + 1]; i += 2
        elif argv[i] == "--details" and i + 1 < len(argv):
            details = json.loads(argv[i + 1]); i += 2
        else:
            # Fail loud on unrecognized tokens: silently swallowing a typo'd
            # flag (e.g. --role_id) once produced malformed telemetry rows.
            sys.exit(f"log_event.py: unrecognized argument {argv[i]!r}\n"
                     "usage: log_event.py <event_type> [--role-id ID] [--company NAME] "
                     "[--ats ATS] [--source SRC] [--details '{\"k\":\"v\"}']")
    ev = log(event_type, role_id, company, ats, source, details)
    print(f"logged {ev['event_type']} {role_id or company or ''}".strip())


if __name__ == "__main__":
    main(sys.argv[1:])
