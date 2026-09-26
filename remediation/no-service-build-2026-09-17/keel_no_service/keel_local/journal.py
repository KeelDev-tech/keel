"""Local diagnostic/work outbox. NEVER an application ledger or receipt writer.

Only idempotent preparation/review requests belong here. The live queue and its
attempt holds remain authoritative. A browser handoff is a proposal, not a click.
"""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import hashlib
import sqlite3
import uuid
from .contracts import ContractError, canonical, digest, number, integer, strict_json, text

KINDS = {"packet_build", "browser_handoff", "discovery_sweep", "readiness_refresh",
         "proposal_review", "scheduler_drift", "browser_lifecycle", "meter"}
APPLICATION_ID = 0x4B444941  # KDIA: diagnostic storage, not a live workflow DB


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Inspect read-only BEFORE changing journal settings or creating any
            # tables. A live workflow database must never be silently adopted.
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as existing_db:
                if existing_db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                    raise ContractError("not a Keel diagnostic database; refusing adoption")
        with self.connection() as conn:
            existing = conn.execute("PRAGMA user_version").fetchone()[0]
            if existing not in (0, 1):
                raise ContractError("unsupported diagnostic database version")
            if existing == 0 and conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ContractError("refusing an existing non-diagnostic database")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS work (
                    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    available REAL NOT NULL, token TEXT, lease_until REAL,
                    last_error TEXT);
                PRAGMA user_version=1;
                PRAGMA application_id=1262766401;
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def enqueue(self, event_id, kind, body, *, now):
        text(event_id); number(now)
        if kind not in KINDS or type(body) is not dict:
            raise ContractError("not an allowed diagnostic/preparation event")
        encoded = canonical(body).decode()
        if len(encoded.encode()) > 65536:
            raise ContractError("event exceeds 64 KiB")
        body_hash = digest({"kind": kind, "body": body})
        with self.transaction() as conn:
            old = conn.execute("SELECT body_hash FROM events WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if old[0] != body_hash:
                    raise ContractError("event ID replayed with different content")
                return False
            conn.execute("INSERT INTO events(event_id,kind,body,body_hash,created) VALUES(?,?,?,?,?)",
                         (event_id, kind, encoded, body_hash, now))
            conn.execute("INSERT INTO work(event_id,state,available) VALUES(?,'PENDING',?)", (event_id, now))
        return True

    def claim(self, *, now, lease_seconds=60, max_attempts=5):
        number(now); number(lease_seconds, minimum=1, maximum=3600); integer(max_attempts, minimum=1)
        token = uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute("UPDATE work SET state='DEAD',token=NULL WHERE attempts>=? AND "
                         "(state='PENDING' OR (state='CLAIMED' AND lease_until<=?))", (max_attempts, now))
            row = conn.execute("SELECT work.*,events.kind,events.body FROM work JOIN events USING(event_id) "
                               "WHERE attempts<? AND ((state='PENDING' AND available<=?) OR "
                               "(state='CLAIMED' AND lease_until<=?)) ORDER BY events.seq LIMIT 1",
                               (max_attempts, now, now)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE work SET state='CLAIMED',attempts=attempts+1,token=?,lease_until=? WHERE event_id=?",
                         (token, now + lease_seconds, row["event_id"]))
            return {"event_id": row["event_id"], "kind": row["kind"], "body": strict_json(row["body"]),
                    "token": token, "attempts": row["attempts"] + 1, "lease_until": now + lease_seconds}

    def heartbeat(self, event_id, token, *, now, lease_seconds=60):
        number(now); number(lease_seconds, minimum=1, maximum=3600)
        with self.transaction() as conn:
            return conn.execute("UPDATE work SET lease_until=? WHERE event_id=? AND token=? "
                                "AND state='CLAIMED' AND lease_until>?",
                                (now + lease_seconds, event_id, token, now)).rowcount == 1

    def finish(self, event_id, token, *, now, error_code=None, retry_after=0, max_attempts=5):
        number(now); number(retry_after); integer(max_attempts, minimum=1)
        # Never store provider bodies, secrets or arbitrary exception text here.
        if error_code not in {None, "RATE_LIMITED", "TEMPORARY", "INVALID_INPUT", "PERMANENT"}:
            raise ContractError("unapproved error category")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM work WHERE event_id=? AND token=? AND state='CLAIMED' "
                               "AND lease_until>?", (event_id, token, now)).fetchone()
            if not row:
                return False
            if error_code is None:
                state, delay = "DONE", 0
            elif error_code in {"INVALID_INPUT", "PERMANENT"} or row["attempts"] >= max_attempts:
                state, delay = "DEAD", 0
            else:
                state = "PENDING"
                jitter = int(hashlib.sha256(f'{event_id}:{row["attempts"]}'.encode()).hexdigest()[:4], 16) / 65535
                delay = max(retry_after, min(3600, 2 ** min(row["attempts"], 12)) * (1 + jitter / 4))
            conn.execute("UPDATE work SET state=?,available=?,token=NULL,lease_until=NULL,last_error=? WHERE event_id=?",
                         (state, now + delay, error_code, event_id))
        return True

    def export(self, page_size=500):
        """Complete consistent snapshot, no silent 1,000-row cap."""
        integer(page_size, minimum=1)
        with self.connection() as conn:
            conn.execute("BEGIN")
            after = 0
            while True:
                rows = conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, page_size)).fetchall()
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
                after = rows[-1]["seq"]
            conn.rollback()

    def backup(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise ContractError("backup destination must be new")
        with self.connection() as conn, sqlite3.connect(destination) as output:
            conn.backup(output)
            if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ContractError("backup integrity failure")
        return {"sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "scope": "diagnostic_outbox_only", "production_restore_proven": False}
