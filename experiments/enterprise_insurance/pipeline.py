"""Opt-in, synthetic-only B2B qualification lane. No network or Keel runtime imports.

Python 3.11+, POSIX. Local integrity checks are not authentication or a sandbox.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import urlsplit

LANE = "enterprise_insurance_v0"
FLAG = "KEEL_ENTERPRISE_INSURANCE_ENABLED"
WEIGHTS = {"identity": 10, "sector": 15, "need": 25,
           "buyer_path": 15, "volume": 15, "workflow": 20}
REQUIRED = {"identity", "sector", "need"}
THRESHOLD = 75  # Independent experiment policy, not the applicant fit model.
MAX_BYTES = 65536
MAX_ACCOUNTS = 1000
MAX_EVENTS = 20000
STATES = {"DISCOVERED", "EVIDENCE_REVIEW", "PARKED", "QUALIFIED_FOR_DISCOVERY",
          "HUMAN_APPROVED_SIMULATED", "PILOT_CANDIDATE_SIMULATED",
          "REJECTED", "SUPPRESSED", "EXPIRED"}
TERMINAL = {"REJECTED", "SUPPRESSED", "EXPIRED"}
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.example\Z")
IDENT = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")


class Refused(ValueError):
    """A bounded, non-sensitive failure reason safe to show to an operator."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refused(reason)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timestamp(value: str) -> datetime:
    require(isinstance(value, str) and len(value) <= 40, "INVALID_TIMESTAMP")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "TIMEZONE_REQUIRED")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise Refused("INVALID_TIMESTAMP") from None


def iso(value: datetime) -> str:
    require(isinstance(value, datetime) and value.tzinfo is not None, "TIMEZONE_REQUIRED")
    return value.astimezone(timezone.utc).isoformat()


def exact(value: object, keys: set[str]) -> None:
    require(type(value) is dict and set(value) == keys, "SCHEMA_FIELDS_REFUSED")


def load_json(text: str, max_bytes: int = MAX_BYTES) -> object:
    require(isinstance(text, str) and len(text.encode()) <= max_bytes, "INPUT_TOO_LARGE")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refused("NONFINITE_JSON")))
    except (json.JSONDecodeError, RecursionError):
        raise Refused("INVALID_JSON") from None


def reference(value: str) -> None:
    require(isinstance(value, str) and len(value) <= 160, "SOURCE_REFUSED")
    try:
        u = urlsplit(value)
        require(u.scheme == "https" and u.hostname is not None
                and DOMAIN.fullmatch(u.hostname) is not None
                and u.netloc == u.hostname and not u.query and not u.fragment
                and re.fullmatch(r"/[a-z0-9/_-]{1,100}", u.path) is not None,
                "SOURCE_REFUSED")
    except ValueError:
        raise Refused("SOURCE_REFUSED") from None


def validate(packet: dict) -> dict:
    exact(packet, {"schema_version", "lane", "synthetic", "account_id", "organization_name",
                   "business_domain", "source_type", "retention_until", "evidence"})
    require(type(packet["schema_version"]) is int and packet["schema_version"] == 1, "VERSION_REFUSED")
    require(packet["lane"] == LANE and packet["synthetic"] is True
            and packet["source_type"] == "synthetic_fixture", "NON_SYNTHETIC_DATA_REFUSED")
    require(isinstance(packet["account_id"], str) and IDENT.fullmatch(packet["account_id"]) is not None,
            "ACCOUNT_ID_REFUSED")
    require(isinstance(packet["organization_name"], str)
            and re.fullmatch(r"Synthetic [A-Za-z ]{1,60}", packet["organization_name"]) is not None,
            "ORGANIZATION_NAME_REFUSED")
    require(isinstance(packet["business_domain"], str)
            and DOMAIN.fullmatch(packet["business_domain"]) is not None, "REAL_DOMAIN_REFUSED")
    timestamp(packet["retention_until"])
    evidence = packet["evidence"]
    require(type(evidence) is list and len(evidence) <= 24, "EVIDENCE_BOUND_REFUSED")
    for item in evidence:
        exact(item, {"criterion", "value", "source_url", "observed_at", "expires_at", "permission"})
        require(type(item["criterion"]) is str and item["criterion"] in WEIGHTS, "CRITERION_REFUSED")
        require(type(item["value"]) is str and item["value"] in {"yes", "no", "unknown"}, "VALUE_REFUSED")
        require(item["permission"] == "synthetic_only", "SOURCE_PERMISSION_REFUSED")
        reference(item["source_url"])
        observed, expires = timestamp(item["observed_at"]), timestamp(item["expires_at"])
        require(observed < expires <= observed + timedelta(days=30), "EVIDENCE_WINDOW_REFUSED")
    require(len(canonical(packet).encode()) <= MAX_BYTES, "INPUT_TOO_LARGE")
    # Normalize observation order; equivalent replays must not reset review state.
    result = json.loads(canonical(packet))
    result["evidence"].sort(key=canonical)
    return result


def assess(packet: dict, now: datetime) -> dict:
    now = timestamp(iso(now))
    values, reasons, score = {}, [], 0
    if timestamp(packet["retention_until"]) <= now:
        return {"state": "EXPIRED", "score": 0, "reasons": ["RETENTION_EXPIRED"]}
    for criterion, weight in WEIGHTS.items():
        observations = [x for x in packet["evidence"] if x["criterion"] == criterion]
        if any(timestamp(x["observed_at"]) > now for x in observations):
            reasons.append("FUTURE_EVIDENCE:" + criterion)
        live = {x["value"] for x in observations
                if timestamp(x["observed_at"]) <= now < timestamp(x["expires_at"])}
        if len(live) > 1:
            reasons.append("CONFLICT:" + criterion)
            values[criterion] = "unknown"
        else:
            values[criterion] = next(iter(live), "unknown")
        if values[criterion] == "unknown":
            reasons.append("MISSING_OR_STALE:" + criterion)
        elif values[criterion] == "yes":
            score += weight
        elif criterion in REQUIRED:
            reasons.append("REQUIRED_NO:" + criterion)
    if score < THRESHOLD:
        reasons.append("BELOW_THRESHOLD")
    return {"state": "PARKED" if reasons else "QUALIFIED_FOR_DISCOVERY",
            "score": score, "reasons": sorted(set(reasons))}


def checked_path(value: str | Path) -> Path:
    p = Path(value)
    require(p.is_absolute() and ".." not in p.parts, "ABSOLUTE_CLEAN_PATH_REQUIRED")
    for part in [*reversed(p.parents), p]:
        require(not part.is_symlink(), "SYMLINK_REFUSED")
    return p.resolve()


def overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def owner_file(path: Path) -> None:
    s = path.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid()
            and s.st_nlink == 1 and stat.S_IMODE(s.st_mode) == 0o600, "UNSAFE_WORKSPACE_FILE")


class Lane:
    def __init__(self, home: str | Path, *, exclude_keel_home: str | Path,
                 enabled: bool = False, now: datetime | None = None):
        require(enabled is True, "LANE_DISABLED")
        self.now = timestamp(iso(now or datetime.now(timezone.utc)))
        self.home = checked_path(home)
        self.excluded = [checked_path(exclude_keel_home)]
        if os.environ.get("KEEL_HOME"):
            self.excluded.append(checked_path(os.environ["KEEL_HOME"]))
        self.source_root = Path(__file__).resolve().parents[2]
        for p in self.excluded + [self.source_root]:
            require(not overlaps(self.home, p), "WORKSPACE_OVERLAP_REFUSED")
        self.dbfile = self.home / "lane.sqlite3"
        self.lockfile = self.home / ".lock"

    def init(self) -> dict:
        require(not self.home.exists() and self.home.parent.is_dir(), "FRESH_HOME_REQUIRED")
        self.home.mkdir(mode=0o700)
        # No reuse or migration of an existing directory or database.
        for p in (self.lockfile, self.dbfile):
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with closing(sqlite3.connect(self.dbfile)) as db, db:
            db.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO meta VALUES ('lane','enterprise_insurance_v0'),('version','1'),('paused','false');
                CREATE TABLE accounts (id TEXT PRIMARY KEY, domain TEXT UNIQUE,
                    packet TEXT, packet_hash TEXT NOT NULL, state TEXT NOT NULL,
                    score INTEGER NOT NULL, reasons TEXT NOT NULL,
                    review_hash TEXT, pilot_expires TEXT);
                CREATE TABLE events (seq INTEGER PRIMARY KEY, at TEXT NOT NULL,
                    account TEXT, action TEXT NOT NULL, payload TEXT NOT NULL,
                    previous TEXT NOT NULL, hash TEXT NOT NULL);
            """)
            db.row_factory = sqlite3.Row
            self._event(db, None, "INIT", {"paused": False})
        return {"lane": LANE, "mode": "synthetic_only", "outreach_authorized": False}

    def _check_home(self, sidecars=True):
        checked_path(self.home)
        s = self.home.stat()
        require(stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid()
                and stat.S_IMODE(s.st_mode) == 0o700, "UNSAFE_WORKSPACE")
        owner_file(self.lockfile)
        owner_file(self.dbfile)
        for suffix in (("-journal", "-wal", "-shm") if sidecars else ()):
            p = Path(str(self.dbfile) + suffix)
            require(not p.exists() and not p.is_symlink(), "SIDECAR_REQUIRES_OPERATOR_REVIEW")

    @contextmanager
    def _db(self, write: bool = False):
        self._check_home(sidecars=False)
        fd = os.open(self.lockfile, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._check_home()
            mode = "rw" if write else "ro"
            db = sqlite3.connect(self.dbfile.as_uri() + "?mode=" + mode, uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                if not write:
                    db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._verify(db)
                yield db
                if write:
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        finally:
            os.close(fd)

    @staticmethod
    def _snapshot(row):
        return {"fingerprint": digest(dict(row)), "state": row["state"]}

    def _event(self, db, account, action, payload):
        last = db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (last["seq"] + 1, last["hash"]) if last else (1, "0" * 64)
        require(seq <= MAX_EVENTS, "EVENT_CAP_REACHED")
        at = iso(self.now)
        body = {"seq": seq, "at": at, "account": account, "action": action,
                "payload": payload, "previous": previous}
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                   (seq, at, account, action, canonical(payload), previous, digest(body)))

    def _record(self, db, account, action):
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone()
        self._event(db, account, action, self._snapshot(row))

    def _verify(self, db):
        meta = dict(db.execute("SELECT key,value FROM meta").fetchall())
        require(meta.get("lane") == LANE and meta.get("version") == "1", "FOREIGN_DATABASE_REFUSED")
        previous, last_time, snapshots, paused = "0" * 64, None, {}, None
        events = db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        require(0 < len(events) <= MAX_EVENTS, "AUDIT_BOUND_REFUSED")
        for expected, row in enumerate(events, 1):
            payload = load_json(row["payload"])
            body = {k: row[k] for k in ("seq", "at", "account", "action", "previous")}
            body["payload"] = payload
            at = timestamp(row["at"])
            require(row["seq"] == expected and row["previous"] == previous
                    and row["hash"] == digest(body)
                    and (last_time is None or at >= last_time), "AUDIT_INTEGRITY_REFUSED")
            previous, last_time = row["hash"], at
            if row["account"] is not None:
                snapshots[row["account"]] = payload
            if row["action"] in {"INIT", "PAUSE", "RESUME"}:
                paused = payload["paused"]
        require(last_time <= self.now, "CLOCK_ROLLBACK_REFUSED")
        require(meta.get("paused") == canonical(paused) and type(paused) is bool, "META_INTEGRITY_REFUSED")
        rows = db.execute("SELECT * FROM accounts").fetchall()
        require(len(rows) <= MAX_ACCOUNTS and {r["id"] for r in rows} == set(snapshots),
                "ACCOUNT_SET_INTEGRITY_REFUSED")
        for row in rows:
            require(row["state"] in STATES and snapshots[row["id"]] == self._snapshot(row),
                    "PROJECTION_INTEGRITY_REFUSED")
            if row["packet"] is None:
                require(row["state"] == "EXPIRED" and row["domain"] is None, "PURGE_INTEGRITY_REFUSED")
            else:
                packet = validate(load_json(row["packet"]))
                require(digest(packet) == row["packet_hash"] and packet["account_id"] == row["id"]
                        and packet["business_domain"] == row["domain"], "PACKET_INTEGRITY_REFUSED")

    @staticmethod
    def _active(db):
        require(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "false", "LANE_PAUSED")

    @staticmethod
    def _row(db, account):
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone()
        require(row is not None, "ACCOUNT_NOT_FOUND")
        return row

    def read_input(self, path: str | Path, *, batch: bool = False) -> object:
        p = checked_path(path)
        for excluded in self.excluded:
            require(not overlaps(p, excluded), "APPLICANT_SOURCE_REFUSED")
        bound = 1048576 if batch else MAX_BYTES
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            s = os.fstat(f.fileno())
            require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size <= bound, "INPUT_FILE_REFUSED")
            return load_json(f.read(bound + 1), max_bytes=bound)

    def read_packet(self, path: str | Path) -> dict:
        return validate(self.read_input(path))

    def ingest(self, packet: dict) -> dict:
        return self.ingest_batch([packet])[0]

    def ingest_batch(self, packets: list[dict]) -> list[dict]:
        require(type(packets) is list and 1 <= len(packets) <= 100, "BATCH_LIMIT_REFUSED")
        prepared = [validate(p) for p in packets]
        require(len(canonical(prepared).encode()) <= 1048576, "INPUT_TOO_LARGE")
        for p in prepared:
            require(self.now < timestamp(p["retention_until"]) <= self.now + timedelta(days=90), "RETENTION_WINDOW_REFUSED")
        outcomes = []
        # All-or-nothing bounded intake; integrity checked once, not skipped.
        with self._db(True) as db:
            self._active(db)
            for p in prepared:
                h = digest(p)
                rows = db.execute("SELECT * FROM accounts WHERE id=? OR domain=?",
                                  (p["account_id"], p["business_domain"])).fetchall()
                if rows:
                    require(len(rows) == 1 and rows[0]["packet_hash"] == h, "IDENTITY_OR_REVISION_CONFLICT")
                    outcomes.append({"result": "duplicate", "account_id": rows[0]["id"], "packet_hash": h})
                    continue
                require(db.execute("SELECT count(*) FROM accounts").fetchone()[0] < MAX_ACCOUNTS, "ACCOUNT_CAP_REACHED")
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
                           (p["account_id"], p["business_domain"], canonical(p), h, "DISCOVERED", 0, "[]", None, None))
                self._record(db, p["account_id"], "INGEST")
                outcomes.append({"result": "created", "account_id": p["account_id"], "packet_hash": h})
        return outcomes

    def revise(self, packet: dict, expected_hash: str) -> dict:
        p = validate(packet)
        require(self.now < timestamp(p["retention_until"]) <= self.now + timedelta(days=90), "RETENTION_WINDOW_REFUSED")
        h = digest(p)
        with self._db(True) as db:
            self._active(db)
            row = self._row(db, p["account_id"])
            require(row["packet_hash"] == expected_hash, "STALE_REVIEW_REFUSED")
            require(row["state"] not in TERMINAL and self._effective(row)["state"] != "EXPIRED"
                    and row["domain"] == p["business_domain"], "REVISION_REFUSED")
            if h != expected_hash:
                db.execute("UPDATE accounts SET packet=?,packet_hash=?,state='DISCOVERED',score=0,reasons='[]',"
                           "review_hash=NULL,pilot_expires=NULL WHERE id=?", (canonical(p), h, p["account_id"]))
                self._record(db, p["account_id"], "REVISE")
        return {"account_id": p["account_id"], "packet_hash": h}

    def _effective(self, row):
        if row["packet"] is None:
            return {"state": "EXPIRED", "score": 0, "reasons": ["RETENTION_EXPIRED"]}
        assessment = assess(load_json(row["packet"]), self.now)
        if assessment["state"] == "EXPIRED":
            return assessment
        if row["state"] in TERMINAL:
            return {"state": row["state"], "score": row["score"], "reasons": load_json(row["reasons"])}
        if assessment["state"] == "PARKED":
            return assessment
        if row["state"] in {"DISCOVERED", "PARKED"}:
            return {"state": row["state"], "score": assessment["score"], "reasons": ["EVALUATION_REQUIRED"]}
        if row["state"] in {"HUMAN_APPROVED_SIMULATED", "PILOT_CANDIDATE_SIMULATED"}:
            if row["review_hash"] != row["packet_hash"]:
                return {"state": "PARKED", "score": assessment["score"], "reasons": ["STALE_REVIEW"]}
            if row["state"] == "PILOT_CANDIDATE_SIMULATED" and timestamp(row["pilot_expires"]) <= self.now:
                return {"state": "PARKED", "score": assessment["score"], "reasons": ["PILOT_PERMISSION_EXPIRED"]}
            assessment["state"] = row["state"]
        return assessment

    def run(self, limit: int = 25) -> dict:
        require(type(limit) is int and 1 <= limit <= 100, "BATCH_LIMIT_REFUSED")
        processed = []
        with self._db(True) as db:
            self._active(db)
            # Snapshot and state updates share one transaction. Unchanged holds do not starve intake.
            for row in db.execute("SELECT * FROM accounts ORDER BY rowid").fetchall():
                if row["state"] in TERMINAL:
                    continue
                result = assess(load_json(row["packet"]), self.now)
                if row["state"] in {"HUMAN_APPROVED_SIMULATED", "PILOT_CANDIDATE_SIMULATED"}:
                    result = self._effective(row)
                if result == {"state": row["state"], "score": row["score"], "reasons": load_json(row["reasons"])}:
                    continue
                db.execute("UPDATE accounts SET state='EVIDENCE_REVIEW' WHERE id=?", (row["id"],))
                self._record(db, row["id"], "EVIDENCE_REVIEW")
                db.execute("UPDATE accounts SET state=?,score=?,reasons=?,review_hash=NULL,pilot_expires=NULL WHERE id=?",
                           (result["state"], result["score"], canonical(result["reasons"]), row["id"]))
                self._record(db, row["id"], "EVALUATE")
                processed.append({"account_id": row["id"], **result})
                if len(processed) >= limit:
                    break
        return {"processed": processed, "count": len(processed)}

    def review(self, account: str, expected_hash: str, decision: str, reviewer: str) -> dict:
        require(decision in {"approve", "reject"} and isinstance(reviewer, str)
                and re.fullmatch(r"synthetic-reviewer-[a-z]{1,12}", reviewer) is not None, "REVIEW_REFUSED")
        with self._db(True) as db:
            self._active(db)
            row = self._row(db, account)
            require(row["packet_hash"] == expected_hash, "STALE_REVIEW_REFUSED")
            require(row["state"] not in TERMINAL, "TERMINAL_ACCOUNT_REFUSED")
            effective = self._effective(row)
            target = "HUMAN_APPROVED_SIMULATED" if decision == "approve" else "REJECTED"
            if row["state"] == target and effective["state"] == target:
                return {"account_id": account, "state": target, "outreach_authorized": False}
            require(row["state"] == effective["state"] == "QUALIFIED_FOR_DISCOVERY", "QUALIFICATION_REQUIRED")
            db.execute("UPDATE accounts SET state=?,review_hash=?,pilot_expires=NULL WHERE id=?",
                       (target, expected_hash if decision == "approve" else None, account))
            self._record(db, account, "REVIEW_" + decision.upper() + ":" + reviewer)
        return {"account_id": account, "state": target, "outreach_authorized": False}

    def pilot(self, grant: dict) -> dict:
        exact(grant, {"synthetic", "account_id", "packet_hash", "scope", "reference", "expires_at"})
        require(grant["synthetic"] is True and grant["scope"] == "offline_pilot", "LIVE_PERMISSION_UNSUPPORTED")
        reference(grant["reference"])
        expiry = timestamp(grant["expires_at"])
        require(self.now < expiry <= self.now + timedelta(days=14), "PILOT_WINDOW_REFUSED")
        with self._db(True) as db:
            self._active(db)
            row = self._row(db, grant["account_id"])
            require(row["packet_hash"] == grant["packet_hash"] and row["review_hash"] == grant["packet_hash"],
                    "STALE_REVIEW_REFUSED")
            state = self._effective(row)["state"]
            if state == "PILOT_CANDIDATE_SIMULATED" and row["pilot_expires"] == iso(expiry):
                return {"state": state, "outreach_authorized": False}
            require(state == "HUMAN_APPROVED_SIMULATED", "HUMAN_REVIEW_REQUIRED")
            db.execute("UPDATE accounts SET state='PILOT_CANDIDATE_SIMULATED',pilot_expires=? WHERE id=?",
                       (iso(expiry), row["id"]))
            self._record(db, row["id"], "SIMULATE_PILOT:" + digest(grant))
        return {"state": "PILOT_CANDIDATE_SIMULATED", "outreach_authorized": False}

    def suppress(self, account: str) -> dict:
        with self._db(True) as db:
            row = self._row(db, account)
            if row["state"] not in {"SUPPRESSED", "EXPIRED"}:
                db.execute("UPDATE accounts SET state='SUPPRESSED',reasons='[\"OPERATOR_SUPPRESSION\"]',"
                           "review_hash=NULL,pilot_expires=NULL WHERE id=?", (account,))
                self._record(db, account, "SUPPRESS")
        return {"account_id": account, "outreach_authorized": False}

    def pause(self, paused: bool) -> dict:
        require(type(paused) is bool, "PAUSE_VALUE_REFUSED")
        with self._db(True) as db:
            old = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0]
            if old != canonical(paused):
                db.execute("UPDATE meta SET value=? WHERE key='paused'", (canonical(paused),))
                self._event(db, None, "PAUSE" if paused else "RESUME", {"paused": paused})
        return {"paused": paused}

    def purge(self) -> dict:
        purged = 0
        with self._db(True) as db:
            for row in db.execute("SELECT * FROM accounts WHERE packet IS NOT NULL").fetchall():
                if timestamp(load_json(row["packet"])["retention_until"]) <= self.now:
                    db.execute("UPDATE accounts SET packet=NULL,domain=NULL,state='EXPIRED',score=0,"
                               "reasons='[\"RETENTION_EXPIRED\"]',review_hash=NULL,pilot_expires=NULL WHERE id=?", (row["id"],))
                    self._record(db, row["id"], "PURGE_LOGICAL")
                    purged += 1
        return {"logically_purged": purged, "secure_erasure": False}

    @staticmethod
    def external_action(action: str) -> dict:
        # No sender/collector/CRM adapter exists. Neither approvals nor flags grant effects.
        return {"allowed": False, "reason": "EXTERNAL_ACTIONS_NOT_IMPLEMENTED", "outreach_authorized": False}

    def report(self) -> dict:
        with self._db() as db:
            accounts = []
            for row in db.execute("SELECT * FROM accounts ORDER BY id"):
                result = self._effective(row)
                accounts.append({"account_id": row["id"], "packet_hash": row["packet_hash"],
                                 "domain": row["domain"] if result["state"] != "EXPIRED" else None,
                                 "stored_state": row["state"], **result, "outreach_authorized": False})
            states = dict(sorted(Counter(a["state"] for a in accounts).items()))
            blockers = dict(sorted(Counter(r for a in accounts for r in a["reasons"]).items()))
            return {"lane": LANE, "as_of": iso(self.now), "mode": "SYNTHETIC_ONLY",
                    "paused": db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true",
                    "unique_accounts": len(accounts), "state_counts": states, "blocker_counts": blockers,
                    "audit_events": db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "accounts": accounts, "outreach_authorized": False, "real_accounts_evaluated": 0,
                    "commercial_validation": "NOT_TESTED", "revenue": None,
                    "external_effect_adapters": 0}


def render_html(report: dict) -> str:
    e = lambda value: html.escape(str(value), quote=True)
    rows = "".join("<tr>" + "".join("<td>" + e(value) + "</td>" for value in
           (a["account_id"], a["state"], a["score"], "; ".join(a["reasons"]) or "None", "Blocked"))
           + "</tr>" for a in report["accounts"])
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Keel | Enterprise lane laboratory</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f3f5f7;color:#172331;line-height:1.6}
main{max-width:1180px;margin:auto;padding:40px 24px}header{border-bottom:3px solid #172331;margin-bottom:28px}
h1{font-size:34px;line-height:1.15}h2{font-size:21px}.badge{font-weight:700;letter-spacing:.1em;font-size:13px}
.metrics{display:flex;gap:20px;flex-wrap:wrap}.card{background:white;border:1px solid #d7dfe7;padding:20px;flex:1;min-width:180px}
strong{font-size:30px;display:block}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;background:white;font-size:14px}
th,td{padding:12px;text-align:left;border-bottom:1px solid #d7dfe7}th{background:#e6ecf2}footer{margin-top:30px;font-size:14px}
</style><main><header><p class="badge">KEEL / ENTERPRISE LAB / SYNTHETIC ONLY</p>
<h1>Business-account qualification</h1><p>Evidence, review and pilot-readiness simulation. Not a live sales pipeline.</p></header>
<div class="metrics"><div class="card">Unique fixture accounts<strong>""" + e(report["unique_accounts"]) + """</strong></div>
<div class="card">Real accounts evaluated<strong>0</strong></div><div class="card">External-action adapters<strong>0</strong></div></div>
<h2>Qualification queue</h2><p>Snapshot: """ + e(report["as_of"]) + " | Paused: " + e(report["paused"]) + """</p>
<div class="scroll"><table><caption>Current effective states; expired evidence is rechecked at report time.</caption>
<thead><tr><th>Account</th><th>State</th><th>Score / 100</th><th>Blockers</th><th>Outreach</th></tr></thead><tbody>""" + rows + """</tbody></table></div>
<footer>These counts describe developer fixtures, not real leads, customers, demand, conversion or revenue.
Local audit hashes detect inconsistency; they do not authenticate people or evidence. No live collection, messaging,
CRM access, insurance solicitation or Keel job-queue integration is installed.</footer></main></html>"""


def fixture(account: str, now: datetime, *, missing: str | None = None, no: str | None = None) -> dict:
    """Developer fixture factory; never use to relabel real records as synthetic."""
    return {"schema_version": 1, "lane": LANE, "synthetic": True, "account_id": account,
            "organization_name": "Synthetic Agency", "business_domain": account + ".example",
            "source_type": "synthetic_fixture", "retention_until": iso(now + timedelta(days=60)),
            "evidence": [{"criterion": k, "value": "no" if k == no else "yes",
                          "source_url": "https://evidence.example/" + account + "/" + k,
                          "observed_at": iso(now - timedelta(days=1)),
                          "expires_at": iso(now + timedelta(days=20)), "permission": "synthetic_only"}
                         for k in WEIGHTS if k != missing]}


def demo(lane: Lane) -> dict:
    lane.init()
    packets = [fixture("qualified-a", lane.now), fixture("qualified-b", lane.now, no="volume"),
               fixture("missing-need", lane.now, missing="need"), fixture("wrong-sector", lane.now, no="sector"),
               fixture("stale", lane.now), fixture("conflicting", lane.now), fixture("suppressed", lane.now),
               fixture("review-reject", lane.now), fixture("low-fit", lane.now, no="workflow")]
    for evidence in packets[4]["evidence"]:
        evidence["observed_at"] = iso(lane.now - timedelta(days=25))
        evidence["expires_at"] = iso(lane.now - timedelta(days=1))
    conflict = dict(packets[5]["evidence"][0], value="no")
    packets[5]["evidence"].append(conflict)
    for evidence in packets[8]["evidence"]:
        if evidence["criterion"] == "buyer_path":
            evidence["value"] = "no"
    outcomes = [lane.ingest(p) for p in packets]
    duplicate = lane.ingest(packets[0])
    lane.run(100)
    lane.suppress("suppressed")
    lane.review("review-reject", outcomes[7]["packet_hash"], "reject", "synthetic-reviewer-a")
    lane.review("qualified-a", outcomes[0]["packet_hash"], "approve", "synthetic-reviewer-a")
    lane.pilot({"synthetic": True, "account_id": "qualified-a", "packet_hash": outcomes[0]["packet_hash"],
                "scope": "offline_pilot", "reference": "https://permission.example/pilot-a",
                "expires_at": iso(lane.now + timedelta(days=7))})
    invalid = fixture("invalid", lane.now)
    invalid["email"] = "fixture@example.com"
    try:
        lane.ingest(invalid)
        raise AssertionError("invalid schema unexpectedly admitted")
    except Refused as error:
        refusal = str(error)
    return {"scenario": "DEVELOPER_FIXTURES_NOT_MARKET_VALIDATION", "duplicate": duplicate["result"],
            "invalid_record": refusal, "report": lane.report()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--exclude-keel-home", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "demo", "pause", "resume", "purge"):
        sub.add_parser(name)
    for name in ("ingest", "ingest-batch", "revise", "pilot"):
        p = sub.add_parser(name)
        p.add_argument("--packet", required=True)
        if name == "revise":
            p.add_argument("--expected-hash", required=True)
    p = sub.add_parser("run"); p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("report"); p.add_argument("--format", choices=("json", "html"), default="json")
    p = sub.add_parser("review")
    p.add_argument("--account", required=True); p.add_argument("--expected-hash", required=True)
    p.add_argument("--decision", choices=("approve", "reject"), required=True)
    p.add_argument("--reviewer", required=True)
    p = sub.add_parser("suppress"); p.add_argument("--account", required=True)
    p = sub.add_parser("request-external"); p.add_argument("--action", required=True)
    args = parser.parse_args(argv)
    try:
        lane = Lane(args.home, exclude_keel_home=args.exclude_keel_home,
                    enabled=os.environ.get(FLAG) == "true")
        cmd = args.command
        if cmd == "init": result = lane.init()
        elif cmd == "demo": result = demo(lane)
        elif cmd == "ingest": result = lane.ingest(lane.read_packet(args.packet))
        elif cmd == "ingest-batch": result = lane.ingest_batch(lane.read_input(args.packet, batch=True))
        elif cmd == "revise": result = lane.revise(lane.read_packet(args.packet), args.expected_hash)
        elif cmd == "pilot": result = lane.pilot(lane.read_input(args.packet))
        elif cmd == "run": result = lane.run(args.limit)
        elif cmd == "review": result = lane.review(args.account, args.expected_hash, args.decision, args.reviewer)
        elif cmd == "suppress": result = lane.suppress(args.account)
        elif cmd == "pause": result = lane.pause(True)
        elif cmd == "resume": result = lane.pause(False)
        elif cmd == "purge": result = lane.purge()
        elif cmd == "request-external": result = lane.external_action(args.action)
        else: result = lane.report()
        if cmd == "report" and args.format == "html":
            print(render_html(result))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if cmd == "request-external" else 0
    except Refused as error:
        print(json.dumps({"error": str(error), "outreach_authorized": False}), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, UnicodeError, TypeError, KeyError, OverflowError):
        print(json.dumps({"error": "LOCAL_INPUT_OR_STORAGE_REFUSED", "outreach_authorized": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
