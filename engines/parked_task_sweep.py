#!/usr/bin/env python3
"""Parked browser-task sweeper (never-halt discipline).

Incident encoded: parked `needs_user` browser tasks once held every browser
slot for ~3h and starved the one live application task. Per the never-halt
directive, parked tasks are CLOSED to free the lane — history is kept (the
directive ledger below is untouched by the close), forms rebuild from the
launch packet on revival, and the closure is recorded in the queue entry's
notes.

What this script does and does NOT do:
  - DETECTS parked tasks from a browser-task snapshot file
    (data/.state/browser-task-snapshot.json). The snapshot is produced by
    the lane owner (whoever holds the live browser surface) — a cron shell
    script cannot reach the runtime task store, so the snapshot file is the
    handoff contract.
  - PROPOSES closures (default --dry-run: zero writes, zero live actions).
  - AUDIT-LOGS every action: proposals append to
    data/.state/parked-task-closures.jsonl (append-only directive ledger)
    and emit one aggregate gate_encountered per run via the sanctioned
    log_event path (gate="unclassified_parked", details.action carries
    propose/confirm semantics).
  - NEVER closes a browser task itself. The live close is a browser-control
    action owned by the lane owner. After the lane owner closes the task,
    they run --confirm-closed <task_id>: the script then records the
    closure in the queue entry's notes (queue backup first) and emits the
    gate_cleared event. That closes the audit loop.
  - NEVER proposes closing an applicant-only item. If the matched queue
    entry's blocker text matches a genuine-input pattern (the applicant's
    own words, attestations, auth, decisions), the task is listed as HOLD —
    it stays parked; the applicant answers in the input tray at their own
    pace.

Modes:
    --dry-run            plan only; zero writes (default)
    --propose            append close directives to the ledger + gate event
    --confirm-closed <task_id> [--closed-by NAME]
                         bookkeeping after the lane owner closed the task live
    --snapshot PATH      snapshot file (default data/.state/browser-task-snapshot.json)

Exit codes: 0 ok; 2 snapshot missing/unreadable (fail loud, never silent).
"""
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA  # noqa: E402 — repo path convention
import log_event  # noqa: E402 — sanctioned telemetry path
try:
    import queue_intake  # noqa: E402 — entry validator
except ImportError:  # pragma: no cover
    queue_intake = None
try:
    from verify_retry import GENUINE_PAT, TRENT_NOTE_PAT  # noqa: E402
except ImportError:  # pragma: no cover — repo edition has no genuine-input
    # detector; use a conservative self-contained one. This errs toward
    # HOLD: the sweeper's invariant is "never auto-close an applicant-only
    # item" — an over-HOLD costs nothing, a wrong close misattributes
    # history and frees a slot that is genuinely waiting on the applicant.
    TRENT_NOTE_PAT = re.compile(r"\(applicant:[^)]*\)", re.I)
    GENUINE_PAT = re.compile(
        r"essay|cover.?letter|attest\w*|needs?.{0,25}applicant.{0,25}"
        r"(word|input|decision|answer|judg?ment)|applicant.{0,25}"
        r"(must|decision|judg?ment|call)|recording.{0,20}consent|"
        r"\bno.?ai\b|personally.{0,20}completed|unaided|"
        r"(referral|reference).{0,20}(name|contact|permission)",
        re.I)

QUEUE = os.path.join(DATA, "queues", "standard-queue.json")
SNAPSHOT = os.path.join(DATA, ".state", "browser-task-snapshot.json")
LEDGER = os.path.join(DATA, ".state", "parked-task-closures.jsonl")

PARKED_MINUTES = 30  # parked longer than this with no lane-owner touch
PARKED_STATUSES = {"needs_user", "parked_outcome"}
PARKED_OUTCOMES = {"needs_user"}  # outcome_status values that mean "waiting"
GATE = "unclassified_parked"


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


def load_queue():
    d = json.load(open(QUEUE))
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def parse_task_title(title):
    """Split a browser task title into (title_part, company_part).

    Observed title shapes:
      "Job Application for <TITLE> at <COMPANY>"
      "<COMPANY> - <TITLE>"
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


def blocker_text(entry):
    # Defensive: a few queue entries carry queue_notes as a list of strings
    # (writer-side type drift); flatten everything to str so the
    # genuine-input check never TypeErrors on well-meaning data. Behavior
    # unchanged for the conventional str fields.
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

    Strips internal (applicant: ...) annotations first (those are agent
    notes, not the applicant's words), then matches the genuine-input
    pattern. A parked task waiting on the applicant's words/auth/decision
    is never auto-close proposed.
    """
    text = TRENT_NOTE_PAT.sub(" ", blocker_text(entry))
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


def build_plan(tasks, entries, now):
    plan = {"ts": now.isoformat(), "parked_minutes_threshold": PARKED_MINUTES,
            "propose_close": [], "hold_applicant_item": [], "uncertain_match": []}
    for task, age_min in parked_candidates(tasks, now):
        role_id, conf = match_role(task, entries)
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
        if entry is not None and is_applicant_item(entry):
            item["hold_reason"] = ("genuine applicant-only blocker per "
                                   "genuine-input pattern; never auto-close")
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
                               "and the task snapshot")
            plan["propose_close"].append(item)
    return plan


def propose(plan, dry_run):
    """Append close directives to the audit ledger + one aggregate gate
    event. dry_run=True: zero writes (plan is only printed by the caller).
    """
    if dry_run or not plan["propose_close"]:
        return 0
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    n = 0
    with open(LEDGER, "a") as f:
        for item in plan["propose_close"]:
            directive = dict(item)
            directive.update({
                "action": "close-proposed",
                "proposed_at": plan["ts"],
                "policy": ("never-halt: free the browser slot; history "
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
                 "task_ids": [i["task_id"] for i in plan["propose_close"]],
                 "role_ids": [i["role_id"] for i in plan["propose_close"]],
                 "reason": (f"{n} parked browser task(s) older than "
                            f"{PARKED_MINUTES}m holding lane slots; close "
                            "directives appended to parked-task-closures.jsonl "
                            "for the lane owner to execute")})
    return n


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _backup_queue():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(DATA, "queues", f"_backup-{ts}-parked-sweep")
    os.makedirs(dest, exist_ok=True)
    data = open(QUEUE, "rb").read()
    open(os.path.join(dest, "standard-queue.json"), "wb").write(data)
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
    matches = [d for d in directives
               if d.get("task_id") == task_id and not d.get("executed_by")]
    if not matches:
        print(f"parked_task_sweep: no open directive for {task_id}",
              file=sys.stderr)
        sys.exit(2)
    directive = matches[-1]
    role_id = directive.get("role_id")

    backup_dir = _backup_queue()
    note = (f"[parked-sweep {now.strftime('%Y-%m-%d %H:%M %Z')}] browser task "
            f"{task_id} closed by {closed_by} to free the lane slot "
            f"(parked {directive.get('parked_min')}m, "
            f"{directive.get('status')}/{directive.get('outcome_status')}). "
            f"History preserved in parked-task-closures.jsonl; revive by "
            f"rebuilding the form from the launch packet.")
    _sweep_note_ok = True
    try:
        import queue_io  # noqa — atomic queue writes with flock, when present
        lock, load_json, atomic_write = (queue_io.queue_lock(),
                                        queue_io.load_json,
                                        queue_io.atomic_write_json)
    except Exception:
        lock, load_json, atomic_write = None, None, None
    if lock is not None:
        # Read-modify-write under queue_io's flock: the no-lock whole-file
        # replace can revert concurrent lane writes and tear the file for
        # concurrent readers.
        with lock:
            raw = load_json(QUEUE)
            entries = raw if isinstance(raw, list) else raw.get(
                "entries", raw.get("items", []))
            _sweep_entry = next((e for e in entries
                                 if e.get("role_id") == role_id), None)
            if _sweep_entry is not None:
                prev = _sweep_entry.get("queue_notes") or ""
                _sweep_entry["queue_notes"] = (prev + "\n" + note).strip()
                if queue_intake is not None:
                    errs = queue_intake.validate_entry(_sweep_entry)
                    if errs:
                        print(f"parked_task_sweep: entry failed validation "
                              f"after note: {errs}; restoring backup",
                              file=sys.stderr)
                        atomic_write(QUEUE, json.load(open(os.path.join(
                            backup_dir, "standard-queue.json"))))
                        _sweep_note_ok = False
            if _sweep_note_ok:
                atomic_write(QUEUE, raw)
    else:
        raw = json.load(open(QUEUE))
        entries = raw if isinstance(raw, list) else raw.get(
            "entries", raw.get("items", []))
        _sweep_entry = next((e for e in entries
                             if e.get("role_id") == role_id), None)
        if _sweep_entry is not None:
            prev = _sweep_entry.get("queue_notes") or ""
            _sweep_entry["queue_notes"] = (prev + "\n" + note).strip()
            if queue_intake is not None:
                errs = queue_intake.validate_entry(_sweep_entry)
                if errs:
                    print(f"parked_task_sweep: entry failed validation "
                          f"after note: {errs}; restoring backup",
                          file=sys.stderr)
                    raw = json.load(open(os.path.join(
                        backup_dir, "standard-queue.json")))
                    _sweep_note_ok = False
        if _sweep_note_ok:
            _atomic_write_json(QUEUE, raw)
    if not _sweep_note_ok:
        sys.exit(2)

    directive["executed_by"] = closed_by
    directive["closed_at"] = now.isoformat()
    directive["queue_backup"] = backup_dir
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"action": "close-confirmed",
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


def main(argv):
    now = datetime.now(timezone.utc)
    snapshot = SNAPSHOT
    dry_run = True
    confirm_id = None
    closed_by = "lane-owner"
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
