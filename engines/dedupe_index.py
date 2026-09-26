"""Bounded local posting identity index; similarity is never proof of identity.

The index is a disposable projection of explicitly configured JSON sources.
Source hashes, rather than timestamps alone, determine freshness. An unavailable
source makes coverage incomplete; stale/corrupt indexes cannot declare freshness.
This is an intake aid, not submission authorization or provider authentication.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Callable
from urllib.parse import urlsplit, parse_qsl

try:
    from .dedupe_gate import URL_FIELDS, canonical_url, _norm_name, _norm_title
    from .posting_identity import identity
except ImportError:  # Script/legacy engines-path entry point.
    from dedupe_gate import URL_FIELDS, canonical_url, _norm_name, _norm_title
    from posting_identity import identity

MAX_SOURCES = 64
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ROW_BYTES = 32 * 1024
MAX_URLS = 32


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _now(clock):
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid host clock")
    return value


def _regular_bytes(path, limit=MAX_BYTES):
    """No symlink leaves or ancestors; reject mutation during the bounded read."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink source prohibited")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit or before.st_nlink != 1:
            raise ValueError("source must be a bounded regular file")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("source size exceeds limit")
        after = os.fstat(fd)
        sig = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if sig(before) != sig(after) or sig(after) != sig(os.stat(path, follow_symlinks=False)):
            raise ValueError("source changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


@dataclass(frozen=True)
class Source:
    source_id: str
    path: str
    kind: str = "queue"

    def __post_init__(self):
        if (not isinstance(self.source_id, str) or not self.source_id or len(self.source_id) > 160
                or self.kind not in {"queue", "ledger"} or not isinstance(self.path, str)
                or not Path(self.path).is_absolute()):
            raise ValueError("source requires unique ID, absolute path and queue/ledger kind")


def keys_for_urls(values):
    """All exact IDs plus conservative URL keys; preserve path and ID case.

    A recognized posting ID has one canonical key across /apply and embed URLs.
    Unknown URLs retain every query parameter except explicit tracking keys.
    Generic receipt/confirmation URLs are not posting identity evidence.
    """
    if isinstance(values, dict):
        items = [(k, values.get(k)) for k in URL_FIELDS]
    elif isinstance(values, (list, tuple)):
        items = [("url", v) for v in values]
    else:
        raise ValueError("candidate URLs must be a field mapping or list")
    if len(items) > MAX_URLS:
        raise ValueError("too many candidate URLs")
    keys, exact, generic = set(), set(), []
    for field, value in items:
        cu = canonical_url(value)
        if not cu:
            continue
        try:
            ident = identity(cu)
        except ValueError:
            ident = None
        if ident:
            key = "posting:" + _json(ident)
            keys.add(key)
            exact.add(key)
        elif field != "confirmation_url":
            parsed = urlsplit(cu)
            tail = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
            query_id = any(k.lower() in {"job", "jobid", "job_id", "gh_jid", "posting_id", "reqid"}
                           and bool(v) for k, v in parse_qsl(parsed.query))
            if query_id or tail not in {"", "jobs", "careers", "apply", "application", "confirmation", "confirm", "thank-you", "thanks", "success"}:
                generic.append((field, "url:" + cu))
    # Distinct provider IDs in one candidate/row are ambiguous, never a union.
    # Generic URLs can be shared company application endpoints. A known exact
    # posting ID takes precedence, so generic URLs cannot merge different IDs.
    if not exact:
        dedicated = {key for field, key in generic if field in {"posting_url", "job_url", "jobUrl", "absolute_url"}}
        keys.update(dedicated or {key for _, key in generic})
    return keys, len(keys) > 1


def _record_evidence(row, kind, source_id, match, key):
    prefix = "ledger" if kind == "ledger" else "queue"
    return {"kind": "duplicate_of_submitted" if kind == "ledger" else "duplicate_in_queue",
            "match": match, prefix + "_role_id": row.get("role_id"),
            prefix + "_company": row.get("company") or row.get("employer"),
            prefix + "_status": row.get("status"), "source_id": source_id,
            "identity_key": key, "execution_authorized": False}


def check_rows(company, title, urls, ledger_rows, queue_entries):
    """Explicit snapshots: caller owns coverage/freshness of these supplied rows."""
    keys, ambiguous = keys_for_urls(urls)
    if ambiguous:
        return "suspect", {"kind": "ambiguous_candidate_identity", "execution_authorized": False}
    if not keys:
        return "suspect", {"kind": "candidate_identity_missing", "execution_authorized": False}
    nc, nt = _norm_name(company), _norm_title(title)
    advisory, unresolved = None, False
    for kind, rows in (("ledger", ledger_rows), ("queue", queue_entries)):
        for row in rows or []:
            if not isinstance(row, dict) or (kind == "ledger" and row.get("status") != "SUBMITTED"):
                continue
            other, conflict = keys_for_urls(row)
            unresolved = unresolved or conflict or not other
            overlap = keys & other
            if overlap and not conflict:
                key = sorted(overlap)[0]
                return "duplicate", _record_evidence(row, kind, "explicit_" + kind,
                    "posting_identity" if key.startswith("posting:") else "posting_url", key)
            rc, rt = _norm_name(row.get("company") or row.get("employer")), _norm_title(row.get("title"))
            if (nt and nt == rt) or (nc and rc and (nc in rc or rc in nc)):
                advisory = {"kind": "possible_duplicate", "match": "name_or_title",
                            "note": "Similarity is advisory; distinct postings may share titles.",
                            "execution_authorized": False}
    if advisory:
        return "suspect", advisory
    if unresolved:
        return "suspect", {"kind": "source_identity_missing", "execution_authorized": False}
    return "fresh", {}


class DedupeIndex:
    def __init__(self, path, sources, *, max_age_seconds=300, clock=time.time,
                 authenticate_review: Callable | None = None):
        self.path = Path(os.path.abspath(path))
        self.sources = tuple(sources)
        if not self.sources or len(self.sources) > MAX_SOURCES or any(not isinstance(s, Source) for s in self.sources):
            raise ValueError("configure 1..64 approved Source objects")
        if len({s.source_id for s in self.sources}) != len(self.sources) or len({os.path.abspath(s.path) for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate source IDs or paths")
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 86400:
            raise ValueError("snapshot max age must be in (0, 86400]")
        self.max_age_seconds, self.clock = max_age_seconds, clock
        self.authenticate_review = authenticate_review
        self.config_revision = _digest(_json([s.__dict__ for s in self.sources]).encode())

    def _connect(self, *, create=False):
        for part in (self.path, *self.path.parents):
            if part.is_symlink():
                raise ValueError("symlink index prohibited")
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1 or self.path.stat().st_size > 256 * 1024 * 1024):
            raise ValueError("invalid index file")
        if not create and not self.path.exists():
            raise ValueError("index missing")
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        parent_state = self.path.parent.stat()
        if hasattr(os, "getuid") and (parent_state.st_uid != os.getuid() or stat.S_IMODE(parent_state.st_mode) & 0o022):
            raise ValueError("index parent must be operator-owned and not group/world writable")
        for local in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            if os.path.lexists(local):
                state = local.lstat()
                if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1 or (hasattr(os, "getuid") and
                        (state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o077)):
                    raise ValueError("index and sidecars must be private operator-owned regular files")
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        if create:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT,
                    revision TEXT, observed REAL, complete INTEGER);
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, digest TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS entries (source TEXT, ordinal INTEGER, payload TEXT,
                    company TEXT, title TEXT, kind TEXT, PRIMARY KEY(source, ordinal));
                CREATE TABLE IF NOT EXISTS identities (key TEXT, source TEXT, ordinal INTEGER,
                    PRIMARY KEY(key, source, ordinal));
                CREATE INDEX IF NOT EXISTS entries_title ON entries(title);
                CREATE INDEX IF NOT EXISTS entries_company ON entries(company);
                CREATE TABLE IF NOT EXISTS aliases (a TEXT, b TEXT, actor TEXT, evidence TEXT,
                    accepted REAL, active INTEGER, config TEXT, PRIMARY KEY(a,b));
                CREATE TABLE IF NOT EXISTS alias_reviews (sequence INTEGER PRIMARY KEY,
                    request TEXT, actor TEXT, observed REAL);
            """)
        return conn

    def refresh(self):
        """Read bounded source snapshots then replace their projection atomically."""
        captured, errors, total, count, unresolved = [], {}, 0, 0, 0
        for src in self.sources:
            try:
                raw = _regular_bytes(src.path)
                total += len(raw)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("aggregate sources exceed byte limit")
                data = _decode(raw)
                if isinstance(data, dict):
                    fields = [k for k in ("rows", "entries", "items", "leads") if k in data]
                    if len(fields) != 1:
                        raise ValueError("snapshot needs exactly one rows/entries/items/leads array")
                    data = data[fields[0]]
                if not isinstance(data, list):
                    raise ValueError("snapshot must contain a row list")
                count += len(data)
                if count > MAX_ROWS:
                    raise ValueError("aggregate sources exceed row limit")
                prepared = []
                source_unresolved = 0
                for number, row in enumerate(data):
                    if not isinstance(row, dict) or len(_json(row).encode()) > MAX_ROW_BYTES:
                        raise ValueError("invalid or oversized source row")
                    if any(row.get(k) is not None and not isinstance(row.get(k), str)
                           for k in (*URL_FIELDS, "company", "employer", "title", "status", "role_id")):
                        raise ValueError("identity fields must be strings")
                    if src.kind == "ledger" and row.get("status") != "SUBMITTED":
                        continue
                    keys, conflict = keys_for_urls(row)
                    source_unresolved += int(conflict or not keys)
                    prepared.append((number, row, set() if conflict else keys))
                captured.append((src, _digest(raw), prepared))
                unresolved += source_unresolved
            except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                errors[src.source_id] = type(exc).__name__ + ": " + str(exc)[:160]
        revision = _digest(_json({"config": self.config_revision,
                                 "sources": {s.source_id: h for s, h, _ in captured},
                                 "errors": errors}).encode())
        conn = self._connect(create=True)
        try:
            with conn:
                conn.execute("DELETE FROM identities")
                conn.execute("DELETE FROM entries")
                conn.execute("DELETE FROM sources")
                for src, digest, rows in captured:
                    conn.execute("INSERT INTO sources VALUES(?,?,NULL)", (src.source_id, digest))
                    for ordinal, row, keys in rows:
                        conn.execute("INSERT INTO entries VALUES(?,?,?,?,?,?)", (src.source_id, ordinal,
                            _json(row), _norm_name(row.get("company") or row.get("employer")), _norm_title(row.get("title")), src.kind))
                        conn.executemany("INSERT INTO identities VALUES(?,?,?)", [(k, src.source_id, ordinal) for k in keys])
                for source_id, error in errors.items():
                    conn.execute("INSERT INTO sources VALUES(?,NULL,?)", (source_id, error))
                conn.execute("INSERT OR REPLACE INTO meta VALUES(1,?,?,?,?)",
                             (self.config_revision, revision, _now(self.clock), int(not errors and not unresolved)))
        finally:
            conn.close()
        return {"revision": revision, "complete": not errors and not unresolved, "errors": errors, "unresolved_rows": unresolved,
                "rows": sum(len(rows) for _, _, rows in captured), "execution_authorized": False}

    def _freshness(self, conn):
        meta = conn.execute("SELECT * FROM meta WHERE id=1").fetchone()
        if not meta or meta["config"] != self.config_revision:
            return "index_configuration_changed", None
        age = _now(self.clock) - meta["observed"]
        if age < 0 or age > self.max_age_seconds:
            return "index_expired", meta["revision"]
        sources = {r["id"]: r for r in conn.execute("SELECT * FROM sources")}
        total, incomplete = 0, not bool(meta["complete"])
        for src in self.sources:
            state = sources.get(src.source_id)
            if not state or state["error"]:
                incomplete = True
                continue
            raw = _regular_bytes(src.path)
            total += len(raw)
            if total > MAX_TOTAL_BYTES or _digest(raw) != state["digest"]:
                return "source_revision_changed", meta["revision"]
        return ("index_incomplete" if incomplete else None), meta["revision"]

    def status(self):
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                reason, revision = self._freshness(conn)
                return {"complete": reason is None, "reason": reason, "revision": revision}
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            return {"complete": False, "reason": "index_unavailable", "error": type(exc).__name__, "revision": None}

    def check_candidate(self, company, title, url="", *, urls=None, description=None):
        values = urls if urls is not None else [url]
        try:
            keys, ambiguous = keys_for_urls(values)
            if ambiguous:
                return "suspect", {"kind": "ambiguous_candidate_identity", "execution_authorized": False}
            if not keys:
                return "suspect", {"kind": "candidate_identity_missing", "execution_authorized": False}
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                reason, revision = self._freshness(conn)
                if reason and reason != "index_incomplete":
                    return "suspect", {"kind": reason, "index_revision": revision, "execution_authorized": False}
                result = self._lookup(conn, company, title, keys, revision)
                reason, _ = self._freshness(conn)
                if reason and reason != "index_incomplete":
                    return "suspect", {"kind": reason, "index_revision": revision, "execution_authorized": False}
                if reason and result[0] != "duplicate":
                    return "suspect", {"kind": reason, "index_revision": revision, "execution_authorized": False}
                if reason:
                    result[1]["coverage_complete"] = False
                return result
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            return "suspect", {"kind": "index_unavailable", "error": type(exc).__name__, "execution_authorized": False}

    def _lookup(self, conn, company, title, keys, revision):
        for key in sorted(keys):
            # Direct, authenticated pairs only: no transitive alias inference.
            aliases = conn.execute("SELECT a,b FROM aliases WHERE active=1 AND config=? AND (a=? OR b=?)", (self.config_revision, key, key)).fetchall()
            lookup = [key] + [r["b"] if r["a"] == key else r["a"] for r in aliases]
            for match_key in lookup:
                hit = conn.execute("SELECT e.* FROM identities i JOIN entries e ON e.source=i.source AND e.ordinal=i.ordinal WHERE i.key=? ORDER BY e.kind,e.source,e.ordinal LIMIT 1", (match_key,)).fetchone()
                if hit:
                    match = "reviewed_alias" if match_key != key else ("posting_identity" if key.startswith("posting:") else "posting_url")
                    evidence = _record_evidence(json.loads(hit["payload"]), hit["kind"], hit["source"], match, key)
                    evidence["index_revision"] = revision
                    return "duplicate", evidence
        nc, nt = _norm_name(company), _norm_title(title)
        hit = conn.execute("SELECT source FROM entries WHERE (title=? AND title!='') OR (company=? AND company!='') LIMIT 1", (nt, nc)).fetchone()
        if hit:
            return "suspect", {"kind": "possible_duplicate", "match": "name_or_title", "index_revision": revision,
                               "note": "Similarity does not block intake.", "execution_authorized": False}
        return "fresh", {"index_revision": revision, "coverage": [s.source_id for s in self.sources], "execution_authorized": False}

    def check_batch(self, entries):
        """Indexed batch with source hashes checked before and after the read.

        All results share a SQLite read transaction. A source change/expiry
        during the batch downgrades the entire result to advisory uncertainty.
        """
        if not isinstance(entries, (list, tuple)) or len(entries) > 10_000 or any(not isinstance(e, dict) for e in entries):
            raise ValueError("batch requires at most 10000 candidate mappings")
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                reason, revision = self._freshness(conn)
                if reason and reason != "index_incomplete":
                    return [("suspect", {"kind": reason, "index_revision": revision, "execution_authorized": False}) for _ in entries]
                results = []
                for entry in entries:
                    keys, conflict = keys_for_urls(entry)
                    results.append(("suspect", {"kind": "ambiguous_candidate_identity" if conflict else "candidate_identity_missing", "execution_authorized": False}) if conflict or not keys else
                                   self._lookup(conn, entry.get("company") or entry.get("employer"), entry.get("title"), keys, revision))
                reason, _ = self._freshness(conn)
                if reason and reason != "index_incomplete":
                    return [("suspect", {"kind": reason, "index_revision": revision, "execution_authorized": False}) for _ in entries]
                if reason:
                    results = [(verdict, {**evidence, "coverage_complete": False}) if verdict == "duplicate" else
                               ("suspect", {"kind": reason, "index_revision": revision, "execution_authorized": False})
                               for verdict, evidence in results]
                return results
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            return [("suspect", {"kind": "index_unavailable", "error": type(exc).__name__, "execution_authorized": False}) for _ in entries]

    def review_alias(self, left_url, right_url, *, evidence_ref, credential, action="accept"):
        """Host verifier binds authenticated operator to this exact review request.

        No caller-supplied approved=True flag is accepted. The host must validate
        credential replay, expiry, operator authority and payload binding.
        """
        if not callable(self.authenticate_review):
            raise PermissionError("host review authenticator is not configured")
        if action not in {"accept", "revoke"} or not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref) > 1024:
            raise ValueError("explicit review action and evidence reference required")
        left, _ = keys_for_urls([left_url])
        right, _ = keys_for_urls([right_url])
        if len(left) != 1 or len(right) != 1 or left == right:
            raise ValueError("two different exact URL/posting identities required")
        a, b = sorted([next(iter(left)), next(iter(right))])
        payload = {"action": action, "left": a, "right": b, "evidence_ref": evidence_ref,
                   "index": str(self.path), "source_config_revision": self.config_revision}
        actor = self.authenticate_review(payload, credential)
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 160:
            raise PermissionError("review credential was not authenticated")
        conn = self._connect(create=True)
        try:
            with conn:
                observed = _now(self.clock)
                conn.execute("INSERT INTO alias_reviews(request,actor,observed) VALUES(?,?,?)", (_json(payload), actor, observed))
                conn.execute("INSERT OR REPLACE INTO aliases VALUES(?,?,?,?,?,?,?)", (a, b, actor, evidence_ref, observed, int(action == "accept"), self.config_revision))
        finally:
            conn.close()
        return {**payload, "actor": actor, "execution_authorized": False}


def get_index(pipe=None, *, ledger_path=None, queue_path=None, config_path=None, refresh=True):
    """Load explicit source registry; otherwise only the two established defaults.

    Registry: {"sources":[{"source_id":"west","path":"/abs/west.json","kind":"queue"}]}.
    The local operator controls this configuration. No directory glob discovers
    backups, archives or new queues. Missing/corrupt configured input stays held.
    """
    from keel_paths import HOME
    root = Path(pipe or HOME).absolute()
    config = Path(config_path) if config_path else root / "config" / "identity-sources.json"
    if config_path is not None or os.path.lexists(config):
        document = _decode(_regular_bytes(config, 64 * 1024))
        if not isinstance(document, dict) or set(document) != {"sources"} or not isinstance(document["sources"], list):
            raise ValueError("invalid identity source registry")
        sources = [Source(**item) for item in document["sources"]]
    else:
        sources = [Source("submitted-ledger", str(Path(ledger_path or root / "data/application-ledger.json").absolute()), "ledger"),
                   Source("standard-queue", str(Path(queue_path or root / "data/queues/standard-queue.json").absolute()))]
    index = DedupeIndex(root / "data/identity-index.sqlite3", sources)
    if refresh and not index.status()["complete"]:
        index.refresh()
    return index


def stage_verdict(entry, index, batch_url_keys=None, batch_emp_titles=None):
    keys, conflict = keys_for_urls(entry)
    verdict, evidence = index.check_candidate(entry.get("company") or entry.get("employer"), entry.get("title"),
                                             urls=entry)
    if not conflict and batch_url_keys is not None and keys & batch_url_keys:
        verdict, evidence = "duplicate", {"kind": "duplicate_in_batch", "match": "posting_identity", "execution_authorized": False}
    # Legacy fourth return slot remains diagnostic, never a hard duplicate key.
    return verdict, evidence, ([] if conflict else sorted(keys)), (_norm_name(entry.get("company") or entry.get("employer")), _norm_title(entry.get("title")))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Local canonical posting identity projection")
    parser.add_argument("--home", help="Keel workspace root")
    parser.add_argument("--config", help="Explicit identity source registry JSON")
    parser.add_argument("operation", choices=("refresh", "status", "check"))
    parser.add_argument("--url", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--title", default="")
    args = parser.parse_args(argv)
    try:
        index = get_index(args.home, config_path=args.config, refresh=False)
        if args.operation == "refresh":
            result = index.refresh()
        elif args.operation == "status":
            result = index.status()
        else:
            verdict, evidence = index.check_candidate(args.company, args.title, args.url)
            result = {"verdict": verdict, "evidence": evidence}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result.get("complete", result.get("verdict") == "fresh") else 2
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"complete": False, "reason": "index_unavailable", "error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
