"""Private, transactional source history for an explicitly configured local host.

Hashes detect changed stored records. They do not authenticate upstream facts or
protect against the OS account that owns this database rewriting the database.
No method grants permission to execute an application action.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from keel_agent.revisions import (
    COMPONENTS, PREAPPROVAL_COMPONENTS, SCHEMA, _canonical, _material_record,
    _time, _token, revision_digest,
)


class SourceStoreError(ValueError):
    """A fixed diagnostic, containing no answer values or attachment content."""


def _require(condition, code):
    if not condition:
        raise SourceStoreError(code)


def _json(value):
    return _canonical(value).decode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require(path.resolve(strict=True) == path.absolute(), "directory_symlink")
    info = path.stat()
    _require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700,
             "directory_not_private")
    _require(not hasattr(os, "getuid") or info.st_uid == os.getuid(), "directory_owner")


def _private_file(path):
    _require(path.resolve(strict=True) == path.absolute(), "database_symlink")
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o600, "database_not_private")
    _require(not hasattr(os, "getuid") or info.st_uid == os.getuid(), "database_owner")


class _Connection(sqlite3.Connection):
    pass


class SourceStore:
    """Append-only versions, scoped compare-and-swap, and consistent exports.

    ``clock`` and ``now`` are trusted host inputs, never model-produced time.
    Transactions use BEGIN IMMEDIATE to serialize writers and approval decisions.
    Source observation times are only supplied by the actual source producer.
    """

    def __init__(self, home, workspace_id, clock=None):
        self.home = Path(home).absolute()
        self.workspace_id = _token(workspace_id)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db_path = self.home / "sources.sqlite3"
        self.identity_path = self.home / "store.identity"
        self.attachment_root = self.home / "attachments"
        self._connection_token = object()
        _private_directory(self.home)
        _private_directory(self.attachment_root)
        created = False
        has_database = self.db_path.exists() or self.db_path.is_symlink()
        has_identity = self.identity_path.exists() or self.identity_path.is_symlink()
        _require(not has_database or has_identity, "store_identity_missing")
        _require(not has_identity or has_database, "source_database_missing")
        if not has_database:
            identity = {"schema": "keel.source_store_identity.v1", "workspace_id": self.workspace_id,
                        "store_id": secrets.token_hex(16)}
            try:
                descriptor = os.open(self.identity_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                     getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                raise SourceStoreError("store_initialization_conflict") from None
            try:
                raw = _canonical(identity)
                offset = 0
                while offset < len(raw):
                    offset += os.write(descriptor, raw[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                     getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                raise SourceStoreError("store_initialization_conflict") from None
            os.close(descriptor)
            directory = os.open(self.home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            created = True
        self.store_id = self._read_identity()["store_id"]
        _private_file(self.db_path)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if created:
                statements = [
                    "CREATE TABLE source_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "CREATE TABLE source_scopes (scope_id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                    "CREATE TABLE source_records (scope_id TEXT NOT NULL, component TEXT NOT NULL, generation INTEGER NOT NULL, descriptor_json TEXT NOT NULL, descriptor_sha256 TEXT NOT NULL, revision TEXT NOT NULL, source_ref TEXT NOT NULL, source_version TEXT NOT NULL, observed_at TEXT NOT NULL, created_at TEXT NOT NULL, row_sha256 TEXT NOT NULL, PRIMARY KEY(scope_id,component,generation))",
                    "CREATE TABLE source_heads (scope_id TEXT NOT NULL, component TEXT NOT NULL, generation INTEGER NOT NULL, PRIMARY KEY(scope_id,component))",
                    "CREATE TABLE source_versions (scope_id TEXT NOT NULL, component TEXT NOT NULL, source_ref TEXT NOT NULL, source_version TEXT NOT NULL, revision TEXT NOT NULL, revoked INTEGER NOT NULL, PRIMARY KEY(scope_id,component,source_ref,source_version))",
                    "CREATE TABLE source_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, body_json TEXT NOT NULL, previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL)",
                ]
                for statement in statements:
                    connection.execute(statement)
                connection.executemany("INSERT INTO source_meta(key,value) VALUES(?,?)", [
                    ("schema", "keel.source_store.v1"), ("workspace_id", self.workspace_id),
                    ("last_clock", "1970-01-01T00:00:00+00:00"), ("snapshot_sequence", "0"),
                    ("store_id", self.store_id),
                ])
            self._verify_meta(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_identity(self):
        _require(self.identity_path.exists() or self.identity_path.is_symlink(), "store_identity_missing")
        _private_file(self.identity_path)
        descriptor = os.open(self.identity_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                             getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(descriptor)
            _require(stat.S_ISREG(info.st_mode) and info.st_size <= 4096, "store_identity_invalid")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        try:
            identity = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SourceStoreError("store_identity_invalid") from None
        _require(isinstance(identity, dict) and set(identity) == {"schema", "workspace_id", "store_id"}
                 and identity["schema"] == "keel.source_store_identity.v1" and
                 _canonical(identity) == raw, "store_identity_invalid")
        _require(identity["workspace_id"] == self.workspace_id, "workspace_mismatch")
        store_id = identity["store_id"]
        _require(isinstance(store_id, str) and len(store_id) == 32 and set(store_id) <= set("0123456789abcdef"),
                 "store_identity_invalid")
        return identity

    def _connect(self, *, read_only=False):
        _require(self._read_identity()["store_id"] == self.store_id, "store_identity_mismatch")
        _require(self.db_path.exists() or self.db_path.is_symlink(), "source_database_missing")
        _private_file(self.db_path)
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro" if read_only else self.db_path,
                                     uri=read_only, timeout=15, isolation_level=None, factory=_Connection)
        connection.row_factory = sqlite3.Row
        if not read_only:
            connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _verify_meta(self, connection):
        try:
            values = dict(connection.execute("SELECT key,value FROM source_meta"))
        except sqlite3.DatabaseError:
            raise SourceStoreError("database_schema_invalid") from None
        _require(values.get("schema") == "keel.source_store.v1", "database_schema_invalid")
        _require(values.get("workspace_id") == self.workspace_id, "workspace_mismatch")
        _require(set(values) == {"schema", "workspace_id", "last_clock", "snapshot_sequence", "store_id"},
                 "database_metadata_invalid")
        _require(values["store_id"] == self.store_id, "store_identity_mismatch")
        _time(values["last_clock"])
        _require(values["snapshot_sequence"].isdigit(), "database_sequence_invalid")
        return values

    def _now(self, now):
        now = self.clock() if now is None else now
        _require(isinstance(now, datetime) and now.utcoffset() is not None, "clock_invalid")
        return now.astimezone(timezone.utc)

    @contextmanager
    def transaction(self, now=None):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            metadata = self._verify_meta(connection)
            now = self._now(now)
            _require(now >= _time(metadata["last_clock"]), "clock_rollback")
            connection.keel_now = now
            connection.keel_store_token = self._connection_token
            yield connection
            connection.execute("UPDATE source_meta SET value=? WHERE key='last_clock'",
                               (now.isoformat(),))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_transaction(self, now=None):
        """Consistent read without updating a clock or writing journal/database bytes.

        Reads reject clocks older than the last committed mutation. They do not
        advance the durable clock watermark, so observation-only exports remain
        truly read-only. Decision transactions always use ``transaction``.
        """
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            metadata = self._verify_meta(connection)
            now = self._now(now)
            _require(now >= _time(metadata["last_clock"]), "clock_rollback")
            connection.keel_now = now
            connection.keel_store_token = self._connection_token
            yield connection
            connection.rollback()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connection(self, connection):
        _require(isinstance(connection, _Connection) and connection.in_transaction and
                 getattr(connection, "keel_store_token", None) is self._connection_token,
                 "transaction_required")

    def _scope(self, scope):
        _require(isinstance(scope, dict) and set(scope) == {
            "workspace_id", "role_id", "application_id", "action"}, "scope_invalid")
        normalized = {key: _token(value) for key, value in scope.items()}
        _require(normalized["workspace_id"] == self.workspace_id, "workspace_mismatch")
        return normalized

    def _scope_id(self, scope):
        return _digest(self._scope(scope))

    def _registered(self, connection, scope):
        self._connection(connection)
        scope = self._scope(scope)
        scope_id = _digest(scope)
        row = connection.execute("SELECT scope_json FROM source_scopes WHERE scope_id=?",
                                 (scope_id,)).fetchone()
        _require(row is not None, "scope_not_registered")
        _require(row["scope_json"] == _json(scope), "scope_integrity_failed")
        return scope_id

    def register_scope(self, scope, *, now=None):
        scope = self._scope(scope)
        with self.transaction(now) as connection:
            scope_id = _digest(scope)
            old = connection.execute("SELECT scope_json FROM source_scopes WHERE scope_id=?",
                                     (scope_id,)).fetchone()
            if old is not None:
                _require(old["scope_json"] == _json(scope), "scope_integrity_failed")
                return {"scope": scope, "created": False, "execution_authorized": False}
            connection.execute("INSERT INTO source_scopes VALUES(?,?,?)",
                               (scope_id, _json(scope), connection.keel_now.isoformat()))
            connection.execute("UPDATE source_meta SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_sequence'")
            self.audit_event(connection, "SCOPE_REGISTERED", scope)
        return {"scope": scope, "created": True, "execution_authorized": False}

    def current_generation(self, connection, scope, component):
        _require(component in COMPONENTS, "component_invalid")
        scope_id = self._registered(connection, scope)
        row = connection.execute("SELECT generation FROM source_heads WHERE scope_id=? AND component=?",
                                 (scope_id, component)).fetchone()
        history = connection.execute("SELECT MAX(generation),COUNT(*) FROM source_records WHERE scope_id=? AND component=?",
                                     (scope_id, component)).fetchone()
        generation = 0 if row is None else row["generation"]
        _require(type(generation) is int and generation >= 0 and
                 generation == (history[0] or 0) == history[1], "source_head_integrity_failed")
        return generation

    def _read_record(self, connection, scope, component, generation):
        scope_id = self._registered(connection, scope)
        row = connection.execute("SELECT * FROM source_records WHERE scope_id=? AND component=? AND generation=?",
                                 (scope_id, component, generation)).fetchone()
        _require(row is not None, "source_history_missing")
        values = dict(row)
        expected_hash = values.pop("row_sha256")
        _require(_digest(values) == expected_hash, "source_row_integrity_failed")
        try:
            descriptor = json.loads(row["descriptor_json"])
        except (ValueError, TypeError):
            raise SourceStoreError("source_json_invalid") from None
        _require(_digest(descriptor) == row["descriptor_sha256"], "source_integrity_failed")
        _require(_json(descriptor) == row["descriptor_json"], "source_json_noncanonical")
        material = self._material(component, descriptor, scope)
        _require(self._revision(scope, component, descriptor, material) == row["revision"],
                 "source_material_integrity_failed")
        return descriptor, row

    def read_scope(self, connection, scope):
        scope = self._scope(scope)
        self._registered(connection, scope)
        sources, generations = {}, {}
        for component in COMPONENTS:
            generation = self.current_generation(connection, scope, component)
            generations[component] = generation
            sources[component] = (self._read_record(connection, scope, component, generation)[0]
                                  if generation else {"absence": {"kind": "SOURCE_RECORD_MISSING"}})
        return {"scope": scope, "sources": sources, "generations": generations}

    def _material(self, component, descriptor, scope):
        if "absence" in descriptor:
            return {"absence": descriptor["absence"]}
        return _material_record(component, descriptor.get("record"), scope, self.attachment_root)

    def _revision(self, scope, component, descriptor, material):
        return revision_digest(scope, component, descriptor, material)

    def _validate_descriptor(self, component, descriptor, scope, now, allow_approval):
        _require(isinstance(descriptor, dict), "source_descriptor_invalid")
        _canonical(descriptor)
        required = {"source_ref", "source_version", "observed_at", "expires_at"}
        _require(required <= set(descriptor) and set(descriptor) <= required | {"record", "absence", "revoked"},
                 "source_descriptor_fields_invalid")
        _token(descriptor["source_ref"])
        _token(descriptor["source_version"])
        observed, expires = _time(descriptor["observed_at"]), _time(descriptor["expires_at"])
        _require(observed <= now, "source_observation_in_future")
        _require(expires > observed, "source_validity_invalid")
        _require("revoked" not in descriptor or type(descriptor["revoked"]) is bool,
                 "source_revocation_invalid")
        _require(("record" in descriptor) != ("absence" in descriptor), "source_record_or_absence_required")
        if component != "approval":
            _require("record" in descriptor, "source_record_required")
            from .capture import validate_source
            validate_source(component, descriptor, scope=scope, attachment_root=self.attachment_root,
                            now=now, allow_inactive=True)
        else:
            _require(allow_approval, "approval_requires_decision_workflow")
            if "absence" in descriptor:
                absence = descriptor["absence"]
                _require(isinstance(absence, dict) and set(absence) == {"kind", "decision_request_ref"}
                         and absence["kind"] == "HUMAN_DECISION_REQUIRED", "approval_absence_invalid")
                _token(absence["decision_request_ref"])
            else:
                record = descriptor["record"]
                _require(isinstance(record, dict), "approval_record_invalid")
                for key in ("approval_id", "actor_id", "authority_record_ref"):
                    _token(record.get(key))
                _require(record.get("decision") in {"APPROVE", "REJECT"}, "approval_decision_invalid")
                _require(record.get("scope") == scope, "approval_scope_mismatch")
                _require(type(record.get("revoked")) is bool, "approval_revocation_invalid")
                bindings = record.get("component_revisions")
                _require(isinstance(bindings, dict) and set(bindings) == set(PREAPPROVAL_COMPONENTS),
                         "approval_binding_invalid")
                _require(all(isinstance(v, str) and len(v) == 64 and set(v) <= set("0123456789abcdef")
                             for v in bindings.values()), "approval_binding_invalid")
                approved, expiry = _time(record.get("approved_at")), _time(record.get("expires_at"))
                _require(approved <= observed and expiry > approved, "approval_validity_invalid")
        return self._material(component, descriptor, scope)

    def put_source(self, scope, component, descriptor, *, expected_generation, now=None):
        _require(component in PREAPPROVAL_COMPONENTS, "approval_requires_decision_workflow")
        with self.transaction(now) as connection:
            return self.write_source(connection, scope, component, descriptor, expected_generation)

    def write_source(self, connection, scope, component, descriptor, expected_generation, allow_approval=False):
        self._connection(connection)
        _require(component in COMPONENTS, "component_invalid")
        _require(component != "approval" or allow_approval, "approval_requires_decision_workflow")
        _require(type(expected_generation) is int and expected_generation >= 0, "generation_invalid")
        scope = self._scope(scope)
        scope_id = self._registered(connection, scope)
        generation = self.current_generation(connection, scope, component)
        _require(generation == expected_generation, "generation_conflict")
        material = self._validate_descriptor(component, descriptor, scope, connection.keel_now, allow_approval)
        descriptor = json.loads(_json(descriptor))
        revision = self._revision(scope, component, descriptor, material)
        prior = connection.execute("SELECT revision,revoked FROM source_versions WHERE scope_id=? AND component=? AND source_ref=? AND source_version=?",
                                   (scope_id, component, descriptor["source_ref"], descriptor["source_version"])).fetchone()
        revoked = descriptor.get("revoked") is True or descriptor.get("record", {}).get("revoked") is True
        if generation:
            previous, _ = self._read_record(connection, scope, component, generation)
            _require(_time(descriptor["observed_at"]) >= _time(previous["observed_at"]), "source_observation_rollback")
            same_version = (descriptor["source_ref"], descriptor["source_version"]) == (
                previous["source_ref"], previous["source_version"])
            if prior is not None:
                _require(same_version, "source_version_replay")
                _require(prior["revision"] == revision, "source_version_material_conflict")
                _require(not prior["revoked"] or revoked, "source_revocation_rollback")
                _require(descriptor != previous, "source_replay")
                # A newly recorded revocation can occur at the same source clock tick.
                new_revocation = revoked and not prior["revoked"]
                _require(new_revocation or _time(descriptor["observed_at"]) > _time(previous["observed_at"]),
                         "source_reobservation_required")
        else:
            _require(prior is None, "source_history_conflict")
        generation += 1
        values = {"scope_id": scope_id, "component": component, "generation": generation,
                  "descriptor_json": _json(descriptor), "descriptor_sha256": _digest(descriptor),
                  "revision": revision, "source_ref": descriptor["source_ref"],
                  "source_version": descriptor["source_version"], "observed_at": descriptor["observed_at"],
                  "created_at": connection.keel_now.isoformat()}
        values["row_sha256"] = _digest(values)
        connection.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(values.values()))
        connection.execute("INSERT INTO source_heads VALUES(?,?,?) ON CONFLICT(scope_id,component) DO UPDATE SET generation=excluded.generation",
                           (scope_id, component, generation))
        connection.execute("INSERT INTO source_versions VALUES(?,?,?,?,?,?) ON CONFLICT(scope_id,component,source_ref,source_version) DO UPDATE SET revoked=MAX(revoked,excluded.revoked)",
                           (scope_id, component, descriptor["source_ref"], descriptor["source_version"], revision, int(revoked)))
        connection.execute("UPDATE source_meta SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_sequence'")
        self.audit_event(connection, "SOURCE_CAPTURED", scope, component, generation,
                         {"descriptor_sha256": values["descriptor_sha256"], "revision": revision,
                          "source_ref": descriptor["source_ref"], "source_version": descriptor["source_version"]})
        return {"generation": generation, "revision": revision, "changed": True, "execution_authorized": False}

    def snapshot_in_transaction(self, connection, scopes=None):
        self._connection(connection)
        if scopes is None:
            scopes = []
            for row in connection.execute("SELECT scope_id,scope_json FROM source_scopes ORDER BY scope_id"):
                scope = json.loads(row["scope_json"])
                _require(self._scope_id(scope) == row["scope_id"], "scope_integrity_failed")
                scopes.append(scope)
        _require(isinstance(scopes, (tuple, list)), "scopes_invalid")
        identifiers = [self._scope_id(scope) for scope in scopes]
        _require(len(identifiers) == len(set(identifiers)), "duplicate_scope")
        roles = []
        for scope in scopes:
            current = self.read_scope(connection, scope)
            roles.append({key: current["scope"][key] for key in ("role_id", "application_id", "action")} |
                         {"sources": current["sources"]})
        metadata = self._verify_meta(connection)
        now = connection.keel_now
        return {"schema": SCHEMA, "workspace_id": self.workspace_id,
                "snapshot": {"source_ref": "local-source-store:" + self.store_id,
                             "source_version": "generation:" + metadata["snapshot_sequence"],
                             "observed_at": now.isoformat(), "expires_at": (now + timedelta(seconds=90)).isoformat()},
                "roles": roles}

    def export_snapshot(self, scopes=None, *, now=None):
        with self.read_transaction(now) as connection:
            return self.snapshot_in_transaction(connection, scopes)

    def audit_event(self, connection, kind, scope=None, component=None, generation=None, details=None):
        self._connection(connection)
        _token(kind)
        _require(component is None or component in COMPONENTS, "component_invalid")
        allowed = {"request_id", "decision", "actor_id", "reviewed_sha256", "authority_record_ref", "reason",
                   "approval_id", "source_ref", "source_version", "descriptor_sha256", "revision"}
        details = {} if details is None else details
        _require(isinstance(details, dict) and set(details) <= allowed and all(
            isinstance(value, str) and 0 < len(value) <= 512 for value in details.values()), "audit_metadata_invalid")
        body = {"kind": kind, "scope_id": self._scope_id(scope) if scope else None,
                "component": component, "generation": generation, "details": details,
                "observed_at": connection.keel_now.isoformat()}
        row = connection.execute("SELECT event_sha256 FROM source_events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        event_hash = _digest({"previous_sha256": previous, "body": body})
        connection.execute("INSERT INTO source_events(body_json,previous_sha256,event_sha256) VALUES(?,?,?)",
                           (_json(body), previous, event_hash))

    def events(self, *, after=0, limit=100, now=None):
        _require(type(after) is int and after >= 0 and type(limit) is int and 1 <= limit <= 1000,
                 "event_pagination_invalid")
        with self.read_transaction(now) as connection:
            previous, output = "0" * 64, []
            for row in connection.execute("SELECT * FROM source_events ORDER BY sequence"):
                body = json.loads(row["body_json"])
                _require(row["previous_sha256"] == previous and row["event_sha256"] ==
                         _digest({"previous_sha256": previous, "body": body}), "audit_integrity_failed")
                previous = row["event_sha256"]
                if row["sequence"] > after and len(output) < limit:
                    output.append({"sequence": row["sequence"], **body, "event_sha256": previous})
            return output
