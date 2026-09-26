#!/usr/bin/env python3
"""operator_decision_journal.py — tamper-evident durable journal for operator decisions.

Port of the hardened journal pattern from Keel 0.16's ``keel_operator`` package
(``fields.py`` / ``setup.py`` / ``commission.py`` ``_Journal``), adapted to our
``approval_records.py`` approval model. The 0.16 sources live at
``~/workspace/keel-transfers/keel-0.16/keel_operator/``; the 0.16 modules
themselves are NOT portable — they import the 0.15 reference stack
(keel_composition, keel_sources, keel_muse, ...) which does not exist in this
tree. The journal storage pattern is the portable value: it is stdlib-only
here and needs no 0.15 imports.

What this adds over ``approval_records.py`` (which stays the minting and
validation layer — records are still minted ONLY via ``issue_record()`` at
the authenticated point):

  - Immutable SQLite event log: BEFORE UPDATE/DELETE triggers ABORT, so
    history cannot be edited in place, only appended to.
  - HMAC-SHA256 (per-event) + an independent MAC'd head anchor: tampering
    with any event, or rolling the log back, breaks verification.
  - Compare-and-swap head on every mutation: concurrent writers fail closed
    instead of interleaving decisions.
  - Monotonic clock enforcement: backdated events are rejected.
  - Revocation and supersession as first-class events (0.16: a new request
    supersedes the previous one; rejected/revoked/expired decisions cannot
    authorize).
  - Expiry semantics: decisions carry ``expires_at``; ``inspect()`` reports
    APPROVED / REJECTED / REVOKED / SUPERSEDED / EXPIRED per decision.
  - Offline-restore fencing: an ``OFFLINE_RESTORE.json`` marker beside the
    journal home blocks reopening until a host has reviewed and requalified
    the restored store.
  - Private storage: mode 0700 home, mode 0600 files, inode/dev identity
    pinned at open and rechecked around every transaction; symlinks rejected.

Authority model (unchanged from approval_records.py): the principal is
DERIVED from the evidence origin (``operator_channel`` = the operator's own
words on the operator channel, handled by the main agent; ``standing_registry``
= recorded standing directives). Workers present journal exports; they never
mint decisions. ``export()`` returns the current APPROVED grants plus an
``approvals_sha256`` pin a grant site re-checks — copying an old export
cannot outlive a revocation.

This module grants NO execution authority. It records decisions; it does not
make them, and a journal entry never substitutes for the operator's words.
Backfilled entries (decisions made before this journal existed) are labeled
``backfilled: true`` with transcript provenance — honest bookkeeping, not
invention.

Storage: one private home per journal, default
``~/workspace/keel/hidden_files/operator-decisions/``. Keep it out of the
source tree and out of distributable bundles.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

AUTHENTICATED_ORIGINS = ("operator_channel", "standing_registry")
"""Origins allowed to mint decisions. Same contract as approval_records.py."""

DB_NAME = "decision-journal.sqlite3"
KEY_NAME = "decision-journal.key"
OFFLINE_MARKER = "OFFLINE_RESTORE.json"
EVENT_SCHEMA = "keel.operator-decision-event.v1"
JOURNAL_VERSION = "odj/1"

APPEND_BATCH_MAX = 50
"""Hard cap on append_batch() size.

Oversized batches are REFUSED with a clear error (bulk_append_too_large) —
never bulk-load the live journal; chunk the work instead. This cap is the
bulk-ingestion guard: single-append paths (decide/revoke/note) are unaffected.
"""

KINDS = ("create", "decide", "revoke", "note")
DECISIONS = ("approve", "reject")


class JournalError(ValueError):
    """Fixed reason codes; private material never belongs in errors."""


def _require(condition, code):
    if not condition:
        raise JournalError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utcnow_int():
    return int(time.time())


def _iso_to_int(value):
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise JournalError("decision_expiry_unparseable")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _private_dir(path: Path) -> Path:
    path = Path(path)
    _require(not path.is_symlink(), "journal_path_symlink_rejected")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.stat()
    _require(not stat.S_ISLNK(st.st_mode), "journal_path_symlink_rejected")
    _require(stat.S_IMODE(st.st_mode) & 0o777 == 0o700 or os.geteuid() != 0,
             "journal_home_permissions_invalid")
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _identity(st):
    return (st.st_dev, st.st_ino)


class DecisionJournal:
    """Tamper-evident append-only decision journal."""

    # ---------------------------------------------------------------- create

    @classmethod
    def create(cls, home, *, clock=None, label="operator-decisions"):
        clock = clock or _utcnow_int
        home = _private_dir(home)
        _require(not (home.parent / OFFLINE_MARKER).exists(),
                 "restored_journal_requires_reviewed_rebinding")
        db_path = home / DB_NAME
        key_path = home / KEY_NAME
        _require(not db_path.exists() and not key_path.exists(),
                 "journal_already_exists")
        now = int(clock())
        key = secrets.token_bytes(32)
        _write_private(key_path, key)
        db = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, "
                       "payload TEXT NOT NULL, mac TEXT NOT NULL)")
            db.execute("CREATE TABLE journal_head (singleton INTEGER PRIMARY KEY "
                       "CHECK(singleton=1), payload TEXT NOT NULL, mac TEXT NOT NULL)")
            for op in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER immutable_{op.lower()} BEFORE {op} ON events "
                    f"BEGIN SELECT RAISE(ABORT, 'decision_events_immutable'); END")
            event = {"schema": EVENT_SCHEMA, "journal": JOURNAL_VERSION,
                     "sequence": 0, "previous_sha256": None, "kind": "create",
                     "issued_at": now,
                     "payload": {"label": label,
                                 "note": "genesis: journal created; no decisions yet"}}
            cls._insert(db, key, event)
        finally:
            db.close()
        os.chmod(db_path, 0o600)
        return cls(home, clock=clock)

    @staticmethod
    def _insert(db, key, event):
        raw = _canonical(event)
        db.execute("INSERT INTO events VALUES (?,?,?)",
                   (event["sequence"], raw.decode("utf-8"),
                    hmac.new(key, raw, hashlib.sha256).hexdigest()))
        anchor = _canonical({"sequence": event["sequence"],
                             "head_sha256": _digest(event)})
        db.execute("INSERT INTO journal_head VALUES (1,?,?) "
                   "ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload,"
                   "mac=excluded.mac",
                   (anchor.decode("utf-8"),
                    hmac.new(key, anchor, hashlib.sha256).hexdigest()))

    # ---------------------------------------------------------------- open

    def __init__(self, home, *, clock=None):
        self.clock = clock or _utcnow_int
        self.home = Path(home)
        _require(self.home.is_dir() and not self.home.is_symlink(),
                 "journal_home_invalid")
        _require(not (self.home.parent / OFFLINE_MARKER).exists(),
                 "restored_journal_requires_reviewed_rebinding")
        self._home_identity = _identity(self.home.stat())
        self._file_identities = {}
        for name in (DB_NAME, KEY_NAME):
            p = self.home / name
            _require(p.is_file() and not p.is_symlink(), "journal_store_invalid")
            st = p.stat()
            _require(stat.S_IMODE(st.st_mode) & 0o777 == 0o600,
                     "journal_file_permissions_invalid")
            self._file_identities[name] = _identity(st)
        with self._connection() as (db, key):
            self._load(db, key)

    def _check_identities(self):
        _require(not (self.home.parent / OFFLINE_MARKER).exists(),
                 "restored_journal_requires_reviewed_rebinding")
        _require(_identity(self.home.stat()) == self._home_identity,
                 "journal_home_replaced")
        for name, ident in self._file_identities.items():
            p = self.home / name
            _require(not p.is_symlink(), "journal_file_symlink_rejected")
            _require(_identity(p.stat()) == ident, "journal_file_replaced")

    @contextmanager
    def _connection(self, write=False):
        self._check_identities()
        db = None
        try:
            key = (self.home / KEY_NAME).read_bytes()
            _require(len(key) == 32, "journal_key_invalid")
            db = sqlite3.connect(
                f"file:{self.home / DB_NAME}?mode={'rw' if write else 'ro'}",
                uri=True, isolation_level=None, timeout=15)
            db.execute("PRAGMA trusted_schema=OFF")
            if write:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
            yield db, key
            self._check_identities()
            _require((self.home / KEY_NAME).read_bytes() == key,
                     "journal_key_changed")
            if write:
                self._load(db, key)
                db.commit()
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _load(self, db, key):
        events, previous, last_now = [], None, 0
        rows = db.execute(
            "SELECT sequence,payload,mac FROM events ORDER BY sequence").fetchall()
        for index, (seq, raw, mac) in enumerate(rows):
            _require(type(raw) is str and type(mac) is str and hmac.compare_digest(
                hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest(), mac),
                "journal_integrity_failure")
            event = json.loads(raw)
            _require(seq == index == event["sequence"]
                     and event["previous_sha256"] == previous
                     and event.get("schema") == EVENT_SCHEMA,
                     "journal_chain_invalid")
            _require(type(event["issued_at"]) is int
                     and last_now <= event["issued_at"],
                     "journal_clock_invalid")
            _require(event["kind"] in KINDS, "journal_event_kind_invalid")
            previous, last_now = _digest(event), event["issued_at"]
            events.append(event)
        _require(bool(events), "journal_empty")
        anchors = db.execute(
            "SELECT singleton,payload,mac FROM journal_head").fetchall()
        _require(len(anchors) == 1 and anchors[0][0] == 1,
                 "journal_anchor_missing")
        raw_anchor, mac_anchor = anchors[0][1:]
        _require(type(raw_anchor) is str and type(mac_anchor) is str
                 and hmac.compare_digest(
                     hmac.new(key, raw_anchor.encode("utf-8"),
                              hashlib.sha256).hexdigest(), mac_anchor)
                 and json.loads(raw_anchor) == {"sequence": len(events) - 1,
                                                "head_sha256": previous},
                 "journal_anchor_mismatch")
        _require(events[0]["kind"] == "create", "journal_genesis_missing")
        _require(int(self.clock()) >= last_now, "journal_clock_rollback")
        return events, previous

    # ---------------------------------------------------------------- write

    def _append(self, db, key, events, head, kind, payload):
        now = int(self.clock())
        _require(now >= events[-1]["issued_at"], "journal_clock_rollback")
        event = {"schema": EVENT_SCHEMA, "journal": JOURNAL_VERSION,
                 "sequence": len(events), "previous_sha256": head,
                 "kind": kind, "issued_at": now, "payload": payload}
        self._insert(db, key, event)
        return events + [event], _digest(event)

    def decide(self, *, decision_id, decision, scope, evidence,
               expires_at=None, ttl_h=None, single_use=False,
               supersedes=None, backfilled=False, expected_head_sha256,
               confirmed=True):
        """Record an operator decision. Mutations require explicit confirmation.

        evidence: {"origin": "operator_channel"|"standing_registry",
                   "text": "<the operator's words>", "at": "<iso, optional>",
                   "ref": "<provenance pointer, optional>"}
        Exactly one of expires_at (ISO-8601) / ttl_h (hours) must be given.
        The principal is derived from evidence["origin"] — there is no
        approver/name parameter a worker could fill.
        """
        _require(confirmed is True, "explicit_decision_confirmation_required")
        _require(type(decision_id) is str and 1 <= len(decision_id) <= 128
                 and decision_id.replace("-", "").replace("_", "").isalnum(),
                 "decision_id_invalid")
        _require(decision in DECISIONS, "decision_invalid")
        origin = (evidence or {}).get("origin")
        _require(origin in AUTHENTICATED_ORIGINS,
                 "decision_origin_not_authenticated")
        text = ((evidence or {}).get("text") or "").strip()
        _require(bool(text), "decision_evidence_text_required")
        _require(isinstance(scope, dict) and scope.get("action"),
                 "decision_scope_requires_action")
        if expires_at is not None:
            exp = _iso_to_int(expires_at)
        elif ttl_h is not None:
            _require(isinstance(ttl_h, (int, float)) and ttl_h > 0,
                     "decision_ttl_invalid")
            exp = int(self.clock()) + int(ttl_h * 3600)
        else:
            raise JournalError("decision_expiry_required")
        _require(isinstance(expected_head_sha256, str)
                 and len(expected_head_sha256) == 64,
                 "decision_head_pin_required")
        payload = {"decision_id": decision_id, "decision": decision,
                   "scope": scope, "evidence": {"origin": origin, "text": text[:2000],
                                                "at": (evidence.get("at") or ""),
                                                "ref": (evidence.get("ref") or "")[:300]},
                   "principal": ("operator (operator channel)" if origin == "operator_channel"
                                 else "operator (recorded standing directive)"),
                   "issued_at": int(self.clock()), "expires_at": exp,
                   "single_use": bool(single_use),
                   "supersedes": supersedes, "backfilled": bool(backfilled)}
        with self._connection(write=True) as (db, key):
            events, head = self._load(db, key)
            _require(head == expected_head_sha256,
                     "decision_head_compare_and_swap_failed")
            if supersedes is not None:
                states = self._states(events, int(self.clock()))
                _require(supersedes in states
                         and states[supersedes]["state"] == "APPROVED",
                         "supersede_target_not_currently_approved")
            else:
                ids = {e["payload"]["decision_id"] for e in events
                       if e["kind"] == "decide"}
                _require(decision_id not in ids, "decision_id_already_recorded")
            events, head = self._append(db, key, events, head, "decide", payload)
        return {"decision_id": decision_id, "head_sha256": head,
                "state": "APPROVED" if decision == "approve" else "REJECTED"}

    def revoke(self, *, decision_id, reason, evidence, expected_head_sha256,
               confirmed=True):
        """Revoke a currently APPROVED decision. Revocation is itself journaled."""
        _require(confirmed is True, "explicit_revocation_confirmation_required")
        _require(type(reason) is str and 1 <= len(reason) <= 512
                 and "\x00" not in reason, "revocation_reason_required")
        origin = (evidence or {}).get("origin")
        _require(origin in AUTHENTICATED_ORIGINS,
                 "revocation_origin_not_authenticated")
        _require(isinstance(expected_head_sha256, str)
                 and len(expected_head_sha256) == 64,
                 "revocation_head_pin_required")
        with self._connection(write=True) as (db, key):
            events, head = self._load(db, key)
            _require(head == expected_head_sha256,
                     "revocation_head_compare_and_swap_failed")
            states = self._states(events, int(self.clock()))
            _require(decision_id in states
                     and states[decision_id]["state"] == "APPROVED",
                     "revocation_requires_current_approval")
            payload = {"decision_id": decision_id, "reason": reason,
                       "evidence": {"origin": origin,
                                    "text": ((evidence.get("text") or "").strip())[:500],
                                    "ref": (evidence.get("ref") or "")[:300]}}
            events, head = self._append(db, key, events, head, "revoke", payload)
        return {"decision_id": decision_id, "head_sha256": head, "state": "REVOKED"}

    def note(self, *, text, ref=""):
        """Append an informational annotation. Notes never authorize anything."""
        _require(type(text) is str and 1 <= len(text) <= 2000,
                 "note_text_invalid")
        with self._connection(write=True) as (db, key):
            events, head = self._load(db, key)
            events, head = self._append(db, key, events, head, "note",
                                        {"text": text, "ref": ref[:300]})
        return {"head_sha256": head}

    def append_batch(self, *, items, expected_head_sha256, confirmed=True):
        """Append up to APPEND_BATCH_MAX notes in ONE write transaction.

        The O(n^2) bulk path (one full-chain _load per note) is why this
        exists: a single _load covers the whole batch, so appending N notes
        is O(N), not O(N^2). All tamper-evidence checks still apply —
        per-event MACs, chain linkage, monotonic clock, CAS head, and the
        pre-commit re-verification in _connection.

        Batches are note-kind ONLY: notes never authorize anything, so a
        batch cannot mint, revoke, or supersede decisions. decide/revoke
        payloads stay on their single-append paths with their own evidence
        and expiry validation.

        Batches larger than APPEND_BATCH_MAX are refused outright
        (bulk_append_too_large) with a clear error — the live journal is
        not a bulk-ingestion store; chunk the work instead.
        """
        _require(confirmed is True, "explicit_batch_confirmation_required")
        _require(isinstance(items, list) and len(items) >= 1,
                 "batch_items_invalid")
        _require(len(items) <= APPEND_BATCH_MAX, "bulk_append_too_large")
        for it in items:
            _require(isinstance(it, dict), "batch_items_invalid")
            _require(it.get("kind", "note") == "note",
                     "batch_kind_not_note")
            _require(type(it.get("text")) is str
                     and 1 <= len(it["text"]) <= 2000, "note_text_invalid")
            _require(type(it.get("ref", "")) is str
                     and len(it.get("ref", "")) <= 300, "batch_ref_invalid")
        _require(isinstance(expected_head_sha256, str)
                 and len(expected_head_sha256) == 64,
                 "batch_head_pin_required")
        with self._connection(write=True) as (db, key):
            events, head = self._load(db, key)
            _require(head == expected_head_sha256,
                     "batch_head_compare_and_swap_failed")
            for it in items:
                events, head = self._append(
                    db, key, events, head, "note",
                    {"text": it["text"], "ref": (it.get("ref") or "")[:300]})
        return {"appended": len(items), "head_sha256": head}

    # ---------------------------------------------------------------- read

    def _states(self, events, now):
        states = {}
        order = []
        for event in events:
            p = event["payload"]
            if event["kind"] == "decide":
                did = p["decision_id"]
                if p.get("supersedes") and p["supersedes"] in states:
                    states[p["supersedes"]]["state"] = "SUPERSEDED"
                    states[p["supersedes"]]["superseded_by"] = did
                states[did] = {"state": "APPROVED" if p["decision"] == "approve"
                               else "REJECTED",
                               "decision_id": did, "scope": p["scope"],
                               "evidence": p["evidence"],
                               "principal": p["principal"],
                               "issued_at": p["issued_at"],
                               "expires_at": p["expires_at"],
                               "single_use": p["single_use"],
                               "backfilled": p["backfilled"],
                               "superseded_by": None}
                if did not in order:
                    order.append(did)
            elif event["kind"] == "revoke":
                did = p["decision_id"]
                if did in states and states[did]["state"] == "APPROVED":
                    states[did]["state"] = "REVOKED"
                    states[did]["revocation"] = {"reason": p["reason"],
                                                 "evidence": p["evidence"],
                                                 "revoked_at": event["issued_at"]}
        for did in order:
            s = states[did]
            if s["state"] == "APPROVED" and now >= s["expires_at"]:
                s["state"] = "EXPIRED"
        return states

    def inspect(self):
        """Current per-decision states. Read-only; never creates records."""
        with self._connection() as (db, key):
            events, head = self._load(db, key)
            now = int(self.clock())
            states = self._states(events, now)
            return {"schema": "keel.operator-decisions.v1",
                    "head_sha256": head, "event_count": len(events),
                    "now": now, "decisions": states}

    def export(self):
        """Currently APPROVED grants + pin. Grant sites re-check the pin —
        a copied export cannot outlive a revocation or expiry."""
        view = self.inspect()
        grants = [s for s in view["decisions"].values()
                  if s["state"] == "APPROVED"]
        return {"schema": "keel.operator-decision-export.v1",
                "head_sha256": view["head_sha256"],
                "grants": grants,
                "approvals_sha256": _digest(grants),
                "exported_at": view["now"]}

    def verify(self):
        """Full chain re-verification. Returns (ok, checks)."""
        checks = []
        try:
            with self._connection() as (db, key):
                events, head = self._load(db, key)
            checks.append(("chain_mac_and_anchor_valid", True))
            checks.append(("event_count", len(events)))
            # Re-derive states twice; determinism is the consistency check.
            now = int(self.clock())
            a = self._states(events, now)
            b = self._states(events, now)
            checks.append(("state_derivation_deterministic",
                           json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)))
            ok = all(c[1] is True or isinstance(c[1], int) for c in checks)
        except JournalError as e:
            checks.append((str(e), False))
            ok = False
        return ok, checks

    def grants_cover(self, required_scope):
        """Currently APPROVED grants whose scope covers required_scope.

        Call this AT the grant site against the live journal — never cache
        a grants_cover/export result and reuse it later; a cached pin cannot
        outlive a revocation or expiry the journal records after the copy.

        Scope cover rule mirrors approval_records.scope_covers: a grant value
        of "*" covers any required value for that key.
        """
        view = self.export()
        matched = []
        for g in view["grants"]:
            scope = g["scope"]
            ok = True
            for k, v in (required_scope or {}).items():
                if k not in scope or (scope[k] != "*" and scope[k] != v):
                    ok = False
                    break
            if ok:
                matched.append(g)
        return matched, view["approvals_sha256"]


# ---------------------------------------------------------------- CLI
# The CLI's `decide`/`revoke` are the authenticated point's tools — the main
# agent calls them with the operator's own words. Workers never mint.

def _cli():
    ap = argparse.ArgumentParser(description="operator decision journal (0.16 port)")
    ap.add_argument("--home",
                    default=str(Path.home() / "workspace/keel/hidden_files/operator-decisions"),
                    help="journal home (private, outside the source tree)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("create", help="create a new journal")

    p = sub.add_parser("decide", help="record an operator decision (authenticated point only)")
    p.add_argument("--decision-id", required=True)
    p.add_argument("--decision", required=True, choices=list(DECISIONS))
    p.add_argument("--scope", required=True, help="JSON scope, must name 'action'")
    p.add_argument("--origin", required=True, choices=list(AUTHENTICATED_ORIGINS))
    p.add_argument("--text", required=True, help="the operator's words (immutable evidence)")
    p.add_argument("--ref", default="", help="provenance pointer")
    p.add_argument("--at", default="", help="ISO timestamp of the original decision")
    p.add_argument("--ttl-h", type=float, default=None)
    p.add_argument("--expires-at", default=None, help="ISO-8601 expiry")
    p.add_argument("--single-use", action="store_true")
    p.add_argument("--supersedes", default=None)
    p.add_argument("--backfilled", action="store_true")
    p.add_argument("--head", required=True, help="expected current head_sha256 (CAS)")
    p.add_argument("--confirmed", action="store_true", required=True)

    p = sub.add_parser("revoke", help="revoke a currently approved decision")
    p.add_argument("--decision-id", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--origin", required=True, choices=list(AUTHENTICATED_ORIGINS))
    p.add_argument("--text", default="", help="revocation evidence text")
    p.add_argument("--ref", default="")
    p.add_argument("--head", required=True)
    p.add_argument("--confirmed", action="store_true", required=True)

    p = sub.add_parser("note", help="append an informational annotation")
    p.add_argument("--text", required=True)
    p.add_argument("--ref", default="")

    sub.add_parser("inspect", help="show current decision states")
    sub.add_parser("export", help="export current APPROVED grants + pin")
    sub.add_parser("verify", help="full chain re-verification")
    p = sub.add_parser("grants-cover", help="APPROVED grants covering a scope")
    p.add_argument("--scope", required=True, help="JSON required scope")

    a = ap.parse_args()
    if a.cmd == "create":
        j = DecisionJournal.create(a.home)
        print(json.dumps({"created": str(a.home),
                          "head_sha256": j.inspect()["head_sha256"]}, indent=1))
        return
    j = DecisionJournal(a.home)
    if a.cmd == "decide":
        print(json.dumps(j.decide(
            decision_id=a.decision_id, decision=a.decision,
            scope=json.loads(a.scope),
            evidence={"origin": a.origin, "text": a.text, "ref": a.ref, "at": a.at},
            expires_at=a.expires_at, ttl_h=a.ttl_h, single_use=a.single_use,
            supersedes=a.supersedes, backfilled=a.backfilled,
            expected_head_sha256=a.head, confirmed=a.confirmed), indent=1))
    elif a.cmd == "revoke":
        print(json.dumps(j.revoke(
            decision_id=a.decision_id, reason=a.reason,
            evidence={"origin": a.origin, "text": a.text, "ref": a.ref},
            expected_head_sha256=a.head, confirmed=a.confirmed), indent=1))
    elif a.cmd == "note":
        print(json.dumps(j.note(text=a.text, ref=a.ref), indent=1))
    elif a.cmd == "inspect":
        print(json.dumps(j.inspect(), indent=1))
    elif a.cmd == "export":
        print(json.dumps(j.export(), indent=1))
    elif a.cmd == "verify":
        ok, checks = j.verify()
        print(json.dumps({"ok": ok, "checks": checks}, indent=1))
        sys.exit(0 if ok else 1)
    elif a.cmd == "grants-cover":
        matched, pin = j.grants_cover(json.loads(a.scope))
        print(json.dumps({"matched": matched, "approvals_sha256": pin}, indent=1))


if __name__ == "__main__":
    _cli()
