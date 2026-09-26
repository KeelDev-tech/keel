"""Telemetry event logger — append-only JSONL event log for the pipeline.

Every engine and subagent logs with one call:

    python3 log_event.py gate_encountered --role-id X --company "Acme" \\
        --ats greenhouse --source discovery-sweep --details '{"gate": "captcha"}'

Event types (keep this list stable; the analyzer depends on it):
    lead_discovered   a new lead entered the system (discovery sweeps)
    lead_verified     posting confirmed live + accepting applications
    lead_dead         posting expired / employer dead / scam-flagged (no application)
    staged_ingested   a staged discovery lead merged into a queue (staging file in details)
    staged_rejected   a staged discovery lead refused at ingestion (reason in details)
    staging_flood_gate junk batch quarantined as a whole — one event instead
                      of per-entry staged_rejected noise
    brief_built       a browser-task brief was generated
    browser_launched  a browser application task was spawned
    gate_encountered  a blocker appeared mid-flow (captcha, email code, account,
                      attestation, travel, wording, login...)
    gate_cleared      a gate was passed (gate name in details; never the secret value)
    gate_blocked      a gate could not be passed -> lead parked / discarded
    account_created   a job-application account was created (name only, never creds)
    submitted         legacy input alias for submission_claimed
    submission_claimed reported submission; unverified unless explicitly labeled
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
import re
import sys
import uuid
import fcntl
import contextlib
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
from keel_paths import HOME as PIPE  # noqa: E402
EVENTS = os.path.join(PIPE, "data", "telemetry", "events.jsonl")

EVENT_TYPES = {
    "lead_discovered", "lead_verified", "lead_dead", "brief_built",
    "browser_launched", "gate_encountered", "gate_cleared", "gate_blocked",
    "account_created", "submitted", "employer_response", "error",
    "staged_ingested", "staged_rejected", "staging_flood_gate",
    # 2026-09-18 (Keel 0.6.0 trust port): keel_trust authority admission —
    # single scoped local admission; never submission evidence. Registered
    # additively in the EVENT vocabulary (not GATE_TYPES — it is not a gate).
    "authority_use",
    # 2026-09-19 (Keel 0.11.0 port): keel_local.supply PoolHealthObserver
    # emits "pool_health" through injected loggers, and
    # monitors/pulse_snapshot.py reads pool_health events from this log;
    # the 0.11-ported monitors/pool_health.py wires PoolHealthObserver to
    # this very logger. Aggregate supply observation; never a verification
    # outcome. Registered additively — canonical names (incl. "submitted")
    # are unchanged.
    "pool_health",
    # 2026-09-19 (silent-defect sweep): producers emit these, but the logger
    # rejected them (ValueError, swallowed by the callers' except) — run
    # telemetry was silently dropped. Registered additively; spellings are
    # verbatim the producer emissions:
    # - engines/verify_cron.py:249 emits "verify_cron_run"
    # - engines/verify_retry.py:1029-1045 emits "url_enriched"/"url_enrich_failed"
    "verify_cron_run",
    "url_enriched", "url_enrich_failed",
    # 2026-09-22 (COV-http_429_halt port): discovery-lane 429 hard stops
    # were stdout-only — invisible to the safety coverage map. One row per
    # halted run, emitted via emit_429_halt() below. Registered additively;
    # spellings are verbatim the producer emissions.
    "http_429_halt",
    # Public observation vocabulary. Claim names do not prove execution
    # or independent verification; legacy submitted input is normalized.
    "flow_attempt", "packet_prepared", "submission_claimed",
    "verification_attempt", "source_sync", "pipeline_recovery",
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
    "breezy_blocked",  # pulse 34 (2026-09-15): ARM 112 dry-run pre-flight refusal gate
    "packet_shelved",  # pulse 35 (2026-09-15): ARM 118 emits gate_blocked/details.gate=packet_shelved at every unsubmitted _archive_packet point
    "edge_flip", "new_ats_detected", "ashby_spam_flag",
    # J-20260922-1859-ats--4856 (2026-09-22): the probe heuristic tried to
    # auto-flip an operator-authorized supervised verdict but the pin held —
    # verdict unchanged, no auto-overwrite. Emitted by edge_probe.
    "edge_flip_pin_held",
    "materials_missing", "materials_demoted", "inflight_cap",
    "feeder_empty", "stale_inflight",
    "recording_consent",  # ARM 67 (2026-09-15): required interview-recording consent is the applicant's own decision
    # ARM 101 (2026-09-15): required no-AI/AI-policy attestation — a hard
    # never-attest boundary.
    "ai_attestation",
    # ARM 45: packet-starvation watchdog — launch packet written but no
    # browser_launched claim within the starvation window.
    "packet_starved",
    # 2026-09-15 (never-halt directive): lane_watchdog.py —
    # buffer loaded but no IN-FLIGHT and no browser launch in 30m.
    "lane_stalled",
    # ARM 123 (2026-09-15): register gate values emitted by live code but
    # previously outside GATE_TYPES (found by ARM 119's emission
    # pre-mortem; all 0 firings so far — prevention, not repair).
    # apply_loop.py:1130 — employer rate-limit refusal path.
    "rate_limit",
    # apply_loop.py:1583 — live api-direct attempt without confirmation.
    "api_direct_failed",
    # feeder_watchdog.py:73/238 — IN-FLIGHT marker cannot be aged (ARM 79).
    "marker_timestamp_missing",
    # 2026-09-15: two-lane concurrency test (operator-authorized). Claim TTL
    # sweeps and test aborts.
    "stale_claim_released",  # lane_watchdog released a claim with no heartbeat in 15 min
    "abort_two_lane_test",  # both lanes aborted per the test's abort conditions
    # 2026-09-15: the email-code resend-and-refresh protocol. Emitted by the
    # supervisor around the Greenhouse 8-char email-code screen.
    "code_gate_resend",  # a resend-and-refresh cycle was executed (fresh poll + fresh reference)
    "code_gate_recovered",  # submission completed after a resend cycle
    "gh_code_gate_exhausted",  # 2 resend cycles failed; parked fail-closed
    # J-20260915-2310-brow-352 (ARM 74-2 pre-mortem, edge-case arm build,
    # 2026-09-15): visible hCaptcha challenge exhausted on the browser lane
    # (Lever, 23:03Z + 23:10Z) — emitted by live code, now registered so the
    # logger no longer warns. Additive only; nothing renamed.
    "hcaptcha_exhausted",
    # J-20260915-2348-gate-407 (ARM 79-G, 2026-09-15): two gate values emitted
    # in production telemetry but previously outside GATE_TYPES
    # (telemetry 2026-09-15T23:07:58Z familiarity_question, Faire;
    # 2026-09-15T23:12:35Z needs_applicant_input, Attentive) — both genuine
    # parked-on-applicant-input blockers. Sanitized from production's
    # name-bearing token; the value is equivalent.
    "familiarity_question", "needs_applicant_input",
    # ENGINE-BRIDGE (2026-09-16, gap-hunt #11): register the remaining gate
    # values firing in production telemetry. Additive only; nothing renamed.
    # verify_retry.py / board-API probes emit lead_dead on dead postings.
    "lead_dead",
    # dedupe_gate.py / dedupe_index.py / queue_intake.py / staging_ingest.py
    # emit duplicate / duplicate_of_submitted on canonical-dedupe hits.
    "duplicate", "duplicate_of_submitted",
    # 2026-09-16 (J-20260916-0050-sour-441): cross-host gh_jid merge
    # (SWEEP19P-STRIPE into CENSUS3X-GH-STRIPE) emitted duplicate_merged
    # in production telemetry; registered so the logger no longer warns.
    "duplicate_merged",
    # 2026-09-19 (silent-defect sweep): monitors/ewma_drift.py's module
    # docstring (:45-47) claims these gate values are "registered
    # additively in log_event.GATE_TYPES" — they never were, so the
    # monitor's drift alerts would be warned-on (stderr) rather than
    # clean vocabulary. Registered additively; spellings are verbatim the
    # docstring's emission contract.
    "yield_drift", "gate_surge",
    # 2026-09-16 (ARM 142 / pulse 142): CS-Recruiting NAPA-CA held as a
    # location-suffix twin of the SUBMITTED AMCANYON twin; gate_blocked
    # emitted with gate=duplicate_application (was unknown -> warning).
    "duplicate_application",
    # 2026-09-16 (ARM 142 / pulse 142): title_triage_deferral fired 106x in
    # production telemetry (gate_cleared by gap-bridge on triage-deferral
    # recovery, e.g. the Kitsch re-triage wave); registered so the logger
    # no longer warns on this de-facto vocabulary.
    "title_triage_deferral",
    # 2026-09-16 (ARM 142): lane_watchdog.py IDLE_READY_GATE — cumulative
    # idle-with-READY>0 tripwire (detection only, deduped 1/hr); emitted by
    # live code, registered per the ARM 123 precedent.
    "lane_idle_with_ready",
    # 2026-09-16 (sustainability matrix): lane_watchdog.py emits
    # gate_encountered with gate=sustainability_redline when a policy red
    # line with a live feed is breached (detection only, deduped 6h).
    # Registered so the logger no longer warns; additive only.
    "sustainability_redline",
    # 2026-09-16 (P-2026-09-16-verify-1): verify_retry.py
    # emits gate_encountered with gate=cooldown_flip when a 72h-track
    # (board-API-live) lead re-checks non-live. Trial gate for the 72h
    # differentiated cooldown; additive only.
    "cooldown_flip",
    # board-API probes emit gate_cleared with dry_run / resume_text
    # (payload assembly verified without POST; pasted-text resume path).
    "dry_run", "resume_text",
    # Historical vocabulary still referenced by outcome-tracking analytics
    # (downtime_analytics.py) and present in production telemetry.
    "malformed_ready", "stale_inflight_reset", "verify_resurrection",
    "future_dated_timestamps", "gh_code_approval_unavailable",
    # ENGINE-BRIDGE (2026-09-16, gap-hunt #2/#5): learning-leak audit
    # (learning_leak_audit.py) emits this when the technique-evidence
    # capture rate drops below the documented floor.
    "learning_leak",
    # 2026-09-16 (Spoiler Alert re-adjudication): main agent emits
    # gate_cleared with gate=false_positive_rejection when a documented
    # false-positive rejection is overturned with live-page evidence.
    # Registered so the logger no longer warns; additive only.
    "false_positive_rejection",
    # ENGINE-BRIDGE (2026-09-16, J-20260916-0045-veri-440): a verify_retry
    # death verdict is later proven wrong (posting was live). Emitted on
    # false-death reversals; the going-forward counter for the false-death
    # rate metric.
    "verify_false_death",
    # 2026-09-16 (item 2/10, answer-consistency guard): prescreen.park_lead
    # emits gate_blocked with gate=answer_mismatch when a finalized
    # packet's rendered answer list diverges from answer_bank.json
    # (the Zipline sponsorship-inversion class). Registered additively
    # per the ARM 123 precedent so the logger never warns on it.
    "answer_mismatch",
    # C-19 (2026-09-16): a park whose post-write read-back
    # failed — the queue write diverged from the telemetry park event.
    # The queue stays the system of record; ok:false is the enforcement.
    "park_divergence",
    # BROWSER-THROUGHPUT arm (2026-09-16, J-20260916-0307-brow-514):
    # lane_watchdog.py emits ready_pool_drained when the READY pool is
    # empty and the lane is idle while upstream supply exists
    # (pending-verify backlog or recent discovery inflow) — distinguishes
    # "drained but supply upstream" from "truly empty". Additive only;
    # nothing renamed.
    "ready_pool_drained",
    # API-DIRECT FAST LANE (2026-09-16, J-20260916-0541-meth-604):
    # the API-direct fast lane's human gate + launch-lock integration.
    # Additive only.
    "human_gate",            # live refused: no/awaiting/expired approval
    "human_gate_staged",     # dry-run ok, staged awaiting human decision
    "payload_mismatch",      # live refused: payload changed since approval
    "launch_lock_held",      # live refused: another task owns the role_id
    "cross_queue_ambiguity", # refused: role_id in both queues (fail-closed)
}

# Detail keys that must never reach the log (values are secrets by definition).
SECRET_KEYS = {
    "password", "passwd", "pass", "token", "secret", "api_key", "apikey",
    "verification_code", "verify_code", "otp", "one_time_code", "code",
    "credential", "credentials", "session", "cookie", "auth",
}


def scrub(details, _depth=0):
    """Bounded recursive redaction (ported from Keel 0.11 reference, 2026-09-18).
    Secret-looking keys are redacted to [REDACTED] (key kept, value hidden);
    Bearer credentials and key=value secret patterns inside strings are
    redacted too. Replaces the previous top-level-only drop. Free prose may
    still contain personal data."""
    if _depth > 16:
        raise ValueError("telemetry nesting limit")
    if isinstance(details, dict):
        clean = {}
        for key, value in details.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {re.sub(r"[^a-z0-9]", "", k) for k in SECRET_KEYS} or any(
                    fragment in normalized for fragment in
                    ("password", "token", "secret", "apikey", "credential", "cookie", "authorization")):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = scrub(value, _depth + 1)
        return clean
    if isinstance(details, list):
        return [scrub(value, _depth + 1) for value in details]
    if isinstance(details, str):
        details = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", details)
        details = re.sub(r"(?i)((?:password|token|api_key|secret)=)[^&\s]+", r"\1[REDACTED]", details)
        return details
    if isinstance(details, float) and (details != details or details in (float("inf"), float("-inf"))):
        raise ValueError("non-finite telemetry value")
    if details is not None and not isinstance(details, (bool, int, float, str)):
        raise ValueError(f"unsupported telemetry value type: {type(details).__name__}")
    return details


@contextlib.contextmanager
def _event_lock():
    """Exclusive advisory lock so event_id conflict check + append is atomic.

    Additive for the trust authority receipt contract (single grant use under
    concurrency). The lock file lives next to the events file; the log stays
    append-only JSONL.
    """
    os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
    with open(EVENTS + ".lock", "a") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _sync_event_directory():
    """Persist the event name and any first-use directory names before receipt.

    A prior failed append may have created ancestors without syncing them, so
    replay also syncs the chain; existence alone is not a persistence receipt.
    """
    directory = os.path.abspath(os.path.dirname(EVENTS) or '.')
    while True:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent


def log(event_type: str, role_id: str = "", company: str = "", ats: str = "",
        source: str = "", details: dict = None, event_id: str = None) -> dict:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type '{event_type}'. "
                         f"valid: {sorted(EVENT_TYPES)}")
    if details is not None and not isinstance(details, dict):
        raise ValueError("telemetry details must be an object")
    details = scrub({} if details is None else details)
    if event_type == "submitted":
        event_type = "submission_claimed"
    if event_type == "submission_claimed":
        # This logger records observations, not provider verification.
        # Preserve an explicit caller label without inferring one from
        # free text, a confirmation quote, or an unrelated verified flag.
        details.setdefault("verification_status", "UNVERIFIED")
    # 2026-09-18 (Keel 0.5.0 trust port): optional caller-supplied event id.
    # keel_trust's grant-consumption receipt requires the logger to echo a
    # caller-supplied event_id back in the returned receipt. Additive only:
    # event_id=None preserves the previous call shape (an id is generated).
    if event_id is None:
        event_id = uuid.uuid4().hex
        caller_supplied_event_id = False
    elif not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", event_id):
        raise ValueError("invalid event_id")
    else:
        caller_supplied_event_id = True
    gate = details.get("gate")
    if gate and gate not in GATE_TYPES:
        print(f"  telemetry: unknown gate '{gate}' — not in GATE_TYPES; "
              f"logged anyway, consider adding it to the vocabulary",
              file=sys.stderr)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "event_id": event_id,
        "role_id": role_id or "",
        "company": scrub(str(company or "")),
        "ats": ats or "",
        "source": source or "",
        "details": details,
    }
    # 2026-09-18 (Keel 0.6.0 trust port): minimal event-id replay semantics.
    # When the caller supplies an event_id, the check-and-append is atomic:
    # an identical earlier payload is returned as-is (idempotent replay, no
    # new row); a conflicting payload fails closed with ValueError; only a
    # genuinely new event_id is appended. Callers without an event_id keep
    # the previous plain-append behavior.
    comparison = {k: v for k, v in event.items() if k != "ts"}
    os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
    with _event_lock():
        if caller_supplied_event_id:
            try:
                with open(EVENTS, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        previous = json.loads(line)
                        if previous.get("event_id") == event_id:
                            if {k: v for k, v in previous.items() if k != "ts"} != comparison:
                                raise ValueError("event_id reused with a different payload")
                            # A previous append may have reached the page cache
                            # before its fsync failed. Replay must establish
                            # durability before returning the existing receipt.
                            os.fsync(f.fileno())
                            _sync_event_directory()
                            return previous
            except FileNotFoundError:
                pass
        with open(EVENTS, "a") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _sync_event_directory()
    return event


def emit_429_halt(scope, details=None, source=None):
    """Emit one http_429_halt telemetry row for an HTTP 429 hard stop.

    COV-http_429_halt (2026-09-18): 429 hard stops across the discovery
    lane previously printed to stdout/stderr only — invisible to the
    safety coverage map (a never-instrumented emergency path is a blind
    spot). One row per halted run/scope; details carry the scope plus
    whatever the halt site knows (board, host, propagated_from, counts).
    Additive, read-only observability: the halt behavior at the call site
    (exit 75 / platform skip / cooldown write) is unchanged, and emission
    can never raise — a telemetry failure degrades to a stderr note
    (fail closed, the never-halt doctrine).
    """
    try:
        log("http_429_halt", role_id="", source=source or scope,
            details={"scope": scope, "halt": "HTTP 429 hard stop",
                     **(details or {})})
    except Exception as ex:  # fail closed: never break the halt path
        print(f"  telemetry: http_429_halt emission failed ({ex})",
              file=sys.stderr)


def log_gate_aggregate(gate, leads, source, reason="", extra=None):
    """Log ONE aggregate gate_blocked event for a batch/triage run.

    `leads` is a list of {"role_id", ...} dicts (or plain role_id strings)
    that all hit the same `gate` in one batch. The event carries
    details.count (affected leads) + details.role_ids (full list), so no
    information is lost versus one event per lead.

    2026-09-15 telemetry-noise fix (ARM 60): the pv-unverifiable triage
    emitted 10 gate_blocked(unverifiable) events in 1 second — the same
    one-event-per-lead shape as the inflight_cap storm fixed at 02:50 PDT
    (apply_loop.emit_cap_cycle_event). Batch and triage scripts must use
    this aggregate, never a per-lead loop. Empty `leads` -> no event.
    The event is batch-level (no single lead), so log() marks its role_id
    'unknown' with a warning, exactly like the inflight_cap cycle event.
    """
    leads = leads or []
    if not leads:
        return None
    role_ids = [l.get("role_id") if isinstance(l, dict) else l
                for l in leads]
    details = {
        "gate": gate,
        "cycle_event": True,
        "count": len(leads),
        "role_ids": role_ids,
        "reason": reason or (f"{len(leads)} lead(s) hit gate '{gate}' in "
                             "one batch run; aggregate replaces per-lead "
                             "events"),
    }
    if extra:
        details.update(extra)
    return log("gate_blocked", role_id="", company="", ats="",
               source=source, details=details)


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
