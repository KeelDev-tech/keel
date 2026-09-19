#!/usr/bin/env python3
"""Parked browser-task sweeper (DEPT 1, 2026-09-15 — never-halt discipline).

Incident: parked `needs_user` browser tasks held all Chromium slots ~176 min
on 2026-09-15 and starved the one live application task (the workspace operating-lessons doc:
"Parked tasks hold browser slots"). Per the never-halt directive, parked
tasks are CLOSED to free the lane — history is kept (the DB row and this
script's directive ledger are untouched by the close), forms rebuild from
the launch packet on revival, and the closure is recorded in the queue
entry's notes.

What this script does and does NOT do:
  - DETECTS parked tasks from a browser-task snapshot file
    (hidden_files/browser-task-snapshot.json). The snapshot is produced
    by the lane owner (main chat, which has the live browser surface) from
    subagent.list / your browser-task table; a cron shell script cannot
    reach the runtime DB, so the snapshot file is the handoff contract.
  - PROPOSES closures (default --dry-run: zero writes, zero live actions).
  - AUDIT-LOGS every action: proposals append to
    hidden_files/parked-task-closures.jsonl (append-only directive ledger)
    and emit one aggregate gate_encountered per run via the sanctioned
    log_event path (gate="unclassified_parked", details.action carries
    propose/confirm semantics — a dedicated "parked_slot_swept" gate value
    is proposed for registration by the log_event.py owner next cycle).
  - NEVER closes a browser task itself. The live close is a browser-control
    action owned by the main chat. After the lane owner closes the task,
    they run --confirm-closed <task_id>: the script then records the
    closure in the queue entry's notes (queue backup first, entries
    re-validated via queue_intake.validate_entry) and emits the
    gate_cleared event. That closes the audit loop.
  - PROPOSES closing a parked task whenever its lead is canonically
    parked (needs_input queue, PARKED* status) — REGARDLESS of whether
    the blocker is a genuine applicant-only item. Rationale (2026-09-17
    stall post-mortem): the HOLD is on the LEAD, not the browser
    session. A canonically parked lead's `needs_user` session holds a
    Chromium slot for nothing — the applicant's input, when it comes, revives
    the lead from the launch packet, never from the dead session.
    Confusing "preserve the parked lead" with "preserve its browser
    session" starved the lane for ~2h on 2026-09-17 (9 of 10 dead
    sessions were never even proposed because load_queue ignored
    needs_input). HOLD now applies ONLY when the matched lead is NOT
    canonically parked (closing would lose unrecorded blocker context —
    park it first, then close).

Modes:
    --dry-run            plan only; zero writes (default)
    --propose            append close directives to the ledger + gate event
    --confirm-closed <task_id> [--closed-by NAME]
                         bookkeeping after the lane owner closed the task live
    --repair-inflight [--live]
                         revert stale IN-FLIGHT markers to READY (dry-run
                         default; --live writes with queue backup). Fails
                         closed on a stale snapshot.
    --snapshot PATH      snapshot file (default hidden_files/browser-task-snapshot.json)

  - ZERO-STEP-STALL (2026-09-17 Monterrey incident): a needs_user session
    with an explicit step_count of 0 and no activity for 20+ minutes never
    started acting — steer fails with continuation-inconclusive, nothing
    was entered, and the slot is the only thing being held. The plan's
    zero_step_stall section proposes close + same-lead fresh spawn (never
    a park; parking would fabricate a blocker that was never read). The
    lead is matched by the entry's browser_task_id when the title is the
    generic "Browser task".

Exit codes: 0 ok; 2 snapshot missing/unreadable (fail loud, never silent).
"""
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA, HOME  # noqa: E402 — repo path convention
import log_event  # noqa: E402 — sanctioned telemetry path
try:
    import queue_intake  # noqa: E402 — entry validator (charter C-17)
except ImportError:  # pragma: no cover
    queue_intake = None
try:
    from genuine_pat import GENUINE_PAT, APPLICANT_NOTE_PAT  # noqa: E402
except ImportError:  # pragma: no cover — keep sweeper usable standalone
    APPLICANT_NOTE_PAT = re.compile(r"\(applicant:[^)]*\)", re.I)
    GENUINE_PAT = re.compile(r"essay|attest|\bapplicant\b", re.I)

QUEUE = os.path.join(DATA, "queues", "standard-queue.json")
NEEDS_INPUT_QUEUE = os.path.join(DATA, "queues", "needs_input-queue.json")
STRATEGIC_QUEUE = os.path.join(DATA, "queues", "strategic-queue.json")
SNAPSHOT = os.path.join(HOME, "hidden_files", "browser-task-snapshot.json")
LEDGER = os.path.join(HOME, "hidden_files", "parked-task-closures.jsonl")

PARKED_MINUTES = 30  # parked longer than this with no lane-owner touch
ZERO_STEP_STALL_MINUTES = 20  # needs_user with zero steps idle this long =
# dead session (2026-09-17: steer fails with continuation-inconclusive;
# the session never started acting, so the slot is the only cost)
PARKED_STATUSES = {"needs_user", "parked_outcome"}
PARKED_OUTCOMES = {"needs_user"}  # outcome_status values that mean "waiting"
GATE = "unclassified_parked"  # registered gate vocab (dedicated
# "parked_slot_swept" proposed for registration by log_event.py owner)


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_snapshot(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as ex:
        print(f"parked_task_sweep: cannot read snapshot {path}: {ex}",
              file=sys.stderr)
        sys.exit(2)
    return d if isinstance(d, list) else d.get("tasks", [])


def _load_queue_file(path, home):
    try:
        d = json.load(open(path))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    entries = d if isinstance(d, list) else d.get("entries", d.get("items", []))
    for e in entries:
        if isinstance(e, dict):
            e["_home"] = home  # which queue file owns this entry
    return entries


def load_queue():
    """All queue entries across standard, needs_input, and strategic.

    2026-09-17 post-mortem: the old version read ONLY standard-queue.json,
    so parked tasks whose leads lived in needs_input never matched and
    were never proposed — 9 of 10 dead sessions on 2026-09-17.
    """
    return (_load_queue_file(QUEUE, "standard")
            + _load_queue_file(NEEDS_INPUT_QUEUE, "needs_input")
            + _load_queue_file(STRATEGIC_QUEUE, "strategic"))


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def parse_task_title(title):
    """Split a browser task title into (title_part, company_part).

    Observed shapes in your browser-task table:
      "Job Application for <TITLE> at <COMPANY>"
      "<COMPANY> - <TITLE>"            (e.g. "Spreedly - Technical Program Manager")
      "<COMPANY> Careers | Apply for <TITLE>"
      "<TITLE>"                        (no company; company_part "")
    """
    t = str(title or "").strip()
    m = re.match(r"^Job Application for (.+?) at ([^|]+?)\s*$", t, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.+?)\s+Careers\s*\|\s*Apply for (.+?)\s*$", t, re.I)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(r"^([A-Z][A-Za-z0-9&'. ]{1,40}?)\s+-\s+(.+?)\s*$", t)
    if m and len(m.group(1).split()) <= 4:
        return m.group(2).strip(), m.group(1).strip()
    return t, ""


def match_role(task, entries):
    """Match a parked task to a queue role_id.

    Returns (role_id, confidence) with confidence in
    {"high", "medium", "none"}. high = parsed company+title both hit the
    same queue entry (or the entry is currently IN-FLIGHT); medium =
    strong token overlap; none = no safe match (fail closed: no proposal).
    """
    title_part, company_part = parse_task_title(task.get("title"))
    tt, ct = _tokens(title_part), _tokens(company_part)
    best, best_score = None, 0
    for e in entries:
        et, ec = _tokens(e.get("title")), _tokens(e.get("company"))
        if not et:
            continue
        score = 0
        if ct and ct & ec:
            score += 3
        overlap = tt & et
        score += len(overlap)
        # title tokens of the task should mostly be covered by the entry
        if tt and len(overlap) >= max(2, len(tt) - 1):
            score += 2
        if score > best_score:
            best, best_score = e, score
    if best is None:
        return "", "none"
    high = (company_part and bool(ct & _tokens(best.get("company")))
            and len(tt & _tokens(best.get("title"))) >= 2)
    return best.get("role_id", ""), ("high" if high else
                                    "medium" if best_score >= 3 else "none")


def _match_lead(task, entries):
    """Best queue-lead match for a browser task: title-token matching
    first, then the entry's pinned browser_task_id as an exact fallback.

    The fallback covers generic task titles ("Browser task") that token
    matching can never find — the 2026-09-17 Monterrey miss, where the
    zero-step needs_user session hid from the sweep for ~30 min.
    """
    role_id, conf = match_role(task, entries)
    if not conf.startswith("high"):
        role_id, conf = match_by_task_id(task.get("task_id"), entries)
    return role_id, conf


def match_by_task_id(task_id, entries):
    """Match a browser task to a queue role_id by the entry's pinned
    browser_task_id.

    2026-09-17: the Databricks Strategic Core AE session stalled with the
    generic title "Browser task" — title-token matching could never find
    the lead. The queue entry's browser_task_id is the exact, fail-closed
    match for that case.
    """
    for e in entries:
        if e.get("browser_task_id") == task_id and task_id:
            return e.get("role_id", ""), "high(task_id)"
    return "", "none"


def _step_count(task):
    """The session's action count, or None when the field is absent.

    Absent is NOT treated as zero: the zero-step rule must only fire on an
    explicit step_count of 0 (older snapshots lack the field).
    """
    try:
        return int(task.get("step_count"))
    except (TypeError, ValueError):
        return None


def blocker_text(entry):
    # Defensive: a few queue entries carry queue_notes as a list of strings
    # (writer-side type drift); flatten everything to str so the GENUINE_PAT
    # check never TypeErrors on well-meaning data. Behavior unchanged for
    # the conventional str fields.
    parts = [entry.get("status_reason"), entry.get("gate_note"),
             entry.get("queue_notes")]
    un = entry.get("unresolved") or []
    parts += un if isinstance(un, list) else [un]
    flat = []
    for p in parts:
        if p is None:
            continue
        if isinstance(p, list):
            flat.extend(str(x) for x in p if x is not None)
        elif isinstance(p, str):
            flat.append(p)
        else:
            flat.append(str(p))
    return " ".join(p for p in flat if p)


def is_applicant_item(entry):
    """True when the queue entry's blocker is a genuine applicant-only item.

    Same convention as verify_retry: strip internal (applicant: ...) fit
    annotations first (those are agent notes, not his words), then match
    GENUINE_PAT. A parked task waiting on HIS words/auth/decision is never
    auto-close proposed — his standing "skip all tasks needing me" rules.
    """
    text = APPLICANT_NOTE_PAT.sub(" ", blocker_text(entry))
    return bool(GENUINE_PAT.search(text))


def parked_candidates(tasks, now):
    """Live (non-terminal) parked tasks older than PARKED_MINUTES."""
    out = []
    for t in tasks:
        if t.get("status") not in PARKED_STATUSES:
            continue
        if t.get("completed_at"):
            continue  # terminal occurrence; not holding a live slot
        if t.get("status") == "parked_outcome" and \
                t.get("outcome_status") not in PARKED_OUTCOMES:
            continue
        upd = parse_ts(t.get("updated_at"))
        if upd is None:
            continue
        age_min = (now - upd).total_seconds() / 60
        if age_min >= PARKED_MINUTES:
            out.append((t, age_min))
    return sorted(out, key=lambda p: p[1], reverse=True)


def zero_step_candidates(tasks, now):
    """Live needs_user sessions with an explicit step_count of 0 idle for
    >= ZERO_STEP_STALL_MINUTES.

    These are sessions that never started acting — no form was opened, no
    field was touched. The 2026-09-17 Monterrey session sat in this state
    ~30 min: the steer failed with "browser continuation is temporarily
    inconclusive", nothing was entered, and the lane stalled while the
    queue lead stayed IN-FLIGHT on a dead task. A zero-step session is
    unrecoverable; the fix is close + same-lead respawn, never parking.
    """
    out = []
    for t in tasks:
        if t.get("status") not in PARKED_STATUSES:
            continue
        if t.get("completed_at"):
            continue  # terminal occurrence; not holding a live slot
        if t.get("status") == "parked_outcome" and \
                t.get("outcome_status") not in PARKED_OUTCOMES:
            continue
        if _step_count(t) != 0:
            continue
        upd = parse_ts(t.get("updated_at"))
        if upd is None:
            continue
        age_min = (now - upd).total_seconds() / 60
        if age_min >= ZERO_STEP_STALL_MINUTES:
            out.append((t, age_min))
    return sorted(out, key=lambda p: p[1], reverse=True)


def _is_canonically_parked(entry):
    """True when the queue — the system of record — has this lead parked.

    needs_input entries carry PARKED* statuses; a strategic/standard
    entry is parked only if its status says so. The queue state decides,
    never the blocker's genuineness.
    """
    st = str(entry.get("status") or "").upper()
    return st.startswith("PARKED")


def build_plan(tasks, entries, now):
    plan = {"ts": now.isoformat(), "parked_minutes_threshold": PARKED_MINUTES,
            "zero_step_stall_threshold": ZERO_STEP_STALL_MINUTES,
            "propose_close": [], "hold_applicant_item": [], "uncertain_match": [],
            "zero_step_stall": [], "hold_deliberate": []}
    # 2026-09-18: deliberate holds (lane-owner override) suppress close
    # proposals for their window — the hold is on the session, not just
    # the lead, and proposing against it manufactures false closures.
    held_ids = _active_hold_task_ids(now)
    for task, age_min in parked_candidates(tasks, now):
        role_id, conf = _match_lead(task, entries)
        item = {
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "outcome_status": task.get("outcome_status"),
            "parked_min": round(age_min, 1),
            "task_created_at": task.get("created_at"),
            "task_updated_at": task.get("updated_at"),
            "role_id": role_id,
            "match_confidence": conf,
        }
        entry = next((e for e in entries if e.get("role_id") == role_id),
                     None) if role_id else None
        # Confidence guard comes FIRST: a medium/low match must never
        # drive a close proposal or a hold attribution — wrong-role
        # history is worse than a held slot. Only high-confidence
        # matches (title-based "high" or exact task-id "high(task_id)")
        # reach the parked/unparked decision below.
        if entry is not None and conf.startswith("high") \
                and _is_canonically_parked(entry):
            # 2026-09-18: deliberate-hold override comes before the
            # parked-forever close rule. The lane owner explicitly
            # preserved this session (hold-deliberate ledger row with a
            # future hold_until); the sweeper must not propose, and the
            # lane owner must not confirm, a close for its window.
            if item["task_id"] in held_ids:
                item["queue_home"] = entry.get("_home")
                item["note"] = ("deliberate hold active: lane owner "
                                "preserved this session; close proposal "
                                "suppressed for the hold window")
                plan["hold_deliberate"].append(item)
                continue
            # 2026-09-17 doctrine: a parked lead's browser session is a
            # dead slot-holder. Propose the close REGARDLESS of blocker
            # genuineness — the HOLD is on the lead (already recorded),
            # never on the session. Revival rebuilds from the packet.
            item["queue_home"] = entry.get("_home")
            if is_applicant_item(entry):
                item["note"] = ("genuine applicant-only blocker, but lead is "
                                "canonically parked — session close is safe; "
                                "revival rebuilds from the launch packet")
            item["revival"] = ("forms rebuild from the launch packet on "
                               "revival; history preserved in this ledger "
                               "and the DB task row")
            plan["propose_close"].append(item)
        elif entry is not None and conf.startswith("high"):
            # Lead matched but NOT canonically parked: the queue does not
            # reflect the block. Closing now would lose unrecorded blocker
            # context — park the lead first (prescreen.park_lead), then the
            # next sweep proposes the close.
            item["hold_reason"] = ("matched lead is not canonically parked "
                                   f"(home={entry.get('_home')}, "
                                   f"status={entry.get('status')}); park it "
                                   "first via the canonical path, then close")
            item["blocker_text"] = blocker_text(entry)[:300]
            plan["hold_applicant_item"].append(item)
        elif conf != "high":
            # Medium/low-confidence matches are never auto-close proposed:
            # a wrong role match would annotate the wrong queue entry and
            # misattribute history. The lane owner judges these by hand.
            item["note"] = (f"match confidence {conf}; close needs the lane "
                            "owner's judgment")
            plan["uncertain_match"].append(item)
        else:
            item["revival"] = ("forms rebuild from the launch packet on "
                               "revival; history preserved in this ledger "
                               "and the DB task row")
            plan["propose_close"].append(item)
    _add_zero_step_stalls(plan, tasks, entries, now, held_ids=held_ids)
    return plan


def _add_zero_step_stalls(plan, tasks, entries, now, held_ids=None):
    """Dead-session detection (2026-09-17 Monterrey incident).

    A needs_user session with step_count 0 and no activity for
    ZERO_STEP_STALL_MINUTES never started acting — the steer fails with
    "browser continuation is temporarily inconclusive" and the slot is
    the only thing being held. Recovery:
      - matched IN-FLIGHT (or other non-parked) lead: close the session
        and reset the IN-FLIGHT marker to a SAME-LEAD fresh spawn. The
        lead is never parked: zero steps means no blocker context was
        ever read, so parking would fabricate a blocker.
      - matched canonically parked lead: the 30-min parked path already
        proposes the close; note the cross-reference instead of
        double-proposing.
      - no queue match: propose the close for the slot (zero steps means
        no form state exists to lose); the lane owner judges the lead.
    Matching is title-first, then by the entry's pinned browser_task_id
    (exact) — a generic "Browser task" title must never hide a lead.

    2026-09-18: tasks under an active deliberate hold are skipped — the
    lane owner explicitly preserved the session, so no close/respawn
    proposal may be manufactured against it.
    """
    proposed_ids = {i.get("task_id") for i in plan["propose_close"]}
    held_ids = set(held_ids or ())
    for task, age_min in zero_step_candidates(tasks, now):
        if task.get("task_id") in held_ids:
            continue
        role_id, conf = _match_lead(task, entries)
        entry = next((e for e in entries if e.get("role_id") == role_id),
                     None) if role_id else None
        item = {
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "outcome_status": task.get("outcome_status"),
            "parked_min": round(age_min, 1),
            "step_count": 0,
            "task_created_at": task.get("created_at"),
            "task_updated_at": task.get("updated_at"),
            "role_id": role_id,
            "match_confidence": conf,
            "directive": False,  # propose() only writes directive=True items
        }
        if entry is not None and _is_canonically_parked(entry):
            if task.get("task_id") in proposed_ids:
                item["note"] = ("already covered by a close proposal in "
                                "propose_close (30-min parked path)")
            else:
                item["note"] = ("lead is canonically parked; the parked "
                                "path proposes the close once it ages out "
                                "of 30m — close is safe now if the lane "
                                "owner prefers")
                item["directive"] = True
                item["recovery"] = ("close session; lead stays parked; "
                                    "revival rebuilds from the launch "
                                    "packet")
            item["queue_home"] = entry.get("_home")
        elif entry is not None:
            item["queue_home"] = entry.get("_home")
            item["recovery"] = ("close session; reset the IN-FLIGHT marker "
                                "to a SAME-LEAD fresh spawn (new browser "
                                "task). Do NOT park the lead: zero steps "
                                "means no blocker context was ever read.")
            item["directive"] = True
        else:
            item["note"] = ("no queue match (title may be generic); zero "
                            "steps means no form state exists to lose")
            item["recovery"] = ("close session to free the slot; lane "
                                "owner judges whether to respawn the lead")
            item["directive"] = True
        plan["zero_step_stall"].append(item)


def _active_hold_task_ids(now):
    """Task ids under an unexpired deliberate hold (2026-09-18).

    The lane owner (main chat) writes action="hold-deliberate" rows with a
    hold_until timestamp when a session must be preserved despite matching
    the parked-forever close rule (e.g. a fully-completed form awaiting
    the applicant's one-click submit). --propose must skip these task_ids: a hold
    is an explicit lane-owner override, and proposing/confirming a close
    against it manufactures false closure records. A missing or
    unparseable hold_until fails CLOSED toward preservation — the cost of
    wrongly closing a deliberately-held session exceeds one more 15-minute
    sweep cycle with the slot held.
    """
    held = set()
    try:
        with open(LEDGER) as f:
            rows = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return held
    for raw in rows:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if d.get("action") != "hold-deliberate":
            continue
        tid = d.get("task_id")
        if not tid:
            continue
        until = parse_ts(d.get("hold_until"))
        if until is None or until > now:
            held.add(tid)
    return held


def _open_directive_task_ids():
    """Task ids with a live (proposed, not confirmed) close directive.

    Reconstructs ledger state: a close-proposed row is open unless a
    close-confirmed row for the same task_id was appended later. Without
    this the sweep re-proposes the same dead task every run (OpenTable was
    proposed nine times with zero executions on 2026-09-16).
    """
    open_ids = set()
    try:
        with open(LEDGER) as f:
            rows = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return open_ids
    for raw in rows:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        tid = d.get("task_id")
        if not tid:
            continue
        if d.get("action", "").endswith("-confirmed"):
            open_ids.discard(tid)
        elif d.get("action", "").endswith("-proposed") and not d.get("executed_by"):
            open_ids.add(tid)
    return open_ids


def propose(plan, dry_run):
    """Append close directives to the audit ledger + one aggregate gate
    event. dry_run=True: zero writes (plan is only printed by the caller).

    2026-09-17: one active directive per task — candidates that already
    have an open (unconfirmed) directive are skipped, so repeated sweeps
    do not stack duplicate proposals for the same dead session.
    Zero-step-stall items get action "close-respawn-proposed" so the lane
    owner sees the recovery is close + same-lead fresh spawn, not a park.
    """
    respawn = [i for i in plan.get("zero_step_stall", [])
               if i.get("directive")]
    if dry_run or not (plan["propose_close"] or respawn):
        return 0
    already_open = _open_directive_task_ids()
    fresh = [(i, "close-proposed") for i in plan["propose_close"]
             if i.get("task_id") not in already_open]
    fresh += [(i, "close-respawn-proposed") for i in respawn
              if i.get("task_id") not in already_open]
    skipped = (len(plan["propose_close"]) + len(respawn)) - len(fresh)
    if not fresh:
        if skipped:
            # M2 (dev-support-deep-sweep run 127): name the skipped leads in
            # details.role_ids so the run-level dedup event is diagnosable
            # lead-level; the central log_event promotion attributes it at
            # the top level when they all share one role_id.
            skipped_items = (
                [i for i in plan["propose_close"]
                 if i.get("task_id") in already_open]
                + [i for i in respawn
                   if i.get("task_id") in already_open])
            log_event.log(
                "gate_encountered", role_id="", company="",
                source="parked_task_sweep",
                details={"gate": GATE, "action": "close-proposed-deduped",
                         "count": 0, "skipped": skipped,
                         "role_ids": sorted({i.get("role_id")
                                             for i in skipped_items
                                             if i.get("role_id")}),
                         "reason": (f"{skipped} candidate(s) already have an "
                                    "open close directive; no duplicate "
                                    "proposals written")})
        return 0
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    n = 0
    with open(LEDGER, "a") as f:
        for item, action in fresh:
            directive = dict(item)
            directive.update({
                "action": action,
                "proposed_at": plan["ts"],
                "policy": ("never-halt: free the Chromium slot; history "
                           "kept; revival rebuilds from launch packet"),
                "executed_by": None,  # filled by --confirm-closed
            })
            f.write(json.dumps(directive) + "\n")
            n += 1
    log_event.log(
        "gate_encountered", role_id="", company="", source="parked_task_sweep",
        details={"gate": GATE,
                 "action": "close-proposed",
                 "count": n,
                 "deduped_skipped": skipped,
                 "task_ids": [i["task_id"] for i, _ in fresh],
                 "role_ids": [i["role_id"] for i, _ in fresh],
                 "actions": [a for _, a in fresh],
                 "reason": (f"{n} parked browser task(s) older than "
                            f"{PARKED_MINUTES}m (or zero-step stalls older "
                            f"than {ZERO_STEP_STALL_MINUTES}m) holding lane "
                            "slots; close directives appended to "
                            "parked-task-closures.jsonl for the lane owner "
                            "to execute")})
    return n


def _backup_queues():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(DATA, "queues",
                        f"_backup-{ts}-parked-sweep")
    os.makedirs(dest, exist_ok=True)
    saved = {}
    for name, path in (("standard-queue.json", QUEUE),
                       ("needs_input-queue.json", NEEDS_INPUT_QUEUE),
                       ("strategic-queue.json", STRATEGIC_QUEUE)):
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        open(os.path.join(dest, name), "wb").write(data)
        saved[name] = path
    # P5 §3.1 (2026-09-18): post-20260918 backup dirs carry a manifest.json
    # (ts, reason, sha256 per file) so a restore routine can list and
    # hash-verify them. Manifest failure must never break the backup.
    try:
        _write_manifest(dest, "parked-sweep queue backup")
    except Exception as ex:
        print(f"  warn: queue backup manifest failed ({ex})",
              file=sys.stderr)
    return dest, saved


def _write_manifest(backup_dir, reason):
    """Write manifest.json into a backup dir: ts, reason, sha256 per file.

    Local so this module needs no uncommitted queue_io additions; the
    manifest itself is written atomically via queue_io.atomic_write_json.
    """
    import queue_io  # noqa: E402 — lazy: only needed for manifests
    files = {}
    for name in sorted(os.listdir(backup_dir)):
        if name == "manifest.json":
            continue
        fpath = os.path.join(backup_dir, name)
        if os.path.isfile(fpath):
            h = hashlib.sha256()
            with open(fpath, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            files[name] = {"sha256": h.hexdigest(),
                           "bytes": os.path.getsize(fpath)}
    queue_io.atomic_write_json(
        os.path.join(backup_dir, "manifest.json"),
        {"ts": datetime.now(timezone.utc).isoformat(),
         "reason": str(reason or ""),
         "created_by": "parked_task_sweep._write_manifest",
         "files": files})


def _backup_queue():
    # Legacy single-queue backup kept for callers; prefer _backup_queues.
    dest, _ = _backup_queues()
    return dest


def confirm_closed(task_id, closed_by, now):
    """Bookkeeping after the lane owner closed the task live.

    Marks the directive executed, records the closure in the matched queue
    entry's notes (queue backup first, entry re-validated), emits
    gate_cleared. Returns the updated directive dict.
    """
    directives = []
    try:
        with open(LEDGER) as f:
            for line in f:
                line = line.strip()
                if line:
                    directives.append(json.loads(line))
    except FileNotFoundError:
        print("parked_task_sweep: no directive ledger; nothing to confirm",
              file=sys.stderr)
        sys.exit(2)
    # 2026-09-17: the old filter ("task_id matches and no executed_by")
    # matched the ORIGINAL close-proposed row on a second
    # --confirm-closed: the ledger is append-only, so the in-memory
    # directive["executed_by"] mutation never persisted, and even the
    # close-confirmed row itself (no executed_by key) matched. A task is
    # open for confirmation only while it has a close-proposed row with no
    # later close-confirmed row.
    confirmed = {d.get("task_id") for d in directives
                 if d.get("action", "").endswith("-confirmed")}
    matches = [d for d in directives
               if d.get("action", "").endswith("-proposed")
               and d.get("task_id") == task_id
               and d.get("task_id") not in confirmed]
    if not matches:
        print(f"parked_task_sweep: no open directive for {task_id}",
              file=sys.stderr)
        sys.exit(2)
    directive = matches[-1]
    role_id = directive.get("role_id")
    home = directive.get("queue_home") or "standard"
    home_path = {"standard": QUEUE, "needs_input": NEEDS_INPUT_QUEUE,
                 "strategic": STRATEGIC_QUEUE}.get(home, QUEUE)

    backup_dir, _ = _backup_queues()
    note = (f"[parked-sweep {now.strftime('%Y-%m-%d %H:%M PDT')}] browser task "
            f"{task_id} closed by {closed_by} to free the lane slot "
            f"(parked {directive.get('parked_min')}m, "
            f"{directive.get('status')}/{directive.get('outcome_status')}). "
            f"History preserved in parked-task-closures.jsonl; revive by "
            f"rebuilding the form from the launch packet.")
    # 2026-09-15: queue read-modify-write under queue_io's flock. The old
    # no-lock whole-file replace reverted concurrent lane writes (17:44
    # incident class); the direct open(QUEUE,"w") restore path could tear
    # the file for concurrent readers.
    # 2026-09-17: annotate the entry in its HOME queue — the old code
    # only ever wrote standard-queue.json, so closures for needs_input
    # leads were silently dropped.
    import queue_io  # noqa: E402
    with queue_io.queue_lock():
        raw = queue_io.load_json(home_path)
        entries = raw if isinstance(raw, list) else raw.get("entries",
                                                            raw.get("items", []))
        _sweep_entry = next((e for e in entries
                             if e.get("role_id") == role_id), None)
        _sweep_note_ok = True
        if _sweep_entry is not None:
            prev = _sweep_entry.get("queue_notes") or ""
            # queue_notes may be a list (several entries use it as such) —
            # appending to a list keeps the type; strings get a newline join.
            if isinstance(prev, list):
                _sweep_entry["queue_notes"] = prev + [note]
            else:
                _sweep_entry["queue_notes"] = (prev + "\n" + note).strip()
            if queue_intake is not None:
                errs, _warns = queue_intake.validate_entry(_sweep_entry)
                if errs:
                    print(f"parked_task_sweep: entry failed validation after "
                          f"note: {errs}; restoring backup", file=sys.stderr)
                    queue_io.atomic_write_json(home_path, json.load(open(os.path.join(
                        backup_dir, os.path.basename(home_path)))))
                    _sweep_note_ok = False
        if _sweep_note_ok:
            queue_io.atomic_write_json(home_path, raw)
    if not _sweep_note_ok:
        sys.exit(2)

    directive["executed_by"] = closed_by
    directive["closed_at"] = now.isoformat()
    directive["queue_backup"] = backup_dir
    # The confirmation row mirrors the proposal's action family so the
    # dedup reconstruction (_open_directive_task_ids) retires the right
    # directive kind (close-confirmed vs close-respawn-confirmed).
    confirmed_action = directive.get("action", "close-proposed").replace(
        "-proposed", "-confirmed")
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"action": confirmed_action,
                            "task_id": task_id, "role_id": role_id,
                            "closed_by": closed_by,
                            "closed_at": now.isoformat()}) + "\n")
    log_event.log(
        "gate_cleared", role_id=role_id or "", company="",
        source="parked_task_sweep",
        details={"gate": GATE, "action": "slot-freed",
                 "task_id": task_id, "closed_by": closed_by,
                 "reason": "parked browser task closed; lane slot freed"})
    return directive


INFLIGHT_STALE_MINUTES = 45  # IN-FLIGHT older than this with no lock/task
LOCK_DIR = os.path.join(HOME, "hidden_files", "launch-locks")
SNAPSHOT_FRESH_MINUTES = 20  # repair refuses a stale snapshot (fail closed)
LIVE_TASK_STATUSES = {"queued", "running", "needs_user", "parked_outcome"}


def _fresh_lock(role_id, now):
    """True when a live launch lock exists for the role (not expired).

    Filename convention mirrors launch_lock._lock_path: sha256 hex of the
    complete role_id (K19 2026-09-17 — the old sanitized-name mirror was
    retired with the K19 drain; the mismatch guard below stays).
    """
    if not isinstance(role_id, str) or not role_id:
        return False
    path = os.path.join(
        LOCK_DIR, hashlib.sha256(role_id.encode("utf-8")).hexdigest() + ".json")
    try:
        d = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return False
    if str(d.get("role_id") or "") != str(role_id or ""):
        return False  # embedded-role guard (was: sanitization collision guard)
    ttl_h = d.get("ttl_hours", 2)
    acquired = parse_ts(d.get("acquired_at"))
    if acquired is None:
        return False
    return (now - acquired) < timedelta(hours=ttl_h)


def _snapshot_fresh_enough(path, now):
    try:
        d = json.load(open(path))
        dumped = parse_ts(d.get("dumped_at"))
    except (OSError, json.JSONDecodeError):
        return False
    return dumped is not None and (now - dumped) < timedelta(
        minutes=SNAPSHOT_FRESH_MINUTES)


def repair_inflight(tasks, now, live):
    """Revert stale IN-FLIGHT markers to READY.

    2026-09-17 post-mortem: OpenTable sat IN-FLIGHT ~2.5h with no live
    browser task and no held lock — the queue claimed an application in
    flight that did not exist. A marker is stale when ALL hold:
      - status IN-FLIGHT older than INFLIGHT_STALE_MINUTES
      - no fresh launch lock for the role
      - no live (non-terminal) browser task matched to the role
    Fail-closed guards: repair SKIPS everything when the snapshot is
    stale/missing (a task invisible to us is not a task that is gone),
    and --live is required to write (default is dry-run). Queue backup
    first; one gate event per run.
    """
    entries = load_queue()  # 2026-09-17: all three queues — the old
    # read of QUEUE (standard) only meant a stale IN-FLIGHT on a
    # needs_input/strategic lead was never repaired.
    plan = {"ts": now.isoformat(), "repaired": [], "skipped_live": [],
            "skipped_completed_task": [], "skipped_birth_proximity": [],
            "skipped_stale_snapshot": False}
    if not _snapshot_fresh_enough(SNAPSHOT, now):
        plan["skipped_stale_snapshot"] = True
        return plan
    live_by_role = {}
    completed_by_role = {}
    task_births = []  # (created_at epoch) for every snapshot task
    for t in tasks:
        ts = t.get("created_at")
        birth = None
        if ts is not None:
            try:
                birth = float(ts)
            except (TypeError, ValueError):
                dt = parse_ts(ts)
                birth = dt.timestamp() if dt else None
        if birth:
            task_births.append(birth)
        if t.get("status") not in LIVE_TASK_STATUSES or t.get("completed_at"):
            if str(t.get("status") or "").lower() == "completed":
                role_id, conf = match_role(t, entries)
                if conf == "high" and role_id:
                    completed_by_role.setdefault(role_id, []).append(
                        t.get("task_id"))
            continue
        role_id, conf = match_role(t, entries)
        if conf == "high" and role_id:
            live_by_role.setdefault(role_id, []).append(t.get("task_id"))
    for e in entries:
        if str(e.get("status") or "").upper() != "IN-FLIGHT":
            continue
        role_id = e.get("role_id")
        anchor = (parse_ts(e.get("in_flight_at"))
                  or parse_ts(e.get("status_updated")))
        age_min = ((now - anchor).total_seconds() / 60) if anchor else None
        if age_min is None or age_min < INFLIGHT_STALE_MINUTES:
            continue
        if _fresh_lock(role_id, now):
            continue
        if role_id in live_by_role:
            plan["skipped_live"].append(
                {"role_id": role_id, "tasks": live_by_role[role_id]})
            continue
        if role_id in completed_by_role:
            # The browser task ran to completion — it may be a submission
            # awaiting canonical reconciliation. Reverting to READY here
            # risks a duplicate application; reconciliation owns it.
            plan["skipped_completed_task"].append(
                {"role_id": role_id, "tasks": completed_by_role[role_id],
                 "reason": "browser task completed; needs canonical "
                           "reconciliation, not auto-revert"})
            continue
        if anchor is not None:
            # Birth-proximity fallback: the browser task for a launch is
            # created within seconds of the IN-FLIGHT marker. If ANY
            # snapshot task was born near the marker, the application
            # materialized (its title may be generic — "Browser task" —
            # when it never started acting). Only a marker with no task
            # anywhere near it is a phantom launch.
            a = anchor.timestamp()
            near = [b for b in task_births if a - 300 <= b <= a + 2700]
            if near:
                plan["skipped_birth_proximity"].append(
                    {"role_id": role_id,
                     "reason": "a browser task was created near the "
                               "IN-FLIGHT marker; not a phantom launch"})
                continue
        item = {"role_id": role_id,
                "company": e.get("company"), "title": e.get("title"),
                "queue_home": e.get("_home", "standard"),
                "inflight_age_min": round(age_min, 1)}
        if not live:
            plan["repaired"].append(dict(item, dry_run=True))
            continue
        plan["repaired"].append(item)
    if live and plan["repaired"]:
        backup_dir, _ = _backup_queues()
        import queue_io  # noqa: E402
        home_paths = {"standard": QUEUE, "needs_input": NEEDS_INPUT_QUEUE,
                      "strategic": STRATEGIC_QUEUE}
        with queue_io.queue_lock():
            # Repair to each entry's HOME queue (2026-09-17: writing only
            # QUEUE silently dropped repairs for needs_input/strategic).
            by_home = {}
            for item in plan["repaired"]:
                by_home.setdefault(item.get("queue_home") or "standard",
                                   []).append(item)
            repaired_all = []
            for home, items in by_home.items():
                home_path = home_paths.get(home, QUEUE)
                raw = queue_io.load_json(home_path)
                qentries = (raw if isinstance(raw, list)
                            else raw.get("entries", raw.get("items", [])))
                for item in items:
                    e = next((x for x in qentries
                              if x.get("role_id") == item["role_id"]), None)
                    if e is None:
                        continue
                    e["status"] = "READY"
                    e["status_reason"] = (
                        "stale IN-FLIGHT marker auto-repaired by parked_task_sweep "
                        f"{now.strftime('%Y-%m-%d %H:%M PDT')}: no fresh launch "
                        "lock and no live browser task; re-queued for launch")
                    prev = e.get("queue_notes") or ""
                    e["queue_notes"] = (
                        prev + "\n[parked-sweep] stale IN-FLIGHT reverted to "
                        "READY (no lock, no live task)").strip()
                    if queue_intake is not None:
                        errs, _warns = queue_intake.validate_entry(e)
                        if errs:
                            print(f"parked_task_sweep: entry failed validation "
                                  f"after repair: {errs}", file=sys.stderr)
                            queue_io.atomic_write_json(
                                home_path, json.load(open(os.path.join(
                                    backup_dir, os.path.basename(home_path)))))
                            plan["repaired"] = []
                            return plan
                    repaired_all.append(item)
                queue_io.atomic_write_json(home_path, raw)
            plan["repaired"] = repaired_all
        plan["queue_backup"] = backup_dir
        log_event.log(
            "gate_cleared", role_id="", company="",
            source="parked_task_sweep",
            details={"gate": GATE, "action": "stale-inflight-repaired",
                     "count": len(plan["repaired"]),
                     "role_ids": [i["role_id"] for i in plan["repaired"]],
                     "reason": "IN-FLIGHT markers with no lock and no live "
                               "task reverted to READY"})
    return plan


def main(argv):
    now = datetime.now(timezone.utc)
    snapshot = SNAPSHOT
    dry_run = True
    confirm_id = None
    closed_by = "lane-owner"
    repair = False
    i = 0
    while i < len(argv):
        if argv[i] == "--snapshot" and i + 1 < len(argv):
            snapshot = argv[i + 1]; i += 2
        elif argv[i] == "--propose":
            dry_run = False; i += 1
        elif argv[i] == "--dry-run":
            dry_run = True; i += 1
        elif argv[i] == "--confirm-closed" and i + 1 < len(argv):
            confirm_id = argv[i + 1]; i += 2
        elif argv[i] == "--closed-by" and i + 1 < len(argv):
            closed_by = argv[i + 1]; i += 2
        elif argv[i] == "--repair-inflight":
            repair = True; i += 1
        elif argv[i] == "--live":
            dry_run = False; i += 1
        else:
            i += 1
    if confirm_id:
        # confirm-closed is explicit post-live-close bookkeeping run by the
        # lane owner; it always executes (never part of a dry run).
        d = confirm_closed(confirm_id, closed_by, now)
        print(json.dumps({"confirmed": d["task_id"],
                          "role_id": d.get("role_id"),
                          "closed_by": closed_by}, indent=1))
        return
    tasks = load_snapshot(snapshot)
    if repair:
        plan = repair_inflight(tasks, now, live=not dry_run)
        print(json.dumps(plan, indent=1))
        if dry_run:
            print("\nDRY RUN: zero writes (no queue, no telemetry).")
        else:
            print(f"\nrepaired {len(plan['repaired'])} stale IN-FLIGHT "
                  f"marker(s)")
        return
    entries = load_queue()
    plan = build_plan(tasks, entries, now)
    print(json.dumps(plan, indent=1))
    if dry_run:
        print("\nDRY RUN: zero writes (no ledger, no telemetry, no queue).")
    else:
        n = propose(plan, dry_run=False)
        print(f"\nproposed {n} close directive(s); ledger: {LEDGER}")


if __name__ == "__main__":
    main(sys.argv[1:])
