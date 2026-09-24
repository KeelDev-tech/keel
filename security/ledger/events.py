"""Security ledger: append-only, hash-chained security event log.

Successful record() calls durably append one decision event under a shared
advisory lock. All writers must use this class and the same stable lock file.
The chain detects malformed or altered retained records. Without an external
trusted anchor it cannot detect deletion of a valid suffix or complete rewrite.
The inherited recorded_at envelope field is not hash-covered.

A partial append is preserved and blocks later writes; repair requires an
explicit recovery procedure. No automatic truncation or history rewrite occurs.

Default path: ~/workspace/keel/hidden_files/security/security_events.jsonl
(env KEEL_SECURITY_LEDGER overrides — tests must use a tmp path).
"""

from __future__ import annotations

import json
import math
import os
import fcntl
import stat
from contextlib import contextmanager
from datetime import datetime, timezone

from ..policy_version import POLICY_VERSION
from .hashchain import GENESIS_PREV, HashChain, record_hash

DEFAULT_LEDGER_PATH = os.path.join(
    os.path.expanduser("~"), "workspace", "keel", "hidden_files",
    "security", "security_events.jsonl")
LEDGER_ENV = "KEEL_SECURITY_LEDGER"
MAX_RECORD_BYTES = 8 * 1024 * 1024


class LedgerIntegrityError(OSError):
    """The retained ledger cannot authorize another durable append."""


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON value")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON value")
    return result


def _default_path() -> str:
    return os.environ.get(LEDGER_ENV, DEFAULT_LEDGER_PATH)


class SecurityLedger(HashChain):
    """File-backed hash-chained ledger of security decisions."""

    def __init__(self, path: str | None = None):
        super().__init__()
        self.path = os.path.abspath(path or _default_path())
        self._integrity_error = ""
        self._load()

    @contextmanager
    def _directory(self, *, create: bool):
        """Walk directory descriptors without following symlink components."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(os.path.sep, flags)
        try:
            for part in os.path.dirname(self.path).split(os.path.sep):
                if not part:
                    continue
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _check_file(fd, parent, name):
        current = os.fstat(fd)
        try:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise LedgerIntegrityError("ledger or lock disappeared during access") from exc
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                not stat.S_ISREG(named.st_mode) or
                (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)):
            raise LedgerIntegrityError("ledger or lock is not a stable regular file")

    @contextmanager
    def _locked(self, *, create: bool):
        with self._directory(create=create) as parent:
            name = os.path.basename(self.path)
            lock_name = name + ".lock"
            safe_flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            lock = os.open(lock_name, os.O_RDWR | os.O_CREAT | safe_flags,
                           0o600, dir_fd=parent)
            try:
                self._check_file(lock, parent, lock_name)
                fcntl.flock(lock, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
                self._check_file(lock, parent, lock_name)
                fd = None
                try:
                    flags = (os.O_RDWR | os.O_APPEND | os.O_CREAT
                             if create else os.O_RDONLY)
                    try:
                        fd = os.open(name, flags | safe_flags, 0o600, dir_fd=parent)
                    except FileNotFoundError:
                        if create:
                            raise
                    if fd is not None:
                        self._check_file(fd, parent, name)
                    yield parent, fd
                    self._check_file(lock, parent, lock_name)
                    if fd is not None:
                        self._check_file(fd, parent, name)
                finally:
                    if fd is not None:
                        os.close(fd)
                    fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                os.close(lock)

    @staticmethod
    def _read_verified(fd):
        records = []
        if fd is None:
            return records
        os.lseek(fd, 0, os.SEEK_SET)
        previous = GENESIS_PREV
        with os.fdopen(os.dup(fd), "rb") as stream:
            while True:
                raw = stream.readline(MAX_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise LedgerIntegrityError("oversized or incomplete ledger record")
                try:
                    rec = json.loads(raw.decode("utf-8"),
                                     object_pairs_hook=_json_object,
                                     parse_constant=_invalid_constant,
                                     parse_float=_finite_float)
                    if (type(rec) is not dict or
                            set(rec) != {"seq", "prev_hash", "recorded_at", "body", "hash"} or
                            type(rec["seq"]) is not int or rec["seq"] != len(records) or
                            rec["prev_hash"] != previous or type(rec["body"]) is not dict or
                            type(rec["recorded_at"]) is not str or not rec["recorded_at"] or
                            type(rec["hash"]) is not str or
                            rec["hash"] != record_hash(rec["seq"], previous, rec["body"])):
                        raise ValueError("invalid chain envelope")
                except (ValueError, TypeError, KeyError, RecursionError) as exc:
                    raise LedgerIntegrityError(
                        f"invalid security ledger record at index {len(records)}") from exc
                records.append(rec)
                previous = rec["hash"]
        return records

    def _load(self) -> None:
        try:
            with self._locked(create=False) as (_, fd):
                records = self._read_verified(fd)
        except FileNotFoundError:
            records = []
        except OSError as exc:
            self._records = []
            self._integrity_error = str(exc)
            return
        self._records = records
        self._integrity_error = ""

    @staticmethod
    def _persist(record: dict, fd: int, parent: int) -> None:
        data = (json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > MAX_RECORD_BYTES:
            raise LedgerIntegrityError("oversized security ledger record")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("security ledger write made no progress")
            view = view[written:]
        os.fsync(fd)
        # Also persist the directory entry, including on the first append.
        os.fsync(parent)

    def record(self, *, agent_id: str, parent_agent_id: str = "",
               requested_capability: str | None = None,
               action_name: str = "", action_class=None,
               policy_version: str = POLICY_VERSION,
               input_hashes: dict | None = None, decision,
               reasons: list | None = None, risk_score: int = 0,
               risk_factors: list | None = None,
               resulting_action: str = "", result_hash: str = "",
               approval_id: str = "",
               redacted_input_preview: str = "") -> dict:
        """Append one security event. Returns the chained record."""
        from ..policy.engine import Decision
        body = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "parent_agent_id": parent_agent_id,
            "requested_capability": requested_capability,
            "action_name": action_name,
            "action_class": (action_class.value if action_class is not None
                             else ""),
            "policy_version": policy_version,
            "input_hashes": input_hashes or {},
            "decision": (decision.value if isinstance(decision, Decision)
                         else str(decision)),
            "reasons": list(reasons or []),
            "risk_score": risk_score,
            "risk_factors": list(risk_factors or []),
            "resulting_action": resulting_action,
            "result_hash": result_hash,
            "approval_id": approval_id,
            # Redacted preview only — raw inputs are hashed, never stored.
            "redacted_input_preview": redacted_input_preview,
        }
        try:
            with self._locked(create=True) as (parent, fd):
                # Instances may have been created before another process wrote.
                # Authority derives exclusively from the freshly verified file.
                self._records = self._read_verified(fd)
                self._integrity_error = ""
                rec = super().append(body)
                self._persist(rec, fd, parent)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            # Never claim a failed fsync or partial append authorized an effect.
            # Do not erase bytes that may have reached the file before failure.
            self._records = []
            self._integrity_error = "security ledger append failed"
            raise OSError("security ledger write failed; refusing decision") from exc
        return rec

    def record_system_event(self, *, action_name: str, decision: str,
                            reasons: list | None = None,
                            input_hashes: dict | None = None) -> dict:
        """Ledger entry for authority-side actions (quarantine, safe mode)."""
        return self.record(
            agent_id="keel-security-authority", action_name=action_name,
            decision=decision, reasons=reasons, input_hashes=input_hashes,
            resulting_action="authority_action")

    def verify(self) -> tuple[bool, str]:
        self._load()
        if self._integrity_error:
            return False, self._integrity_error
        return True, f"ledger ok: {len(self._records)} events"
