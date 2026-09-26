"""Scoped SQLite FTS5 projection over authenticated observation records.

The trusted host supplies the observation store and clock. Document metadata,
permissions and text must be bound by that store's verified event. Every read
rechecks provenance, expiry and conflicts; persisted search rows are never an
authority cache. The existing TemporalMemory owns revision/dependency semantics.
Cross-store projection is idempotent, with durable PENDING state after a crash.
These stores do not defend against a malicious owner of all local databases.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from keel_loki.common import canonical, clone, digest, require_hash, require_id, require_int
from keel_loki.retrieval import search as rank_passages
from keel_loki.temporal import TemporalMemory

SCHEMA = 'keel.memory.index.v1'
USES = {'planning', 'review', 'application_fact'}
MAX_DOCUMENTS = 10000
MAX_CANDIDATES = 1024
FLAGS = {'execution_authorized': False, 'canonical_writes': 0,
         'truth_verified': False, 'untrusted_text': True}


class IndexError(ValueError):
    """Content-free evidence indexing error."""


def _check(condition, code):
    if not condition:
        raise IndexError(code)


def text_digest(text):
    _check(type(text) is str and 0 < len(text.encode('utf-8')) <= 32768,
           'bounded_document_text_required')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def document_digest(document):
    return digest(_document(document))


def _document(value):
    _check(type(value) is dict and set(value) == {
        'schema', 'document_id', 'observation_id', 'account_id', 'scope', 'source_id',
        'claim_key', 'text', 'permitted_uses', 'valid_from', 'valid_until', 'supersedes'},
        'document_schema_invalid')
    _check(value['schema'] == 'keel.memory.document.v1', 'document_version_invalid')
    for key in ('document_id', 'observation_id', 'account_id', 'scope', 'source_id', 'claim_key'):
        require_id(value[key])
    text_digest(value['text'])
    require_int(value['valid_from'], 0, 253402300799)
    if value['valid_until'] is not None:
        require_int(value['valid_until'], value['valid_from'] + 1, 253402300799)
    uses = value['permitted_uses']
    _check(type(uses) is list and 1 <= len(uses) <= len(USES)
           and all(type(x) is str and x in USES for x in uses)
           and len(set(uses)) == len(uses), 'document_uses_invalid')
    previous = value['supersedes']
    _check(type(previous) is list and len(previous) <= 64, 'supersession_invalid')
    for identity in previous:
        require_id(identity)
    _check(len(set(previous)) == len(previous) and value['document_id'] not in previous,
           'supersession_invalid')
    return clone(value)


def _private(path, directory=False):
    try:
        _check(path.resolve(strict=True) == path, 'index_symlink_forbidden')
        info = path.lstat()
        _check((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
               and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
               'index_storage_not_private')
        _check(directory or info.st_nlink == 1, 'index_hardlink_forbidden')
        _check(not hasattr(os, 'getuid') or info.st_uid == os.getuid(), 'index_owner_mismatch')
        return info.st_dev, info.st_ino
    except OSError:
        raise IndexError('index_storage_unavailable') from None


def _ancestors(path):
    for parent in path.parents:
        info = parent.lstat()
        _check(stat.S_ISDIR(info.st_mode), 'index_symlink_forbidden')
        _check(not hasattr(os, 'getuid') or info.st_uid in (0, os.getuid()),
               'index_untrusted_ancestor_owner')
        _check(not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX,
               'index_writable_ancestor_forbidden')


class EvidenceIndex:
    """Rebuildable private projection, pinned to one host-owned observation store.

    ``observations`` must implement store_id and inspect(account_id,event_id).
    It is an injected host capability, never a caller-supplied JSON assertion.
    Same-owner local processes are trusted; do not expose this object directly
    to a model/plugin or share the database directory with an untrusted worker.
    """

    def __init__(self, home, observations, *, clock=None):
        self.home = Path(os.path.abspath(home))
        _check(self.home.parent.resolve(strict=True) == self.home.parent, 'index_symlink_forbidden')
        _ancestors(self.home)
        self.home.mkdir(mode=0o700, exist_ok=True)
        self._home_identity = _private(self.home, True)
        self.observations = observations
        self.store_id = require_id(observations.store_id)
        self.clock = clock or (lambda: int(time.time()))
        self.path = self.home / 'index.sqlite3'
        created = False
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            created = True
        except FileExistsError:
            pass
        self._file_identity = _private(self.path)
        with self._db(write=True) as db:
            if created:
                db.executescript('''
                    CREATE TABLE meta (schema TEXT NOT NULL, store_id TEXT NOT NULL, last_now INTEGER NOT NULL);
                    CREATE TABLE documents (account TEXT NOT NULL, identity TEXT NOT NULL,
                        scope TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('PENDING','READY','CONFLICT')),
                        PRIMARY KEY(account,identity));
                    CREATE TABLE uses (account TEXT, identity TEXT, purpose TEXT,
                        PRIMARY KEY(account,identity,purpose));
                    CREATE TABLE artifacts (account TEXT, identity TEXT, payload TEXT,
                        PRIMARY KEY(account,identity));
                    CREATE VIRTUAL TABLE search_text USING fts5(account UNINDEXED, identity UNINDEXED, text);
                ''')
                db.execute('INSERT INTO meta VALUES (?,?,?)', (SCHEMA, self.store_id, self._now()))
            row = db.execute('SELECT schema,store_id FROM meta').fetchall()
            _check(len(row) == 1 and tuple(row[0]) == (SCHEMA, self.store_id), 'index_store_binding_mismatch')
        self._temporal = TemporalMemory(self.home / 'temporal', clock=self.clock)

    def _now(self):
        return require_int(self.clock(), 0, 253402300799)

    def _storage(self):
        _ancestors(self.home)
        _check(_private(self.home, True) == self._home_identity and
               _private(self.path) == self._file_identity, 'index_storage_replaced')
        for suffix in ('-journal', '-wal', '-shm'):
            companion = Path(str(self.path) + suffix)
            if companion.exists() or companion.is_symlink():
                _private(companion)

    @contextmanager
    def _db(self, write=False):
        self._storage()
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                                         isolation_level=None, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            self._storage()
            connection.commit()
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise IndexError('index_database_error_or_fts5_unavailable') from None
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _locked(self):
        self._storage()
        fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _tick(self, db):
        now = self._now()
        previous = db.execute('SELECT last_now FROM meta').fetchone()[0]
        _check(now >= previous, 'index_clock_regressed')
        db.execute('UPDATE meta SET last_now=?', (now,))
        return now

    @staticmethod
    def _revision(document):
        return 'doc-' + digest([document['account_id'], document['document_id']])

    def _proof(self, document):
        try:
            row = self.observations.inspect(document['account_id'], document['observation_id'])
        except Exception:
            return None, ['observation_unavailable']
        if type(row) is not dict or type(row.get('event')) is not dict:
            return None, ['observation_unavailable']
        event, reasons = row['event'], []
        if row.get('trust') != 'VERIFIED' or row.get('status') != 'CURRENT':
            reasons.append('observation_not_current_verified')
        if event.get('account_id') != document['account_id'] or event.get('event_id') != document['observation_id']:
            reasons.append('observation_identity_mismatch')
        if event.get('lineage', {}).get('source_id') != document['source_id']:
            reasons.append('observation_source_mismatch')
        revisions = event.get('revisions', {})
        if (revisions.get('evidence') != text_digest(document['text'])
                or revisions.get('memory_document') != document_digest(document)):
            reasons.append('document_not_bound_by_observation')
        proof = row.get('verification')
        now = self._now()
        if (type(proof) is not dict or type(proof.get('expires_at')) not in (int,float)
                or type(proof.get('verified_at')) not in (int,float)
                or not math.isfinite(proof['expires_at']) or not math.isfinite(proof['verified_at'])
                or not proof['verified_at'] <= now < proof['expires_at']):
            reasons.append('verification_not_current')
        if type(proof) is dict and proof.get('account_id') != document['account_id']:
            reasons.append('verification_account_mismatch')
        return row, reasons

    def _project(self, document, row):
        revision = self._revision(document)
        claim = {'revision_id': revision, 'subject_id': document['account_id'],
                 'key': document['claim_key'], 'scope': document['scope'],
                 'value': {'document_sha256': document_digest(document)},
                 'source': {'source_id': document['source_id'], 'sha256': document_digest(document),
                            'classification': 'personal', 'allowed_scopes': [document['scope']],
                            'permitted_uses': document['permitted_uses']},
                 'permitted_uses': document['permitted_uses'], 'valid_from': document['valid_from'],
                 'valid_until': document['valid_until'],
                 'supersedes': ['doc-' + digest([document['account_id'], x]) for x in document['supersedes']]}
        self._temporal.record_claim(claim, 'claim-' + revision)
        observation = {'observation_id': 'binding-' + revision, 'revision_id': revision,
                       'reviewer_id': 'authenticated-binding-projection', 'verdict': 'verified',
                       'source_sha256': document_digest(document),
                       'observed_at': int(datetime.fromisoformat(row['event']['occurred_at'].replace('Z','+00:00')).timestamp())}
        self._temporal.record_observation(observation, 'binding-' + revision)

    def ingest(self, document):
        document = _document(document)
        row, reasons = self._proof(document)
        _check(not reasons, 'document_authentication_failed')
        key = (document['account_id'], document['document_id'])
        with self._locked():
            with self._db(write=True) as db:
                self._tick(db)
                existing = db.execute('SELECT * FROM documents WHERE account=? AND identity=?', key).fetchone()
                if existing is not None and existing['digest'] != document_digest(document):
                    db.execute("UPDATE documents SET state='CONFLICT' WHERE account=? AND identity=?", key)
                    return {'status': 'CONFLICT', 'created': False, **FLAGS}
                if existing is not None and existing['state'] == 'CONFLICT':
                    return {'status': 'CONFLICT', 'created': False, **FLAGS}
                for previous in document['supersedes']:
                    old = db.execute('SELECT payload,state FROM documents WHERE account=? AND identity=?',
                                     (document['account_id'], previous)).fetchone()
                    _check(old is not None and old['state'] == 'READY', 'unknown_or_unprojected_supersession')
                    old_doc = _document(json.loads(old['payload']))
                    _check(all(old_doc[k] == document[k] for k in ('account_id','scope','claim_key')),
                           'cross_scope_supersession')
                if existing is None:
                    _check(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] < MAX_DOCUMENTS,
                           'index_document_limit')
                    db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)',
                               (*key, document['scope'], canonical(document).decode(), document_digest(document), 'PENDING'))
                    db.executemany('INSERT INTO uses VALUES (?,?,?)', [(*key, p) for p in document['permitted_uses']])
                    db.execute('INSERT INTO search_text VALUES (?,?,?)', (*key, document['text']))
            self._project(document, row)
            # Recheck producer state after projection. Reads also revalidate it.
            _, reasons = self._proof(document)
            if reasons:
                return {'status': 'HELD', 'created': existing is None, 'holds': reasons, **FLAGS}
            with self._db(write=True) as db:
                db.execute("UPDATE documents SET state='READY' WHERE account=? AND identity=?", key)
        return {'status': 'INDEXED', 'created': existing is None,
                'document_sha256': document_digest(document), **FLAGS}

    def refresh(self, account_id):
        require_id(account_id)
        with self._db() as db:
            pending = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM documents WHERE account=? AND state='PENDING' ORDER BY rowid", (account_id,))]
        results = []
        for document in pending:
            _, reasons = self._proof(document)
            if reasons:
                results.append({'document_id': document['document_id'], 'status': 'HELD', 'holds': reasons})
            else:
                results.append({'document_id': document['document_id'], **self.ingest(document)})
        return {'status': 'REFRESHED', 'projections': results, **FLAGS}

    def _status(self, record, purpose, now, history=None):
        document = _document(json.loads(record['payload']))
        _check(document_digest(document) == record['digest'], 'indexed_document_corrupt')
        row, reasons = self._proof(document)
        if record['state'] != 'READY':
            reasons.append('document_' + record['state'].lower())
        if purpose not in document['permitted_uses']:
            reasons.append('purpose_forbidden')
        if not document['valid_from'] <= now or (document['valid_until'] is not None and now >= document['valid_until']):
            reasons.append('document_not_current')
        history = self._temporal.history() if history is None else history
        resolution = self._temporal._resolve(history, document['account_id'], document['claim_key'],
                                            document['scope'], purpose, now)
        if resolution['revision_ids'] != [self._revision(document)] or not resolution['usable_observation']:
            reasons.append('revision_' + resolution['status'].lower())
        return document, row, sorted(set(reasons))

    def search(self, query, *, account_id, scope, purpose, limit=5):
        for value in (account_id, scope):
            require_id(value)
        _check(type(purpose) is str and purpose in USES, 'purpose_invalid')
        require_int(limit, 1, 50)
        _check(type(query) is str and 0 < len(query.encode('utf-8')) <= 4096, 'query_invalid')
        terms = sorted(set(re.findall(r'\w+', query.casefold(), flags=re.UNICODE)))
        _check(1 <= len(terms) <= 32, 'query_terms_invalid')
        expression = ' OR '.join('"' + term + '"' for term in terms)
        with self._db(write=True) as db:
            now = self._tick(db)
            records = db.execute('''SELECT d.* FROM search_text
                JOIN documents d ON d.account=search_text.account AND d.identity=search_text.identity
                JOIN uses u ON u.account=d.account AND u.identity=d.identity
                WHERE search_text MATCH ? AND d.account=? AND d.scope=? AND u.purpose=?
                ORDER BY d.identity LIMIT ?''',
                (expression, account_id, scope, purpose, MAX_CANDIDATES + 1)).fetchall()
        if len(records) > MAX_CANDIDATES:
            return {'status': 'QUERY_TOO_BROAD', 'matches': [], 'holds': ['narrow_query_required'], **FLAGS}
        passages, proofs, held = [], {}, 0
        history = self._temporal.history()
        for record in records:
            document, proof, reasons = self._status(record, purpose, now, history)
            if reasons:
                held += 1
                continue
            identity = document['document_id']
            proofs[identity] = (record, proof)
            passages.append({'passage_id': identity, 'source_id': document['source_id'],
                'source_sha256': document_digest(document), 'text': document['text'], 'scope': scope,
                'permitted_uses': document['permitted_uses'], 'valid_from': document['valid_from'],
                'valid_until': document['valid_until'], 'verification': 'verified_observation', 'embedding': None})
        matches = []
        if passages:
            corpus = {'schema': 'keel.loki.corpus.v1', 'passages': passages}
            ranked = rank_passages(corpus, query, expected_corpus_sha256=digest(corpus),
                                   scope=scope, purpose=purpose, now=now, limit=limit)
            latest_history = self._temporal.history()
            for match in ranked['matches']:
                record, previous = proofs[match['passage_id']]
                with self._db() as db:
                    fresh = db.execute('SELECT * FROM documents WHERE account=? AND identity=?',
                                       (account_id,match['passage_id'])).fetchone()
                if fresh is None or fresh['digest'] != record['digest'] or fresh['state'] != 'READY':
                    held += 1
                    continue
                document, current, reasons = self._status(fresh, purpose, self._now(), latest_history)
                if reasons or current != previous:
                    held += 1
                    continue
                matches.append({**match, 'document_id': match['passage_id'],
                    'observation_id': document['observation_id'],
                    'assurance': current['verification']['assurance'],
                    'producer_binding_verified': True})
        return {'schema': 'keel.memory.search.v1', 'status': 'RETRIEVED' if matches else 'NO_CURRENT_MATCH',
                'store_id': self.store_id, 'as_of': now, 'matches': matches,
                'held_matching_documents': held, 'purpose': purpose, 'model_calls': 0, **FLAGS}

    def register_artifact(self, artifact):
        _check(type(artifact) is dict and set(artifact) == {'artifact_id','account_id','scope','purpose','sha256','documents'},
               'artifact_schema_invalid')
        artifact = clone(artifact)
        for key in ('artifact_id','account_id','scope'):
            require_id(artifact[key])
        require_hash(artifact['sha256'])
        _check(type(artifact['purpose']) is str and artifact['purpose'] in USES, 'purpose_invalid')
        ids = artifact['documents']
        _check(type(ids) is list and 1 <= len(ids) <= 64, 'artifact_documents_invalid')
        for identity in ids:
            require_id(identity)
        _check(len(ids) == len(set(ids)), 'artifact_duplicate_documents')
        with self._locked():
            with self._db(write=True) as db:
                now = self._tick(db)
                records = [db.execute('SELECT * FROM documents WHERE account=? AND identity=?',
                                     (artifact['account_id'], identity)).fetchone() for identity in ids]
                _check(all(r is not None and r['scope'] == artifact['scope'] for r in records),
                       'artifact_document_scope_mismatch')
                old = db.execute('SELECT payload FROM artifacts WHERE account=? AND identity=?',
                                 (artifact['account_id'],artifact['artifact_id'])).fetchone()
                _check(old is None or old[0] == canonical(artifact).decode(), 'artifact_identity_conflict')
            docs = []
            for record in records:
                document, _, reasons = self._status(record, artifact['purpose'], now)
                _check(not reasons, 'artifact_dependencies_not_current')
                docs.append(document)
            temporal_artifact = {'artifact_id':'artifact-' + digest([artifact['account_id'],artifact['artifact_id']]),
                'subject_id':artifact['account_id'], 'scope':artifact['scope'], 'purpose':artifact['purpose'],
                'sha256':artifact['sha256'], 'dependencies':[self._revision(d) for d in docs]}
            self._temporal.register_artifact(temporal_artifact, temporal_artifact['artifact_id'])
            with self._db(write=True) as db:
                db.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?)',
                           (artifact['account_id'], artifact['artifact_id'], canonical(artifact).decode()))
        return self.artifact_status(artifact['account_id'], artifact['artifact_id'])

    def artifact_status(self, account_id, artifact_id):
        require_id(account_id); require_id(artifact_id)
        with self._db(write=True) as db:
            now = self._tick(db)
            row = db.execute('SELECT payload FROM artifacts WHERE account=? AND identity=?',
                             (account_id,artifact_id)).fetchone()
            _check(row is not None, 'artifact_not_found')
            artifact = json.loads(row[0])
            records = [db.execute('SELECT * FROM documents WHERE account=? AND identity=?',
                                 (account_id,identity)).fetchone() for identity in artifact['documents']]
        stale = []
        for identity, record in zip(artifact['documents'], records):
            reasons = ['document_missing'] if record is None else self._status(record, artifact['purpose'], now)[2]
            if reasons:
                stale.append({'document_id':identity,'reasons':reasons})
        return {'artifact_id':artifact_id,'sha256':artifact['sha256'],
                'status':'STALE' if stale else 'DEPENDENCIES_CURRENT','reapproval_required':bool(stale),
                'stale_dependencies':stale,'artifact_content_verified':False, **FLAGS}
