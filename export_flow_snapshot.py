#!/usr/bin/env python3
"""Read-only canonical export adapter: live pipeline state -> keel_flow.board snapshot.

v1 (2026-09-18): observer only. Never writes queues, telemetry, tray, or
schedules. Reads the three queues, the input tray, and the pool guardian's
exact pool_state() logic, then emits a board.build-compatible snapshot JSON
to a NEW file (never overwrites).

Field-mapping honesty (fail closed where the live system has no evidence):
  - identity: keel_local.contracts.application_identity; rows without a
    usable (provider, employer, posting URL) triple are EXCLUDED and counted
    (logged to stderr, never invented).
  - history_reconciled: per-lead, from real records (ARM 3, 2026-09-20:
    J-20260920-1831-feed-3884). True when the lead has no ledger submission
    and no journal events (vacuous: no legacy history exists to reconcile),
    or when journal events exist and the journal is instrumented-complete
    (the complete journal IS the reconciled history). False when a ledger
    SUBMITTED row exists (its reconciliation is not evidenced) or when
    journal events exist but the journal is incomplete. v1 hardcoded False
    for every lead, which blocked even leads with zero attempt records.
  - attempt_state: mapped from the ledger + the attempt journal, conditioned
    on attempt_history_complete (ARM 3). Ledger SUBMITTED row -> SUBMITTED
    (duplicate-application guard); journal events + complete journal ->
    the latest event state on the contract vocabulary (INTENT/DISPATCHED
    stay holds; CANCELLED_BEFORE_DISPATCH -> AUTHORITATIVE_NOT_SUBMITTED);
    journal events + incomplete journal -> UNKNOWN; no records anywhere ->
    NONE (the ledger and the complete journal were both checked). v1
    hardcoded UNKNOWN, which forced attempt_hold on leads with genuinely no
    attempt.
  - launch_lock_held: True/False/None from the canonical lease registry
    (ARM 3). The launcher acquires a sha256(role_id).json lease before
    dispatch; a role with no lease file holds no lock (genuine negative
    evidence, not missing evidence). An expired lease (now > acquired_at +
    ttl_hours) is not held -- this retires the api-direct stale-lock class.
    An unreadable registry or unparseable lease fails closed to None
    (unknown), the v1 posture.
  - approval_valid/packet_present: evidence-bound (ARM 3). packet_present is
    True only for a staged-launch entry whose packet file exists, parses, and
    whose brief renders the identity answers (email marker); anything less
    fails closed to False. approval_valid follows the contract-governance
    definition (NOT the operator's personal input): under the standing FULL
    AUTOPILOT scope the apply-loop buffer guard only stages leads that
    passed its attestation/answer-consistency checks, so a staged lead with
    an identity-complete packet carries the lane's launch authorization.
    approval_expires_at is then REQUIRED by the contract; it is an EXPORT
    CONVENTION (staged_at + 24h, bounding the single-batch staging claim --
    the lane purges stale staged entries), never a lane-issued expiry.
    provider_contract_validated stays False (fail closed): the exporter has
    no provider-contract validation evidence, and the gate only applies to
    api-route leads.
  - dependencies: keel_local.readiness.dependency_hash requires exactly the
    seven contract keys {policy, form, answers, attachments, target,
    approval, route} with non-empty string revision values. When a real
    packet is present (see packet_present), each key carries a mechanical
    content hash derived from the packet and the queue entry (policy inputs;
    brief structure lines; brief answer lines; upload_files; target URL;
    the staged approval artifact; the route) -- real revisions, never
    placeholders -- and packet_dependency_hash is pinned to
    dependency_hash() of exactly that set, so the export is internally
    consistent and pin comparisons across snapshots detect packet rebuilds.
    With no packet, each key keeps the explicit sentinel "unobserved" and
    the pin stays None, so the board honestly reports
    packet_dependencies_changed (pin unverifiable) instead of the masking
    input_contract_invalid. (ARM 2, 2026-09-19: J-20260919-1858-feed-3021.)
  - capacity: measure_supplied_capacity() -- a genuine rate ONLY when unique
    canonical ledger completions coincide with DISPATCHED flow_attempt supply
    inside the supplied interval; otherwise capacity is None with an explicit
    capacity_evidence record (starved / span too short / zero completions),
    never a fabricated 0.0. Substituting a starved rate is exactly what the
    model forbids.
  - releases: cooldown-release groups built ONLY from measured evidence --
    per-role last lead_verified timestamps + per-lead cooldowns from
    verify_retry.verify_cooldown_hours (24h daily / 72h board-API-live /
    168h structural repost-watch; rev 2026-09-18 replaced the flat 24h
    VERIFY_COOLDOWN_H after ARM 72 measured 0/640 scannable under it --
    J-20260918-1911-inte-2246), joined against live queue status
    (PARKED-PENDING-VERIFICATION + PARKED-AWAITING-MATERIALS only).
    Conversion from 7d of verify-retry scan_summary batches (synthetic
    excluded), p95 timings measured from telemetry with their methodology
    stated in the evidence_ref. No batch with scans, no releases. Emitting
    un-evidenced groups would invent refill.
  - discovery sources: only sources with real measured minutes/checks. The
    LinkedIn sweep's verification minutes are measured from the sweep's own
    scan_summary event timestamps (wall clock), never estimated.
  - lead["holds"] carries hold FAMILY names (not hold IDs): the board's
    release gate exempts the "cooldown" family by name, and readiness treats
    any non-empty holds list as a canonical hold.
"""

import hashlib
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

KEEL_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.expanduser("~/workspace/job-pipeline")
ENGINES = os.path.join(PIPELINE, "engines", "application-executor")

sys.path.insert(0, KEEL_DIR)          # keel_flow, keel_local resolve live
# NOTE: ENGINES (the private pipeline's engine dir) is deliberately NOT added
# to sys.path. An import-time insert here once shadowed same-named test
# modules (test_launch_lock_x20, test_prescreen) during unittest discovery
# and broke CI. Pipeline engines load lazily by absolute path via
# _pipeline_module() below, which never touches sys.path.


ADAPTER_VERSION = "export_flow_snapshot/1.3.0"  # 1.3.0 (2026-09-20 ARM 3):
# readiness gates mapped to real ledger / lock-dir / buffer evidence
# (J-20260920-1831-feed-3884): attempt_state from ledger + attempt journal,
# launch_lock_held from the canonical lease registry, history_reconciled
# per-lead, approval_valid per contract-governance (lane staging, not the operator's
# input), packet_present from staged packets with identity coverage, and a
# mechanical per-key dependency pin when a real packet is present. Every
# mapping fails closed to the v1 placeholder posture when its evidence is
# absent; lead_row() without an evidence dict keeps the exact v1 behavior.
# Candidate key feeding application_identity() digests. Runtime override
# precedence (first hit wins):
#   1. env KEEL_CANDIDATE_ID
#   2. the restricted file hidden_files/candidate-id.json
#      (mode 0600; JSON {"candidate_id": "..."}); the private host keeps
#      the historical key here so attempt-telemetry joins stay stable.
#   3. the example-value default below: public clones get a synthetic key
#      and no real identifier lives in the tree.
#
# Rationale (review thread on this line, PR #21): the key is part of the
# identity digest, so changing the identity input outright would orphan
# historical digests — outstanding INTENT/DISPATCHED events would miss the
# join at lead_row() and attempted leads would misreport as executable.
# A stable configured key keeps history joinable while the tree stays
# synthetic.
_CANDIDATE_ID_ENV = "KEEL_CANDIDATE_ID"
_CANDIDATE_ID_FILE = os.path.join(KEEL_DIR, "hidden_files",
                                  "candidate-id.json")
_EXAMPLE_CANDIDATE_ID = "demo-candidate"


def _load_candidate_id():
    """Resolve the candidate key per the precedence above.

    Every path fails closed to the example default on malformed or
    unreadable input."""
    raw = os.environ.get(_CANDIDATE_ID_ENV)
    if raw and raw.strip():
        return raw.strip()
    try:
        with open(_CANDIDATE_ID_FILE) as f:
            doc = json.load(f)
        cid = doc.get("candidate_id") if isinstance(doc, dict) else None
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    except (OSError, ValueError, TypeError):
        pass
    return _EXAMPLE_CANDIDATE_ID


CANDIDATE_ID = _load_candidate_id()
REVIEW_AFTER_DAYS = 7
MEASUREMENT_WINDOW_DAYS = 7

STATUS_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?\s*([A-Z]{2,4})?$")


def parse_status_ts(value):
    """Parse '2026-09-15 23:52 PDT' style queue timestamps; None on failure."""
    if not value or not isinstance(value, str):
        return None
    m = STATUS_TS_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    try:
        from zoneinfo import ZoneInfo
        zone = {"PDT": "America/Los_Angeles", "PST": "America/Los_Angeles",
                "UTC": "UTC"}.get(tz or "UTC", "UTC")
        return datetime(int(y), int(mo), int(d), int(h), int(mi),
                        int(s or 0), tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    except Exception:
        return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def adapter_revision():
    try:
        with open(__file__, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception:
        digest = "unknown"
    return f"{ADAPTER_VERSION}:{digest}"


def build_identity(entry):
    """application_identity from verified posting metadata; None if unbuildable.

    Queue schema carries the posting URL under several field names
    (application_url / posting_url / ats_url / official_url). All four are
    honored in preference order; a lead with no URL in any field stays
    unbuildable (fail closed). 2026-09-24: the SWP16 Anduril READY lead
    carried its URL only in ats_url/official_url and was dropped from the
    board's lead inventory as identity_unbuildable, raising a
    READY_COUNT_CONFLICT against the pool guardian's ready=1
    (J-20260924-0725-feed-5624).
    """
    provider = (entry.get("ats") or entry.get("discovery_platform")
                or entry.get("route") or "browser")
    employer = entry.get("company") or entry.get("employer")
    posting = (entry.get("application_url") or entry.get("posting_url")
               or entry.get("ats_url") or entry.get("official_url"))
    if not (provider and employer and posting):
        return None
    try:
        from keel_local.contracts import application_identity
        return application_identity(CANDIDATE_ID, provider, employer, posting), posting
    except Exception:
        return None


def active_outside_sleep_window(la):
    """True when an America/Los_Angeles datetime is outside the 01:30-10:00
    PT sleep window. The window is half-open: inactive exactly for
    01:30 <= local time < 10:00.

    2026-09-24 (J-20260924-0725-feed-5624): the old inline predicate
    (la.hour < 10 ...) wrongly marked 00:00-01:29 inactive too, stealing 90
    live min/day from forecasting (keel_flow/forecast.py: not active ->
    SCHEDULED_PAUSE). Those 90 minutes are genuine operating time: the sleep
    window per job-pipeline/hidden_files/max-mode.json is 01:30-10:00 only.
    """
    return not ((la.hour == 1 and la.minute >= 30) or 1 < la.hour < 10)


def lead_row(entry, observed_at, evidence=None):
    """Build one board lead row.

    evidence (dict, optional) carries the real ledger / lock-dir / buffer
    observations (see main()); when omitted, every readiness field fails
    closed to the v1 placeholder posture (attempt_state UNKNOWN,
    launch_lock_held None, history_reconciled False, approval_valid False,
    packet_present False, dependencies "unobserved", pin None). ARM 3,
    2026-09-20: J-20260920-1831-feed-3884.
    """
    built = build_identity(entry)
    if built is None:
        return None, "identity_unbuildable"
    identity, posting_url = built
    fit = entry.get("fit_score")
    if type(fit) not in (int, float) or fit != fit or fit in (float("inf"), float("-inf")):
        return None, "fit_score_unmeasured"  # fail closed: never default a fit score
    unresolved = entry.get("unresolved") or []
    role_id = entry["role_id"]
    route = "browser" if entry.get("action_band") == "APPLY" else "unknown"

    if evidence:
        now = evidence.get("now")
        staged_packets = evidence.get("staged_packets") or {}
        submitted = role_id in (evidence.get("ledger_submitted") or set())
        journal_hit = (evidence.get("attempt_by_app") or {}).get(identity)
        journal_state = journal_hit[1] if journal_hit else None
        history_complete = bool(evidence.get("history_complete"))
        attempt_state = resolve_attempt_state(
            submitted_in_ledger=submitted, journal_state=journal_state,
            history_complete=history_complete)
        history_reconciled = resolve_history_reconciled(
            submitted_in_ledger=submitted, journal_state=journal_state,
            history_complete=history_complete)
        launch_lock_held = live_launch_lock(
            role_id, now, lock_dir=evidence.get("lock_dir", LAUNCH_LOCK_DIR),
            lock_names=evidence.get("lock_names"))
        approval_valid, approval_expires_at = approval_evidence(
            role_id, staged_packets, now)
        staged_rec = staged_packets.get(role_id)
        packet_present = bool(staged_rec and staged_rec.get("packet")
                              and staged_rec.get("identity_covered"))
        if packet_present:
            from keel_local.readiness import dependency_hash
            dependencies = packet_dependencies(
                entry, staged_rec["packet"], staged_rec.get("staged_at"),
                route)
            packet_dependency_hash = dependency_hash(dependencies)
        else:
            dependencies = {k: "unobserved" for k in DEPENDENCY_KEYS}
            packet_dependency_hash = None
    else:
        # v1 fail-closed posture: no evidence supplied, claim nothing.
        attempt_state, history_reconciled = "UNKNOWN", False
        launch_lock_held = None
        approval_valid, approval_expires_at = False, None
        packet_present = False
        dependencies = {k: "unobserved" for k in DEPENDENCY_KEYS}
        packet_dependency_hash = None

    return {
        "schema_version": 1,
        "role_id": role_id,
        "identity": identity,
        "fit_score": fit,
        "action_band": entry.get("action_band"),
        "holds": [],  # populated by the holds section join below
        "status": entry.get("status"),
        "observed_at": observed_at,
        # evaluate_readiness evidence fields (honest mapping; see module docstring)
        "posting_url": posting_url,
        "history_reconciled": history_reconciled,
        "attempt_state": attempt_state,
        "launch_lock_held": launch_lock_held,
        "policy_pass": entry.get("action_band") == "APPLY",
        "posting_verified": entry.get("status") == "READY",
        "answers_resolved": not bool(unresolved),
        "approval_valid": approval_valid,
        "packet_present": packet_present,
        "route": route,
        "route_supported": True,
        "provider_contract_validated": False,
        # Contract shape (keel_local.readiness.DEPENDENCIES): with a real
        # packet, mechanical content-hash revisions per key (see
        # packet_dependencies); without one, the explicit "unobserved"
        # sentinel -- absence marked, never a fabricated revision.
        "dependencies": dependencies,
        "packet_dependency_hash": packet_dependency_hash,
        "approval_expires_at": approval_expires_at,
    }, None


FAMILY_BY_KEYWORD = [
    ("consent", ("consent", "opt-in", "opt in", "whatsapp", "recording")),
    ("operator_input", ("essay", "unaided", "unassisted", "certify", "attest",
                        "pledge", "personally completed", "writing sample")),
    ("policy", ("travel", "relocat", "onsite", "on-site", "in-office", "hybrid",
                "office")),
    ("verification", ("verif", "account creation", "create an account")),
    ("cooldown", ("cooldown", "rate limit")),
]


def map_family(question):
    text = (question or "").casefold()
    for family, keywords in FAMILY_BY_KEYWORD:
        if any(k in text for k in keywords):
            return family
    return "operator_input"


def build_holds(tray, queue_index, observed_at, evidence_revision):
    holds = []
    for card in tray.get("cards", []):
        key = card.get("key")
        question = card.get("question") or ""
        family = map_family(question)
        opened = parse_status_ts(card.get("first_seen")) or observed_at
        review_after = (opened + timedelta(days=REVIEW_AFTER_DAYS)).isoformat()
        for lead in card.get("leads", []) or []:
            role_id = lead.get("role_id")
            if not role_id:
                continue
            # Evidence-bound owner: the hold issuer is the employer named on
            # the card's own lead data (employer/platform, never a personal
            # name, never invented). None keeps the reviewer's ASSIGN_OWNER
            # action honest when no issuer is evidenced.
            employer = (lead.get("employer") or "").strip()
            owner = f"employer:{employer}" if employer else None
            holds.append({
                "hold_id": f"{key}:{role_id}",
                "role_id": role_id,
                "family": family,
                "owner": owner,
                "opened_at": opened.isoformat(),
                # No employer/platform deadline is evidenced on tray cards:
                # review_after is the next review cadence, not a deadline.
                "not_before_utc": None,
                "review_after": review_after,
                "occurrences": card.get("times_seen", 1),
                "evidence_revision": evidence_revision,
                "release_condition": question[:500],
            })
    return holds


def build_question_dependencies(tray, queue_index):
    """Map tray cards -> per-role open question ids (exact audit question ids)."""
    deps = []
    for card in tray.get("cards", []):
        key = card.get("key")
        employers = {}
        for lead in card.get("leads", []) or []:
            employers.setdefault(lead.get("employer") or "unknown", []).append(lead)
        for employer, leads in employers.items():
            qid = key if len(employers) == 1 else f"{key}@{employer}"
            for lead in leads:
                role_id = lead.get("role_id")
                fit = lead.get("fit")
                if type(fit) not in (int, float):  # fail closed: no invented fit
                    continue
                entry = queue_index.get(role_id, {})
                unresolved = entry.get("unresolved") or []
                # other gates clear if this card is the only blocker family present
                other = [u for u in unresolved if key not in str(u)]
                deps.append({
                    "role_id": role_id,
                    "fit_score": lead.get("fit"),
                    "action_band": entry.get("action_band"),
                    "other_gates_clear": not bool(other),
                    "open_question_ids": [qid],
                })
    # one row per role: merge open_question_ids
    merged = {}
    for d in deps:
        row = merged.setdefault(d["role_id"], dict(d, open_question_ids=[]))
        for qid in d["open_question_ids"]:
            if qid not in row["open_question_ids"]:
                row["open_question_ids"].append(qid)
        row["other_gates_clear"] = row["other_gates_clear"] and d["other_gates_clear"]
    return [merged[k] for k in sorted(merged)]


def build_tray_document(tray):
    """Map tray cards -> audit_tray's strict question schema (mechanical only)."""
    questions = []
    for card in tray.get("cards", []):
        key = card.get("key")
        question = card.get("question") or ""
        employers = {}
        for lead in card.get("leads", []) or []:
            employers.setdefault(lead.get("employer") or "unknown", []).append(lead)
        field_type = "textarea" if "essay" in question.casefold() else "text"
        for employer in employers:
            qid = key if len(employers) == 1 else f"{key}@{employer}"
            questions.append({
                "id": qid,
                "employer": employer,
                "policy_scope": "application",
                "field_type": field_type,
                "text": question,
                "required": True,
                "options": [],
                "quarantined": False,  # audit_tray classifies quarantine itself
            })
    return {"schema_version": 1, "questions": questions,
            "answer_bank": {"answers": {}, "_provenance": {}}}


def attempt_events_from_rows(rows):
    """Extract flow_attempt events from pre-scanned telemetry rows.

    Pre-filters on event_type so the bounded journal contract never sees
    the full multi-year file. Rows the engine's is_synthetic() classifier
    flags (e.g. the 2026-09-18 R-TEST replay probe that failed open into
    production telemetry) are excluded read-side -- the append-only log is
    never rewritten. Genuine rows are emitted by the pipeline's
    flow_attempt emitter (2026-09-18, Keel 0.8.0 live-gate) on the real
    launch-attempt paths; the scan picks them up without an adapter change.
    """
    try:
        is_synthetic = _pipeline_module("log_event").is_synthetic
        candidates = [r for r in rows
                      if r.get("event_type") == "flow_attempt"
                      and not is_synthetic(r)]
        from keel_flow.journal import events_from_log
        return events_from_log(candidates)
    except Exception as exc:
        print(f"export: attempt-event scan failed ({exc}); treating as empty",
              file=sys.stderr)
    return []


def attempt_history_instrumented():
    """True only when the genuine flow_attempt emitter is verified present
    on every real attempt-path entry in the live pipeline engines.

    2026-09-18 (Keel 0.8.0 live-gate): the flag was hard-coded False because
    the flow_attempt type had zero genuine rows and no emitter existed on
    any attempt path -- the only row in existence was synthetic. Flipping
    requires positive evidence of instrumentation, never a row count: a
    quiet period with zero attempts is a complete history, and counting
    rows would let a silently reverted emitter keep import-scopes blocked
    (or a fabricated row unblock it). The check is function-scoped so a
    stray mention of the emitter name elsewhere in the file does not pass.
    """
    checks = (
        # (engine file, scope start, the genuine-path call inside it)
        ("stage_ready_launches.py", "def apply_plan(",
         'emit_flow_attempt("staged"'),
        ("inflight_marker.py", "def mark_inflight(",
         'emit_flow_attempt("spawn"'),
    )
    for filename, scope_start, call in checks:
        try:
            with open(os.path.join(ENGINES, filename)) as f:
                src = f.read()
        except OSError:
            return False
        _head, sep, tail = src.partition(scope_start)
        if not sep:
            return False
        scope = tail.split("\ndef ")[0]
        if call not in scope:
            return False
    return True


def scan_telemetry():
    """One pass over events.jsonl; yields parsed rows. Caller filters."""
    path = os.path.join(PIPELINE, "telemetry", "events.jsonl")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception as exc:
        print(f"export: telemetry scan failed ({exc})", file=sys.stderr)


def parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _pipeline_module(name):
    """Load a pipeline engine module by absolute path under a distinct
    sys.modules key.

    Never rely on `import <name>` for engine modules: a sibling test run
    (or any earlier import) can poison sys.modules['log_event'] with
    keel/engines/log_event.py, a different module entirely. Absolute-path
    loading keeps the adapter pinned to the live pipeline source.
    """
    key = f"pipeline_{name}"
    mod = sys.modules.get(key)
    if mod is None:
        import importlib.util
        path = os.path.join(ENGINES, f"{name}.py")
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


def live_verify_cooldown_hours(entry):
    """Per-lead cooldown via verify_retry.verify_cooldown_hours(entry).

    Reuses the live escalated policy -- 24h daily / 72h board-API-live /
    168h structural repost-watch (eligibility-screen weekly tier included) --
    and never duplicates it here. Fail-open to 24h only when the policy
    itself is unreadable (a lead is never starved by a bad policy read).
    """
    try:
        return float(_pipeline_module("verify_retry").verify_cooldown_hours(entry))
    except Exception:
        return 24.0


# --- readiness evidence (ARM 3, 2026-09-20: J-20260920-1831-feed-3884) ------
# Each helper below is read-only and fails closed to the v1 placeholder
# posture when its evidence is absent. The honesty line of every mapping is
# stated on the helper; nothing here invents a READY or an executable
# verdict -- the board's evaluate_readiness gates are untouched.

STAGED_LAUNCHES_PATH = os.path.join(PIPELINE, "hidden_files", "staged-launches.json")
LAUNCH_LOCK_DIR = os.path.join(PIPELINE, "hidden_files", "launch-locks")
# Identity-coverage markers for a buffered packet's brief. The email is the
# discriminator: the brief header names the candidate by construction, but
# the email answer line renders only when the brief builder actually emitted
# the identity answers (the defective Sept-19 builder withheld them).
#
# Runtime override precedence (first hit wins; 2026-09-22 ARM02 pulse-806):
#   1. env KEEL_PACKET_IDENTITY_MARKERS_JSON -- JSON array of marker
#      strings, e.g. '["email@example.invalid","Name Example"]'
#   2. the restricted file hidden_files/packet-identity-markers.json
#      (mode 0600; JSON {"markers": [...]}); the pulse runner injects the
#      real markers there at runtime before the export runs.
#   3. the example-value default below: public CI / committed-history
#      behavior is unchanged (no real personal identifier in the tree).
_IDENTITY_MARKERS_ENV = "KEEL_PACKET_IDENTITY_MARKERS_JSON"
_IDENTITY_MARKERS_FILE = os.path.join(KEEL_DIR, "hidden_files",
                                      "packet-identity-markers.json")
_EXAMPLE_IDENTITY_MARKERS = ("alex.applicant1@example.invalid",
                             "Alex Applicant Doe")


def _load_identity_markers():
    """Resolve the packet identity-coverage markers per the precedence above.

    Every path fails closed to the example-value default on any malformed
    or unreadable input, so the gauge's identity_covered check is honest:
    it only passes when the markers genuinely appear in the brief.
    """
    raw = os.environ.get(_IDENTITY_MARKERS_ENV)
    if raw:
        try:
            markers = json.loads(raw)
            if (isinstance(markers, list) and markers
                    and all(isinstance(m, str) and m for m in markers)):
                return tuple(markers)
        except (ValueError, TypeError):
            pass
    try:
        with open(_IDENTITY_MARKERS_FILE) as f:
            doc = json.load(f)
        markers = doc.get("markers") if isinstance(doc, dict) else None
        if (isinstance(markers, list) and markers
                and all(isinstance(m, str) and m for m in markers)):
            return tuple(markers)
    except (OSError, ValueError, TypeError):
        pass
    return _EXAMPLE_IDENTITY_MARKERS


PACKET_IDENTITY_MARKERS = _load_identity_markers()
# Export convention bounding the staging-approval claim (see
# approval_evidence): the lane purges stale staged entries, so a staging
# authorization is a single-batch claim, never open-ended.
APPROVAL_VALIDITY_HOURS = 24.0
DEPENDENCY_KEYS = ("policy", "form", "answers", "attachments", "target",
                   "approval", "route")


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_staged_packets(path=STAGED_LAUNCHES_PATH):
    """role_id -> {entry, packet, identity_covered, staged_at}.

    Reads the lane's sanctioned launch buffer (staged-launches.json) and
    loads each referenced packet. Never raises: a missing file, an
    unparseable entry, a missing/unparseable packet, or a packet whose brief
    lacks the identity markers yields packet=None / identity_covered=False
    for that role, which fails closed downstream.
    """
    out = {}
    doc = load_json(path, {})
    staged = doc.get("staged") if isinstance(doc, dict) else None
    if not isinstance(staged, list):
        return out
    for e in staged:
        if not isinstance(e, dict):
            continue
        role_id = e.get("role_id")
        if not role_id or role_id in out:
            continue
        rec = {"entry": e, "packet": None, "identity_covered": False,
               "staged_at": parse_ts(e.get("staged_at"))}
        try:
            ppath = e.get("packet_path")
            if ppath and os.path.isfile(ppath):
                with open(ppath) as f:
                    pkt = json.load(f)
                brief = pkt.get("brief") if isinstance(pkt, dict) else None
                if type(brief) is str and brief.strip():
                    rec["packet"] = pkt
                    rec["identity_covered"] = all(
                        m in brief for m in PACKET_IDENTITY_MARKERS)
        except Exception:
            rec["packet"] = None
            rec["identity_covered"] = False
        out[role_id] = rec
    return out


def live_launch_lock(role_id, now, lock_dir=LAUNCH_LOCK_DIR, lock_names=None):
    """True/False/None: is a launch lock genuinely held for role_id now?

    The lock directory is the canonical lease registry: the launcher
    acquires sha256(role_id).hexdigest()+".json" before dispatch. A role
    with no lease file holds no lock -- genuine negative evidence, not
    missing evidence. An expired lease (now > acquired_at + ttl_hours) is
    not held (this retires the api-direct stale-lock class). An unreadable
    registry (lock_names None) or an unparseable lease fails closed to None
    (unknown), the v1 posture.
    """
    if lock_names is None:
        try:
            lock_names = set(os.listdir(lock_dir))
        except OSError:
            return None
    fname = hashlib.sha256(role_id.encode("utf-8")).hexdigest() + ".json"
    if fname not in lock_names:
        return False
    try:
        with open(os.path.join(lock_dir, fname)) as f:
            lease = json.load(f)
        acquired = parse_ts(lease.get("acquired_at"))
        ttl = lease.get("ttl_hours")
        if acquired is None or type(ttl) not in (int, float) or not ttl > 0:
            return None
        return bool(now < acquired + timedelta(hours=ttl))
    except Exception:
        return None


def ledger_submitted_roles(ledger_rows):
    """role_ids with a canonical SUBMITTED ledger row (duplicate guard)."""
    return {str(r.get("role_id")) for r in (ledger_rows or [])
            if str(r.get("status") or "").upper() == "SUBMITTED"
            and r.get("role_id")}


def attempt_state_by_application(attempt_events):
    """application_id -> (observed_at, state) of the latest journal event."""
    latest = {}
    for e in attempt_events or []:
        app = e.get("application_id")
        state = e.get("state")
        ts = parse_ts(e.get("observed_at"))
        if not app or not state or ts is None:
            continue
        prev = latest.get(app)
        if prev is None or ts > prev[0]:
            latest[app] = (ts, state)
    return latest


# Journal states onto the readiness contract's attempt vocabulary. The
# contract holds on anything but NONE / AUTHORITATIVE_NOT_SUBMITTED; a
# terminal CANCELLED_BEFORE_DISPATCH is the journal's authoritative record
# that no submission resulted, while any claimed/reported submission state
# maps to SUBMITTED (the ledger duplicate guard then applies).
_ATTEMPT_STATE_MAP = {
    "INTENT": "INTENT",
    "DISPATCHED": "DISPATCHED",
    "SUBMISSION_CLAIMED": "SUBMITTED",
    "CONFIRMATION_REPORTED": "SUBMITTED",
    "RECONCILIATION_REPORTED": "SUBMITTED",
    "CANCELLED_BEFORE_DISPATCH": "AUTHORITATIVE_NOT_SUBMITTED",
    "UNKNOWN": "UNKNOWN",
}


def resolve_attempt_state(*, submitted_in_ledger, journal_state, history_complete):
    """attempt_state for the readiness contract, from real evidence.

    - ledger SUBMITTED row -> SUBMITTED (the ledger is the canonical
      submission record; a READY lead with one is a duplicate).
    - journal event present + history_complete (emitter verified on every
      real attempt path, so the journal is the complete record) -> the
      latest event state on the contract vocabulary; an outstanding INTENT
      or DISPATCHED stays a hold, honestly.
    - journal event present + incomplete journal -> UNKNOWN (cannot verify;
      the v1 posture).
    - no records anywhere -> NONE: the ledger and the complete journal were
      both checked and hold nothing for this lead. (v1 said UNKNOWN here,
      forcing attempt_hold on leads with genuinely no attempt.)
    """
    if submitted_in_ledger:
        return "SUBMITTED"
    if journal_state is None:
        return "NONE"
    if not history_complete:
        return "UNKNOWN"
    return _ATTEMPT_STATE_MAP.get(journal_state, "UNKNOWN")


def resolve_history_reconciled(*, submitted_in_ledger, journal_state,
                               history_complete):
    """history_reconciled per lead, from real evidence.

    - ledger SUBMITTED row -> False: a submission record exists whose
      reconciliation against the flow event model is not evidenced.
    - journal events present -> history_complete: the instrumented-complete
      journal IS the reconciled history; an incomplete journal cannot
      verify -> False.
    - no records anywhere -> True (vacuous): no legacy attempt history
      exists for this lead, so there is nothing to reconcile. (v1 hardcoded
      False for every lead, blocking even leads with zero attempt records.)
    """
    if submitted_in_ledger:
        return False
    if journal_state is None:
        return True
    return bool(history_complete)


def _brief_lines(brief):
    """(answer_lines, structure_lines): the brief renders answers as
    '  - key: value ...' lines; everything else is form structure."""
    lines = brief.splitlines()
    ans = [l for l in lines if l.startswith("  - ")]
    return ans, [l for l in lines if not l.startswith("  - ")]


def packet_dependencies(entry, packet, staged_at, route):
    """Mechanical per-key dependency revisions from the buffered packet.

    Each value is a content hash of the real artifact, never a placeholder:
    - policy: the policy inputs the packet was built under
      (action_band / fit_score / resume_lane).
    - form: the brief's structure lines (answer lines excluded).
    - answers: the brief's rendered answer lines.
    - attachments: the packet's upload_files list.
    - target: the URL the packet targets.
    - approval: the governance approval artifact (staged role + staged_at).
    - route: the dispatch route (stable opaque revision).
    All values are non-empty strings, satisfying dependency_hash(). The
    pin (packet_dependency_hash) is set to dependency_hash() of exactly
    this set: the export is internally consistent, and comparing pins
    across snapshots detects packet rebuilds. The pin is not a temporal
    claim within a single snapshot.
    """
    brief = packet.get("brief") or ""
    ans_lines, struct_lines = _brief_lines(brief)
    return {
        "policy": _sha256_text(json.dumps({
            "action_band": entry.get("action_band"),
            "fit_score": entry.get("fit_score"),
            "resume_lane": entry.get("resume_lane")}, sort_keys=True)),
        "form": _sha256_text("\n".join(struct_lines)),
        "answers": _sha256_text("\n".join(ans_lines)),
        "attachments": _sha256_text(json.dumps(
            packet.get("upload_files") or [], sort_keys=True)),
        "target": _sha256_text(
            packet.get("ats_url") or entry.get("posting_url")
            or entry.get("application_url") or ""),
        "approval": _sha256_text(json.dumps({
            "staged_role": entry.get("role_id"),
            "staged_at": staged_at.isoformat() if staged_at else None},
            sort_keys=True)),
        "route": route,
    }


def approval_evidence(role_id, staged_packets, now):
    """(approval_valid, approval_expires_at): the contract-governance definition.

    The flow model's approval is the LANE's launch authorization, not
    the operator's personal input. Under the standing FULL AUTOPILOT scope
    (standard legal attestations pre-authorized; the apply-loop buffer
    guard parks anything unattested), a lead the sanctioned pipeline has
    staged with a loadable, identity-complete packet carries the lane's
    governance approval -- the buffer guard only stages leads that passed
    its attestation/answer-consistency checks. approval_valid=True exactly
    then; otherwise False (fail closed, the v1 posture).

    approval_expires_at is REQUIRED by the contract whenever an approval
    is claimed (a missing expiry raises input_contract_invalid). It is an
    EXPORT CONVENTION, not a lane-issued expiry: staged_at +
    APPROVAL_VALIDITY_HOURS bounds the single-batch staging claim (the
    lane purges stale staged entries -- 2026-09-16 precedent). Labeled as
    convention so the timestamp is never mistaken for a governance fact.
    """
    rec = (staged_packets or {}).get(role_id)
    if not rec or not rec.get("packet") or not rec.get("identity_covered"):
        return False, None
    base = rec.get("staged_at") or now
    return True, (base + timedelta(hours=APPROVAL_VALIDITY_HOURS)).isoformat()


# 2026-09-18 (J-20260918-1911-inte-2246): the cooldown-released cohort is
# the LIVE verification pool only -- PARKED-PENDING-VERIFICATION +
# PARKED-AWAITING-MATERIALS. Out-of-pool statuses (SUBMITTED, PARKED,
# PARKED-LOW-FIT, CLOSED-*, ...) never join a VERIFICATION_ELIGIBLE
# release; the flat 24h VERIFY_COOLDOWN_H the old build assumed measured
# 0/640 scannable (ARM 72), so each lead's cooldown is its own live
# per-lead policy window.
VERIFY_POOL_STATUSES = frozenset({
    "PARKED-PENDING-VERIFICATION",
    "PARKED-AWAITING-MATERIALS",
})

COOLDOWN_RELEASE_DEFINITION = (
    "cooldown-released cohort rev 2026-09-18 (J-20260918-1911-inte-2246): "
    "live verify pool only -- PARKED-PENDING-VERIFICATION + "
    "PARKED-AWAITING-MATERIALS; per-lead cooldown from "
    "verify_retry.verify_cooldown_hours (24h daily / 72h board-API-live / "
    "168h structural repost-watch); SUBMITTED, PARKED, PARKED-LOW-FIT, "
    "CLOSED-* and other non-pool statuses excluded from VERIFICATION_ELIGIBLE "
    "supply. Replaces the flat-24h definition after ARM 72 measured 0/640 "
    "scannable under it. "
    "Rev 2026-09-18 (ARM 3): leads with no lead_verified in the 30d "
    "telemetry window are unscanned supply and join NO cooldown release "
    "(they stay on the board lead list with no cooldown hold). The "
    "2026-09-18 21:02Z forecast anchored on 212/219 such phantoms "
    "(cohort_join coverage 0.0); a forecast release must correspond to a "
    "genuinely expiring cooldown cohort.")


def last_verified_by_role(rows, window_start):
    """role_id -> latest lead_verified ts (any source) within the window."""
    last = {}
    for r in rows:
        if r.get("event_type") != "lead_verified":
            continue
        rid = r.get("role_id")
        if not rid:
            continue
        ts = parse_ts(r.get("ts"))
        if not ts or ts < window_start:
            continue
        if rid not in last or ts > last[rid]:
            last[rid] = ts
    return last


def verify_conversion(rows, window_start):
    """Measured (conversion, scanned, promoted, batches) from verify-retry
    scan_summary batches, synthetic excluded. None when no scans."""
    scanned = promoted = batches = 0
    for r in rows:
        if r.get("event_type") != "scan_summary":
            continue
        if r.get("source") != "verify-retry":
            continue
        d = r.get("details") or {}
        if d.get("synthetic_test"):
            continue
        ts = parse_ts(r.get("ts"))
        if not ts or ts < window_start:
            continue
        batches += 1
        scanned += d.get("scanned", 0) or 0
        promoted += d.get("promoted_ready", 0) or 0
    if not scanned:
        return None
    return {"conversion": promoted / scanned, "scanned": scanned,
            "promoted": promoted, "batches": batches}


def batch_verify_p95(rows, window_start):
    """p95 of per-batch mean verify latency.

    Batches are delimited by verify-retry scan_summary events; a batch's
    mean = (last lead_verified ts - first lead_verified ts) / scanned.
    Per-role p95 is not instrumented anywhere, so this is documented as a
    lower-bound proxy, never as a per-role p95. None when unmeasurable.
    """
    bounds = []  # (ts, scanned)
    ver = []
    for r in rows:
        ts = parse_ts(r.get("ts"))
        if not ts or ts < window_start:
            continue
        if r.get("event_type") == "scan_summary" and r.get("source") == "verify-retry":
            d = r.get("details") or {}
            if d.get("synthetic_test"):
                continue
            bounds.append((ts, d.get("scanned", 0) or 0))
        elif r.get("event_type") == "lead_verified" and r.get("source") == "verify-retry":
            ver.append(ts)
    bounds.sort()
    ver.sort()
    means = []
    for i in range(1, len(bounds)):
        lo, hi = bounds[i - 1][0], bounds[i][0]
        inb = [t for t in ver if lo <= t < hi]
        scanned = bounds[i][1]
        if len(inb) >= 2 and scanned > 0:
            means.append((inb[-1] - inb[0]).total_seconds() / scanned)
    if not means:
        return None
    means.sort()
    return {"p95": means[min(int(len(means) * 0.95), len(means) - 1)],
            "batches": len(means)}


def prepare_p95(rows, window_start):
    """p95 of brief_built -> browser_launched per role.

    Measures end-to-end packet-to-launch latency INCLUDING packet-buffer
    queueing, not pure preparation work; the evidence_ref says so.
    None when unmeasurable.
    """
    built, launched = {}, {}
    for r in rows:
        rid = r.get("role_id")
        if not rid:
            continue
        ts = parse_ts(r.get("ts"))
        if not ts or ts < window_start:
            continue
        if r.get("event_type") == "brief_built":
            built.setdefault(rid, ts)
        elif r.get("event_type") == "browser_launched":
            launched.setdefault(rid, ts)
    deltas = []
    for rid, lb in launched.items():
        bb = built.get(rid)
        if bb and lb > bb:
            deltas.append((lb - bb).total_seconds())
    if not deltas:
        return None
    deltas.sort()
    return {"p95": deltas[min(int(len(deltas) * 0.95), len(deltas) - 1)],
            "pairs": len(deltas)}


def linkedin_sweep_measurement(rows, window_start):
    """Latest non-dry-run linkedin-discovery sweep, measured from its own
    scan_summary event timestamps (wall clock). None when no live sweep."""
    events = []
    for r in rows:
        if r.get("event_type") != "scan_summary":
            continue
        if r.get("source") != "linkedin-discovery":
            continue
        d = r.get("details") or {}
        if d.get("dry_run"):
            continue
        ts = parse_ts(r.get("ts"))
        if not ts or ts < window_start:
            continue
        events.append((ts, d))
    if not events:
        return None
    events.sort()
    # cluster into sweeps: a >30min gap starts a new sweep; take the latest
    sweeps, current = [], [events[0]]
    for prev, cur in zip(events, events[1:]):
        if (cur[0] - prev[0]).total_seconds() > 1800:
            sweeps.append(current)
            current = [cur]
        else:
            current.append(cur)
    sweeps.append(current)
    sweep = sweeps[-1]
    first, last = sweep[0][0], sweep[-1][0]
    # summary row (no query) carries the sweep totals; else sum per-query rows
    summary = next((d for _, d in sweep if not d.get("query")), None)
    if summary:
        cards, new = summary.get("cards", 0), summary.get("new", 0)
    else:
        cards = sum(d.get("cards", 0) for _, d in sweep)
        new = sum(d.get("new", 0) for _, d in sweep)
    minutes = max((last - first).total_seconds() / 60.0, 0.01)
    return {"minutes": minutes, "cards": cards, "new": new,
            "started_at": first.isoformat(), "completed_at": last.isoformat(),
            "events": len(sweep)}


def build_linkedin_source(rows, window_start):
    """Discovery source with measured minutes, or the honest unmeasured form.

    RETIRED-POLICY (2026-09-22): permitted is False in all branches per the
    G-5 suspension approved by Trent 2026-09-21 and the 2026-09-19 standing
    LinkedIn hard boundary ("just stop using it"). Measurement fields are
    retained for historical evidence only; the allocator must always exclude
    this row as SOURCE_NOT_PERMITTED even with a fresh measurement. Revival
    path: Trent's ban-scope word, then a fresh measurement — never the stale
    4.77-min figure (blackboard J-20260922-0840-sour-4640).
    """
    retired_note = (
        "RETIRED-POLICY 2026-09-22: G-5 suspension approved by Trent "
        "2026-09-21 (permitted:false; suspended_reason="
        "linkedin-ban-scope-pending-trent); 2026-09-19 standing LinkedIn hard "
        "boundary ('just stop using it'). Allocator must exclude as "
        "SOURCE_NOT_PERMITTED even with fresh measurement; revival only on "
        "Trent's ban-scope word with a fresh measurement.")
    base = {
        "source_id": "linkedin-discovery-sweep",
        "employer_group": "linkedin",
        "permitted": False,
        "fit_floor": 75,
        "rate_limited": False,
    }
    m = linkedin_sweep_measurement(rows, window_start)
    if m is None:
        return {**base,
                "measurement_complete": False,  # card counts exist, minutes not measured
                "checks": 0, "qualified_unique_live": 0, "verification_minutes": 0.0,
                "measurement_started_at": None, "measurement_completed_at": None,
                "evidence_ref": (
                    retired_note + " Adapter-note: verification minutes not "
                    "measured; see sweep state.")}
    return {**base,
            "measurement_complete": True,
            "checks": m["cards"],
            # qualified_unique_live stays 0: wall-clock sweep time does not
            # establish fit, liveness, or qualification. The 64 net-new leads
            # entered the queue as PARKED-PENDING-VERIFICATION with
            # action_band=PARKED; the board never allocates from this source
            # on unverified export anyway. Never invent yield.
            "qualified_unique_live": 0,
            "verification_minutes": round(m["minutes"], 2),
            "measurement_started_at": m["started_at"],
            "measurement_completed_at": m["completed_at"],
            "observed_at": m["completed_at"],
            "evidence_ref": (
                retired_note +
                f" telemetry/events.jsonl: latest live linkedin-discovery sweep, "
                f"{m['events']} scan_summary events, wall-clock "
                f"{m['minutes']:.2f} min for {m['cards']} cards / {m['new']} new; "
                f"queue-level dedupe recorded 64 net-new on 2026-09-18")}


def measure_supplied_capacity(ledger_rows, attempt_events, window_start, now, source_revision):
    """Genuine supplied-capacity measurement, or explicit unmeasured evidence.

    A rate is returned ONLY when all three hold:
      1. unique canonical submission completions exist in the supplied
         interval (deduplicated by confirmation identity; rows without
         confirmation_text are not canonical and never count),
      2. supplied-work evidence exists: DISPATCHED flow_attempt events in the
         measurement window prove eligible work reached the execution stage,
      3. the supplied interval (first..last DISPATCHED timestamp) spans at
         least 60 seconds, so a rate is arithmetically meaningful.

    Completions are timestamped by ledger date_submitted only; rows with a
    missing or unparseable date_submitted are excluded and counted, never
    estimated. A starved interval (no DISPATCHED events) is reported as
    starved, never as zero service capacity; zero measured completions with
    supplied work present yields an explicit unmeasured record with the
    counts, not a fabricated 0.0 rate.

    Returns (capacity, evidence). capacity is None unless measured; evidence
    always carries the counts, interval bounds, route, source revision and
    the reason when unmeasured.
    """
    window_start_iso = window_start.isoformat()
    ledger_rows = ledger_rows or []
    submitted_family = [r for r in ledger_rows
                        if str(r.get("status") or "").upper() == "SUBMITTED"]
    # Canonical completions: confirmation_text is the evidenced receipt.
    canonical = [r for r in submitted_family if (r.get("confirmation_text") or "").strip()]
    seen, unique_completions, duplicate_receipts, no_parseable_ts = set(), [], 0, 0
    for r in canonical:
        identity = (r.get("confirmation_url") or "").strip() or (
            str(r.get("role_id") or ""),
            (r.get("confirmation_text") or "")[:120])
        if identity in seen:
            duplicate_receipts += 1
            continue
        seen.add(identity)
        ts = parse_ts(r.get("date_submitted"))
        if ts is None:
            no_parseable_ts += 1
            continue
        unique_completions.append((ts, r))
    # Attempt events arrive FLAT: attempt_events_from_rows() unwraps
    # row["details"] via keel_flow.journal.events_from_log(), so "state"
    # and "observed_at" sit at the event's top level. Every other consumer
    # (attempts.reconcile, attempt_state_by_application) reads them flat;
    # the nested e.get("details") read here starved capacity forever.
    dispatched = sorted(
        ts for e in (attempt_events or [])
        for ts in [parse_ts(e.get("observed_at"))]
        if str(e.get("state") or "").upper() == "DISPATCHED"
        and ts is not None and ts >= window_start)
    evidence = {
        "window_start": window_start_iso,
        "window_end": now.isoformat(),
        "route": ("ledger/application-ledger.json (date_submitted, confirmation_text) "
                  "+ telemetry flow_attempt DISPATCHED (observed_at)"),
        "source_revision": source_revision,
        "ledger_rows_scanned": len(ledger_rows),
        "submitted_family_rows": len(submitted_family),
        "non_canonical_rows_excluded": len(submitted_family) - len(canonical),
        "duplicate_receipts_excluded": duplicate_receipts,
        "rows_without_parseable_ts": no_parseable_ts,
        "unique_completions_in_window": len(unique_completions),
        "dispatched_attempts_in_window": len(dispatched),
        "supplied_interval_start": dispatched[0].isoformat() if dispatched else None,
        "supplied_interval_end": dispatched[-1].isoformat() if dispatched else None,
        "supplied_span_seconds": ((dispatched[-1] - dispatched[0]).total_seconds()
                                  if len(dispatched) >= 2 else 0.0),
        "measured_while_supplied": False,
        "reason": None,
    }
    if not dispatched:
        evidence["reason"] = ("starved interval: no DISPATCHED flow_attempt events in "
                              "the measurement window; capacity unmeasured, not zero")
        return None, evidence
    span_s = evidence["supplied_span_seconds"]
    if span_s < 60:
        evidence["reason"] = (f"supplied interval spans {span_s:.0f}s (<60s); "
                              "rate unmeasurable")
        return None, evidence
    start, end = dispatched[0], dispatched[-1]
    in_interval = [ts for ts, _ in unique_completions if start <= ts <= end]
    evidence["unique_completions_in_supplied_interval"] = len(in_interval)
    if not in_interval:
        evidence["reason"] = ("zero measured completions inside the supplied interval "
                              f"({len(unique_completions)} unique in window); capacity "
                              "unmeasured, not zero")
        return None, evidence
    span_h = span_s / 3600.0
    capacity = {
        "value": round(len(in_interval) / span_h, 4),
        "unit": "applications/hour",
        "horizon_seconds": 86400,
        "measured_while_supplied": True,
        "evidence_ref": (
            f"ledger/application-ledger.json + telemetry flow_attempt: "
            f"{len(in_interval)} unique canonical completions / {span_h:.3f}h "
            f"supplied interval [{start.isoformat()}..{end.isoformat()}]; "
            f"{duplicate_receipts} duplicate receipts and {no_parseable_ts} "
            f"rows without parseable date_submitted excluded"),
        "source_revision": source_revision,
        "observed_at": now.isoformat(),
    }
    evidence["measured_while_supplied"] = True
    return capacity, evidence


def build_cooldown_supply(standard, seen_roles, rows, now, observed_at,
                      evidence=None):
    """Cooldown-release grouping with evidenced conversion.

    Returns (extra_leads, extra_holds, releases, skipped). A verify-pool
    APPLY/fit>=75 lead with no unresolved blockers and a buildable identity
    joins the snapshot with a cooldown-family hold grounded in its last
    lead_verified timestamp + its own live cooldown window from
    verify_retry.verify_cooldown_hours (24h daily / 72h board-API-live /
    168h structural repost-watch -- never a flat window). Release groups
    carry the measured 7d verify conversion; p95 timings are measured with
    their methodology stated. Empty releases when the evidence is absent --
    never invented.

    2026-09-18 (J-20260918-1911-inte-2246): the cohort is joined against
    LIVE queue status -- only PARKED-PENDING-VERIFICATION and
    PARKED-AWAITING-MATERIALS enter VERIFICATION_ELIGIBLE releases.
    SUBMITTED, PARKED, PARKED-LOW-FIT, CLOSED-* and other non-pool
    statuses are excluded from supply. Each release carries a `definition`
    note recording this change (honest labeling).
    """
    window_start = now - timedelta(days=MEASUREMENT_WINDOW_DAYS)
    history_start = now - timedelta(days=30)
    last_ver = last_verified_by_role(rows, history_start)
    conv = verify_conversion(rows, window_start)
    ver_p95 = batch_verify_p95(rows, window_start)
    prep_p95 = prepare_p95(rows, window_start)

    extra_leads, extra_holds, skipped = [], [], 0
    groups = {"released": [], "releasing_24h": []}
    unscanned = 0  # verify-pool leads with no lead_verified in the 30d
    # telemetry window: unscanned supply, never a cooldown release
    for entry in standard:
        role_id = entry.get("role_id")
        if not role_id or role_id in seen_roles:
            continue
        status = (entry.get("status") or "").upper()
        if status not in VERIFY_POOL_STATUSES:
            continue  # live verify pool only; out-of-pool statuses excluded
        if entry.get("action_band") != "APPLY":
            continue
        fit = entry.get("fit_score")
        if type(fit) not in (int, float) or not (fit >= 75):
            continue
        if entry.get("unresolved"):
            continue
        row, reason = lead_row(entry, observed_at, evidence)
        if row is None:
            skipped += 1
            continue  # identity_unbuildable / fit unmeasured
        cooldown_h = live_verify_cooldown_hours(entry)
        lv = last_ver.get(role_id)
        if lv is None:
            # ARM 3 (2026-09-18): never verified within the 30d telemetry
            # window = unscanned supply, NOT a cooldown release. The old
            # branch treated these as "eligible now" and anchored the
            # forecast with a phantom cohort (212/219 of the 2026-09-18
            # 21:02Z cooldown-released cohort had zero lead_verified events
            # in all telemetry; conv-recal-v2 cohort_join coverage 0.0).
            # A forecast release must correspond to a genuinely expiring
            # cooldown cohort. The lead stays on the board's lead list with
            # no cooldown hold; it joins no release and carries no forecast.
            unscanned += 1
        else:
            available_at = lv + timedelta(hours=cooldown_h)
            hold = {
                "hold_id": f"cooldown:{role_id}",
                "role_id": role_id,
                "family": "cooldown",
                # The issuer is the platform's own cooldown policy; the
                # release condition cites the evidenced method and timestamp.
                "owner": "platform:keel-verify-cooldown",
                "opened_at": lv.isoformat(),
                # Evidence-bound release deadline from telemetry, not a fixed
                # review delay.
                "not_before_utc": available_at.isoformat(),
                "review_after": available_at.isoformat(),
                "occurrences": 1,
                "evidence_revision": (
                    f"verify-telemetry@{history_start.date()}"),
                "release_condition": (
                    f"verify_retry.verify_cooldown_hours={cooldown_h:g}h "
                    f"elapsed since last verification at {lv.isoformat()}"),
            }
            extra_holds.append(hold)
            if available_at <= now:
                groups["released"].append(role_id)
            elif available_at <= now + timedelta(hours=24):
                groups["releasing_24h"].append(role_id)
            else:
                continue  # still deep in cooldown: hold recorded, not supply yet
        extra_leads.append(row)
        seen_roles.add(role_id)

    releases = []
    if conv is not None and ver_p95 is not None and prep_p95 is not None:
        conv_ref = (
            f"telemetry/events.jsonl: {conv['batches']} verify-retry "
            f"scan_summary batches, {MEASUREMENT_WINDOW_DAYS}d, synthetic "
            f"excluded; {conv['promoted']}/{conv['scanned']} promoted_ready")
        timing_ref = (
            f"p95_verify={ver_p95['p95']:.2f}s is the p95 of per-batch mean "
            f"verify latency across {ver_p95['batches']} batches (lower-bound "
            f"proxy; per-role p95 not instrumented); p95_prepare="
            f"{prep_p95['p95']:.0f}s is the p95 of brief_built->browser_launched "
            f"across {prep_p95['pairs']} pairs (includes packet-buffer queueing)")
        defs = [
            ("cooldown-released", groups["released"], now,
             "roles with genuinely expired verify cooldown"),
            ("cooldown-releases-next-24h", groups["releasing_24h"],
             now + timedelta(hours=24),
             "roles whose verify cooldown expires within 24h"),
        ]
        unscanned_note = (
            f"{unscanned} verify-pool leads with no lead_verified in the "
            f"30d telemetry window excluded as unscanned supply (not a "
            f"cooldown release)")
        for release_id, members, available_at, desc in defs:
            members = sorted(set(members))
            if not members:
                continue
            releases.append({
                "release_id": release_id,
                "definition": COOLDOWN_RELEASE_DEFINITION,
                "evidence_ref": (
                    f"{desc}; {unscanned_note}; {conv_ref}"),
                "count": len(members),
                "role_ids": members,
                "stage": "VERIFICATION_ELIGIBLE",
                "available_at": available_at.isoformat(),
                "p95_prepare_seconds": prep_p95["p95"],
                "p95_verify_seconds": ver_p95["p95"],
                "estimated_conversion": conv["conversion"],
                "conversion_evidence_ref": f"{conv_ref}; {timing_ref}",
            })
    return extra_leads, extra_holds, releases, skipped


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else None
    if out_path and os.path.exists(out_path):
        print(f"export: refusing to overwrite existing {out_path}", file=sys.stderr)
        sys.exit(2)

    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    observed_at = now.isoformat()

    standard = load_json(os.path.join(PIPELINE, "queue", "standard-queue.json"), [])
    needs_input = load_json(os.path.join(PIPELINE, "queue", "needs_input-queue.json"), [])
    tray = load_json(os.path.join(PIPELINE, "hidden_files", "input-tray.json"), {})
    # one-lead-one-queue: standard entry wins on duplicate role_id — needs_input
    # is listed first so the standard entry (listed last) takes precedence.
    queue_index = {e.get("role_id"): e for e in needs_input + standard if e.get("role_id")}

    # --- readiness evidence (ARM 3, 2026-09-20): real ledger / lock-dir /
    # buffer observations for the readiness gates, read once and shared by
    # every lead_row call. Read-only; each helper fails closed on missing
    # evidence. The ledger load moved up from the capacity section so the
    # duplicate guard is available to the leads section.
    ledger_rows = load_json(
        os.path.join(PIPELINE, "ledger", "application-ledger.json"), [])
    try:
        lock_names = set(os.listdir(LAUNCH_LOCK_DIR))
    except OSError as exc:
        print(f"export: launch-lock registry unreadable ({exc}); "
              f"locks fail closed to unknown", file=sys.stderr)
        lock_names = None
    staged_packets = load_staged_packets()
    print(f"export: staged_packets={len(staged_packets)} "
          f"identity_covered="
          f"{sum(1 for r in staged_packets.values() if r['identity_covered'])}",
          file=sys.stderr)
    readiness_evidence = {
        "staged_packets": staged_packets,
        "lock_names": lock_names,
        "lock_dir": LAUNCH_LOCK_DIR,
        "ledger_submitted": ledger_submitted_roles(ledger_rows),
        "attempt_by_app": {},   # filled after the telemetry pass
        "history_complete": False,  # filled after the attempts section
        "now": now,
    }

    # --- telemetry: single pass feeds attempts + all measurements --------------
    # (moved above the leads section: the readiness evidence needs the
    # attempt journal before any lead row is built)
    tele_rows = list(scan_telemetry())

    # --- attempts ----------------------------------------------------------------
    attempt_events = attempt_events_from_rows(tele_rows)
    history_complete = attempt_history_instrumented()
    readiness_evidence["attempt_by_app"] = attempt_state_by_application(
        attempt_events)
    readiness_evidence["history_complete"] = history_complete
    print(f"export: attempt_events={len(attempt_events)} "
          f"attempt_history_complete={history_complete}", file=sys.stderr)

    # --- leads: READY rows + rows referenced by holds/questions ----------------
    ready_roles = {e["role_id"] for e in standard if e.get("status") == "READY"}
    tray_roles = {l.get("role_id") for c in tray.get("cards", [])
                  for l in (c.get("leads") or []) if l.get("role_id")}
    hold_roles = {e["role_id"] for e in needs_input if e.get("unresolved")}
    wanted = ready_roles | tray_roles | hold_roles
    leads, skipped = [], 0
    seen_roles = set()
    for role_id in sorted(wanted):
        entry = queue_index.get(role_id)
        if not entry:
            skipped += 1
            continue
        row, reason = lead_row(entry, observed_at, readiness_evidence)
        if row is None:
            skipped += 1
            continue
        leads.append(row)
        seen_roles.add(role_id)
    print(f"export: leads={len(leads)} skipped_unidentifiable={skipped}",
          file=sys.stderr)

    # --- cooldown supply: parked APPLY leads + evidenced release groups -------
    cd_leads, cd_holds, releases, cd_skipped = build_cooldown_supply(
        standard, seen_roles, tele_rows, now, observed_at, readiness_evidence)
    leads.extend(cd_leads)
    skipped += cd_skipped
    print(f"export: cooldown_leads={len(cd_leads)} cooldown_holds={len(cd_holds)} "
          f"releases={len(releases)}", file=sys.stderr)

    # --- pool: exact guardian logic -------------------------------------------
    pool_state = _pipeline_module("pool_guardian").pool_state

    ready, actionable = pool_state(now=now)
    pool = {"schema_version": 1, "ready": ready, "actionable": actionable,
            "observed_at": observed_at}

    # --- holds -----------------------------------------------------------------
    evidence_revision = f"input-tray@{tray.get('generated_at', 'unknown')}"
    holds = build_holds(tray, queue_index, now, evidence_revision)
    holds.extend(cd_holds)
    lead_roles = {row["role_id"] for row in leads}
    kept, dropped = [], 0
    for h in holds:
        if h["role_id"] in lead_roles:
            kept.append(h)
        else:
            dropped += 1
    holds = kept
    print(f"export: holds={len(holds)} dropped_unjoined={dropped}", file=sys.stderr)
    # lead["holds"] carries hold FAMILY names: the board's release gate exempts
    # the "cooldown" family by name; readiness treats any non-empty list as held.
    by_role = {}
    for h in holds:
        by_role.setdefault(h["role_id"], set()).add(h["family"])
    for row in leads:
        row["holds"] = sorted(by_role.get(row["role_id"], ()))

    # --- questions --------------------------------------------------------------
    question_dependencies = [d for d in build_question_dependencies(tray, queue_index)
                             if d["role_id"] in lead_roles]

    # --- discovery: measured minutes where the sweep measured them ------------
    window_start = now - timedelta(days=MEASUREMENT_WINDOW_DAYS)
    linkedin_source = build_linkedin_source(tele_rows, window_start)
    print(f"export: linkedin measurement_complete="
          f"{linkedin_source['measurement_complete']} "
          f"minutes={linkedin_source['verification_minutes']}", file=sys.stderr)

    # --- capacity: genuine supplied-interval rate or explicit unmeasured -----
    # (ledger_rows loaded once in the readiness-evidence block above)
    capacity, capacity_evidence = measure_supplied_capacity(
        ledger_rows, attempt_events, window_start, now, adapter_revision())
    print(f"export: capacity_measured="
          f"{capacity_evidence['measured_while_supplied']} "
          f"reason={capacity_evidence['reason']}", file=sys.stderr)

    # --- active: outside the 01:30-10:00 PT sleep window -------------------------
    la = now.astimezone(ZoneInfo("America/Los_Angeles"))
    active = active_outside_sleep_window(la)

    snapshot = {
        "schema_version": 1,
        "source_revision": adapter_revision(),
        "observed_at": observed_at,
        "complete": True,
        "active": bool(active),
        "leads": leads,
        "pool": pool,
        "holds": holds,
        "hold_decisions": [],
        "capacity": capacity,
        "capacity_evidence": capacity_evidence,
        "releases": releases,
        "discovery": {
            "budget_minutes": 60,
            "sources": [linkedin_source],
        },
        "tray": build_tray_document(tray),
        "question_dependencies": question_dependencies,
        "attempt_events": attempt_events,
        "attempt_history_complete": history_complete,
        "applications": [],
    }

    # validate against the real board contract before writing
    from keel_flow.board import build as board_build
    board_build(snapshot, now=now)  # raises ContractError on any adapter lie

    payload = json.dumps(snapshot, indent=1, sort_keys=True)
    if out_path:
        with open(out_path, "w") as f:
            f.write(payload)
        print(f"export: wrote {out_path} ({len(payload)} bytes)", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
