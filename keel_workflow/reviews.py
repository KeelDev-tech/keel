"""Persisted two-phase local reviews, inspired by the blinded adversarial protocol.

This database is workflow metadata, not the canonical application/consent log.
The API withholds other opinions until every expected reviewer has committed.
The host must authenticate reviewer IDs, isolate reviewers, supply the current
subject and a trusted clock, and protect the database. A process/file owner can
read or rewrite SQLite; this cannot prove external model blindness, reviewer
independence, source truth, or authorization to execute an application.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat


MAX_SUBJECT_BYTES = 262144
MAX_LIFETIME_SECONDS = 900
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERDICTS = frozenset({"PASS", "FAIL", "ABSTAIN"})


def _id(value, label="id", *, reviewer=False):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded canonical identifier")
    if reviewer and value != value.casefold():
        raise ValueError("reviewer IDs must be lowercase; aliases are not accepted")
    return value


def _ids(values, label, *, minimum=0, maximum=256, reviewer=False):
    if not isinstance(values, list) or not minimum <= len(values) <= maximum:
        raise ValueError(f"{label} must be a list with {minimum}..{maximum} entries")
    output = [_id(value, label, reviewer=reviewer) for value in values]
    if len(set(output)) != len(output):
        raise ValueError(f"duplicate {label}")
    return output


def _json(value, limit=MAX_SUBJECT_BYTES):
    # Bound recursion and input traversal before asking the JSON encoder to run.
    stack = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > 20 or visited > 16384:
            raise ValueError("JSON structure exceeds review bounds")
        if item is None or type(item) is bool:
            continue
        if isinstance(item, str):
            if len(item) > limit:
                raise ValueError("JSON text exceeds review bounds")
        elif type(item) in (int, float):
            if type(item) is float and not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            if type(item) is int and item.bit_length() > 256:
                raise ValueError("JSON integer exceeds review bounds")
        elif type(item) is dict:
            if len(item) > 4096 or any(not isinstance(k, str) for k in item):
                raise ValueError("JSON objects require bounded string keys")
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) > 4096:
                raise ValueError("JSON array exceeds review bounds")
            stack.extend((child, depth + 1) for child in item)
        else:
            raise ValueError("only JSON values are accepted")
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                        allow_nan=False)
    try:
        size = len(result.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("JSON contains invalid Unicode") from exc
    if size > limit:
        raise ValueError("JSON document exceeds review bounds")
    return result


def subject_digest(subject):
    """Digest exact review task, claim inventory, evidence and version inputs."""
    if type(subject) is not dict:
        raise ValueError("review subject must be a JSON object")
    return hashlib.sha256(_json(subject).encode("utf-8")).hexdigest()


def _time(value):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("time must be an aware ISO timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be an aware datetime or ISO timestamp")
    return value.timestamp()


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _findings(values):
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("findings must be a bounded list")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 2048
           for value in values):
        raise ValueError("findings must be nonempty text, at most 2048 characters")
    _json(values, limit=65536)
    return list(values)


class ReviewStore:
    """File-backed SQLite workflow store; no network, model calls or side effects."""

    def __init__(self, db_path):
        if str(db_path) == ":memory:":
            raise ValueError("reviews require a persistent database file")
        self.path = Path(db_path).absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise ValueError("review database must be a regular file")
        else:
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS review_rounds (
                    round_id TEXT PRIMARY KEY, subject_json TEXT NOT NULL,
                    subject_sha256 TEXT NOT NULL, reviewers_json TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    last_event_at REAL NOT NULL, sealed_at REAL,
                    invalidated_at REAL, invalid_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS review_commitments (
                    round_id TEXT NOT NULL REFERENCES review_rounds(round_id),
                    reviewer_id TEXT NOT NULL, document TEXT NOT NULL,
                    commitment_sha256 TEXT NOT NULL,
                    PRIMARY KEY(round_id, reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS review_reveals (
                    round_id TEXT NOT NULL REFERENCES review_rounds(round_id),
                    reviewer_id TEXT NOT NULL, revealed_at REAL NOT NULL,
                    PRIMARY KEY(round_id, reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS review_audits (
                    round_id TEXT NOT NULL REFERENCES review_rounds(round_id),
                    reviewer_id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(round_id, reviewer_id)
                );
                CREATE TRIGGER IF NOT EXISTS review_commitment_no_update
                BEFORE UPDATE ON review_commitments BEGIN
                    SELECT RAISE(ABORT, 'commitments are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS review_commitment_no_delete
                BEFORE DELETE ON review_commitments BEGIN
                    SELECT RAISE(ABORT, 'commitments are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS review_audit_no_update
                BEFORE UPDATE ON review_audits BEGIN
                    SELECT RAISE(ABORT, 'audits are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS review_audit_no_delete
                BEFORE DELETE ON review_audits BEGIN
                    SELECT RAISE(ABORT, 'audits are immutable'); END;
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def create_round(self, round_id, subject, reviewer_ids, expires_at, *, now=None):
        _id(round_id, "round_id")
        reviewers = _ids(reviewer_ids, "reviewer_ids", minimum=2, maximum=16, reviewer=True)
        digest = subject_digest(subject)
        _ids(subject.get("required_claim_ids"), "required_claim_ids", minimum=1)
        if expires_at is None:
            raise ValueError("expires_at is required")
        timestamp, expiry = _time(now), _time(expires_at)
        if not timestamp < expiry <= timestamp + MAX_LIFETIME_SECONDS:
            raise ValueError("review lifetime must be positive and no more than 900 seconds")
        with self._transaction() as connection:
            try:
                connection.execute("""INSERT INTO review_rounds
                    (round_id,subject_json,subject_sha256,reviewers_json,created_at,
                     expires_at,last_event_at) VALUES (?,?,?,?,?,?,?)""",
                    (round_id, _json(subject), digest, _json(sorted(reviewers)),
                     timestamp, expiry, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ValueError("round_id already exists; stale round reuse is forbidden") from exc
        return {"round_id": round_id, "subject_sha256": digest,
                "expires_at": _stamp(expiry), "execution_authorized": False}

    @staticmethod
    def _round(connection, round_id):
        _id(round_id, "round_id")
        row = connection.execute("SELECT * FROM review_rounds WHERE round_id=?",
                                 (round_id,)).fetchone()
        if row is None:
            raise ValueError("unknown review round")
        return row

    @staticmethod
    def _check(row, digest, timestamp, reviewer_id=None):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ValueError("subject_sha256 must be a SHA256 digest")
        if digest != row["subject_sha256"]:
            raise ValueError("subject_changed: a new review round is required")
        if row["invalidated_at"] is not None:
            raise ValueError("review round is invalidated")
        if timestamp < row["last_event_at"]:
            raise ValueError("clock_before_latest_event")
        if timestamp >= row["expires_at"]:
            raise ValueError("review round expired")
        if reviewer_id is not None:
            _id(reviewer_id, "reviewer_id", reviewer=True)
            if reviewer_id not in json.loads(row["reviewers_json"]):
                raise ValueError("reviewer is not assigned to this round")

    @staticmethod
    def _touch(connection, round_id, timestamp):
        connection.execute("UPDATE review_rounds SET last_event_at=? WHERE round_id=?",
                           (timestamp, round_id))

    def phase_a(self, round_id, reviewer_id, subject_sha256, *, now=None):
        """Return task input and the caller's own submission status only."""
        timestamp = _time(now)
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            self._check(row, subject_sha256, timestamp, reviewer_id)
            own = connection.execute("""SELECT commitment_sha256 FROM review_commitments
                WHERE round_id=? AND reviewer_id=?""", (round_id, reviewer_id)).fetchone()
            return {"round_id": round_id, "reviewer_id": reviewer_id,
                    "subject_sha256": subject_sha256, "subject": json.loads(row["subject_json"]),
                    "submitted": own is not None,
                    "own_commitment_sha256": own[0] if own else None,
                    "execution_authorized": False}

    def commit(self, round_id, reviewer_id, subject_sha256, *, verdict,
               covered_claim_ids, findings=None, now=None):
        """Atomically freeze one phase-A assessment; retries cannot overwrite it."""
        timestamp = _time(now)
        if not isinstance(verdict, str) or verdict not in _VERDICTS:
            raise ValueError("verdict must be PASS, FAIL or ABSTAIN")
        claims = _ids(covered_claim_ids, "covered_claim_ids")
        notes = _findings([] if findings is None else findings)
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            self._check(row, subject_sha256, timestamp, reviewer_id)
            if row["sealed_at"] is not None:
                raise ValueError("phase A is sealed")
            required = set(json.loads(row["subject_json"])["required_claim_ids"])
            if not set(claims) <= required:
                raise ValueError("coverage contains a claim outside the bound subject")
            document = {"round_id": round_id, "reviewer_id": reviewer_id,
                        "subject_sha256": subject_sha256, "verdict": verdict,
                        "covered_claim_ids": sorted(claims), "findings": notes,
                        "committed_at": _stamp(timestamp)}
            encoded = _json(document)
            commitment = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            try:
                connection.execute("INSERT INTO review_commitments VALUES (?,?,?,?)",
                                   (round_id, reviewer_id, encoded, commitment))
            except sqlite3.IntegrityError as exc:
                raise ValueError("reviewer already committed; replacement is forbidden") from exc
            self._touch(connection, round_id, timestamp)
        return {"round_id": round_id, "reviewer_id": reviewer_id,
                "commitment_sha256": commitment, "execution_authorized": False}

    def seal(self, round_id, subject_sha256, *, now=None):
        timestamp = _time(now)
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            self._check(row, subject_sha256, timestamp)
            actual = {item[0] for item in connection.execute(
                "SELECT reviewer_id FROM review_commitments WHERE round_id=?", (round_id,))}
            if actual != set(json.loads(row["reviewers_json"])):
                raise ValueError("all assigned phase-A commitments are required before seal")
            if row["sealed_at"] is None:
                connection.execute("UPDATE review_rounds SET sealed_at=? WHERE round_id=?",
                                   (timestamp, round_id))
                self._touch(connection, round_id, timestamp)
        return {"round_id": round_id, "state": "SEALED", "execution_authorized": False}

    @staticmethod
    def _commitments(connection, round_id):
        return [dict(json.loads(item[0]), commitment_sha256=item[1])
                for item in connection.execute("""SELECT document, commitment_sha256
                FROM review_commitments WHERE round_id=? ORDER BY reviewer_id""", (round_id,))]

    def phase_b(self, round_id, reviewer_id, subject_sha256, *, now=None):
        """Reveal every immutable phase-A opinion only after a complete seal."""
        timestamp = _time(now)
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            self._check(row, subject_sha256, timestamp, reviewer_id)
            if row["sealed_at"] is None:
                raise ValueError("phase B is unavailable until all phase A is sealed")
            connection.execute("INSERT OR IGNORE INTO review_reveals VALUES (?,?,?)",
                               (round_id, reviewer_id, timestamp))
            self._touch(connection, round_id, timestamp)
            return {"round_id": round_id, "subject_sha256": subject_sha256,
                    "subject": json.loads(row["subject_json"]),
                    "commitments": self._commitments(connection, round_id),
                    "execution_authorized": False}

    def audit(self, round_id, reviewer_id, subject_sha256, *, verdict,
              findings=None, now=None):
        """Freeze the second-phase audit; it cannot replace phase-A disagreement."""
        timestamp = _time(now)
        if not isinstance(verdict, str) or verdict not in _VERDICTS:
            raise ValueError("verdict must be PASS, FAIL or ABSTAIN")
        notes = _findings([] if findings is None else findings)
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            self._check(row, subject_sha256, timestamp, reviewer_id)
            revealed = connection.execute("""SELECT 1 FROM review_reveals
                WHERE round_id=? AND reviewer_id=?""", (round_id, reviewer_id)).fetchone()
            if row["sealed_at"] is None or revealed is None:
                raise ValueError("reviewer must retrieve sealed phase B before auditing")
            commitments = self._commitments(connection, round_id)
            document = {"round_id": round_id, "reviewer_id": reviewer_id,
                        "subject_sha256": subject_sha256, "verdict": verdict,
                        "findings": notes, "audited_at": _stamp(timestamp),
                        "commitment_sha256s": [item["commitment_sha256"] for item in commitments]}
            try:
                connection.execute("INSERT INTO review_audits VALUES (?,?,?)",
                                   (round_id, reviewer_id, _json(document)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("reviewer already audited; replacement is forbidden") from exc
            self._touch(connection, round_id, timestamp)
        return {"round_id": round_id, "reviewer_id": reviewer_id,
                "state": "AUDIT_RECORDED", "execution_authorized": False}

    def invalidate(self, round_id, reason, *, now=None):
        """Irreversibly retire a round when upstream inputs/authority change."""
        _findings([reason])
        timestamp = _time(now)
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            if timestamp < row["last_event_at"]:
                raise ValueError("clock_before_latest_event")
            if row["invalidated_at"] is None:
                connection.execute("""UPDATE review_rounds SET invalidated_at=?,
                    invalid_reason=?,last_event_at=? WHERE round_id=?""",
                    (timestamp, reason, timestamp, round_id))
        return {"round_id": round_id, "state": "INVALIDATED", "execution_authorized": False}

    def evaluate(self, round_id, subject_sha256, *, now=None):
        """Diagnostic result; READY always means ready for human review only."""
        timestamp = _time(now)
        result = {"round_id": round_id, "state": "HOLD", "reasons": [],
                  "execution_authorized": False, "reviewer_identity_authenticated": False,
                  "external_blindness_proven": False, "phase_b_complete": False}
        with self._transaction() as connection:
            row = self._round(connection, round_id)
            result["reviewer_ids"] = json.loads(row["reviewers_json"])
            result["subject_sha256"] = row["subject_sha256"]
            try:
                self._check(row, subject_sha256, timestamp)
            except ValueError as exc:
                result["reasons"] = [str(exc)]
                return result
            commitments = self._commitments(connection, round_id)
            expected = set(json.loads(row["reviewers_json"]))
            if row["sealed_at"] is None:
                # Do not leak early opinions through aggregate status or reasons.
                result["state"] = "WAITING_FOR_PHASE_A" if len(commitments) < len(expected) else "WAITING_FOR_SEAL"
                result["reasons"] = ["phase_a_not_sealed"]
                return result
            audits = [json.loads(item[0]) for item in connection.execute(
                "SELECT document FROM review_audits WHERE round_id=? ORDER BY reviewer_id", (round_id,))]
            required = set(json.loads(row["subject_json"])["required_claim_ids"])
            if {item["reviewer_id"] for item in commitments} != expected:
                result["reasons"].append("phase_a_incomplete")
            if len({item["verdict"] for item in commitments}) > 1:
                result["reasons"].append("phase_a_disagreement")
            for item in commitments:
                if item["verdict"] != "PASS":
                    result["reasons"].append("phase_a_" + item["verdict"].lower())
                if set(item["covered_claim_ids"]) != required:
                    result["reasons"].append("phase_a_claim_coverage_incomplete")
                if item["findings"]:
                    result["reasons"].append("phase_a_unresolved_findings")
            if {item["reviewer_id"] for item in audits} != expected:
                result["reasons"].append("phase_b_incomplete")
            else:
                result["phase_b_complete"] = True
            for item in audits:
                if item["verdict"] != "PASS":
                    result["reasons"].append("phase_b_" + item["verdict"].lower())
                if item["findings"]:
                    result["reasons"].append("phase_b_unresolved_findings")
            result["reasons"] = sorted(set(result["reasons"]))
            result["phase_a_commitment_sha256s"] = [item["commitment_sha256"] for item in commitments]
            result["phase_b_completed"] = len(audits)
            if not result["reasons"]:
                result["state"] = "READY_FOR_HUMAN_REVIEW"
        return result
