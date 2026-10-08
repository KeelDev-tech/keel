"""Public source intake and verification control plane, standard library only.

No submissions, generated applicant claims, API keys, source guessing, or READY
promotion. Public posting observations and applicant decisions are independent.
All queue updates compare the selected row with current state under queue_lock.
"""
from __future__ import annotations
import copy
import hashlib
import math
import re
import socket
import tempfile
import time
import uuid
from collections import Counter, defaultdict, OrderedDict
from datetime import timedelta
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from safe_http import urlopen, validate_url, HostRateLimited, NetworkPolicyError
from safe_io import (atomic_json, read_json, rows, loads, digest, canonical, utc_now,
                     fresh, aware_time, file_lock, MAX_JSON_BYTES)
from queue_io import queue_lock
from posting_identity import TOKEN, identity, observe
from fit_policy import main_floor

QUEUES = ('standard', 'strategic', 'needs_input', 'rejected')
# Include the closed lifecycle states recognized by queue_intake and the
# strategic reconciler. A posting read must not implicitly reopen them.
FINAL_OR_ACTIVE = {'SUBMITTED', 'SUBMISSION_CLAIMED', 'IN-FLIGHT', 'APPLYING', 'UNKNOWN_OUTCOME',
                   'REJECTED', 'DEAD', 'CANCELLED', 'CLOSED', 'CLOSED-EXPIRED'}
MAX_BOARDS = 100
MAX_JOBS = 20000
PAGE_SIZE = 100
MAX_BOARD_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_RECORDS = 20000
MAX_CANDIDATE_BYTES = 32 * 1024 * 1024
MAX_CANDIDATE_RECORDS = 40000
POSTING_FRESH_SECONDS = 3600
VERIFY_RETRY_SECONDS = 300


@dataclass(frozen=True)
class VerificationResult:
    """Posting signal and transport outcome are independent authorities.

    NONE never revises a posting verdict or recovery track. The evidence is
    bounded diagnostic data, never exception text or applicant facts.
    """
    signal: str
    transport_class: str
    evidence: dict
    retry_after: int


def _transport_class(exc):
    if isinstance(exc, HostRateLimited):
        return 'PERSISTED_HTTP_429'
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return 'HTTP_429'
        if exc.code in (401, 403):
            return 'ACCESS_WALL'
        if exc.code >= 500:
            return 'HTTP_5XX'
        # A board endpoint 404/410 is not death evidence for one posting.
        return 'HTTP_SOURCE_ERROR'
    reason = getattr(exc, 'reason', None)
    cause = reason if isinstance(reason, BaseException) else getattr(exc, '__cause__', None)
    if isinstance(exc, socket.gaierror) or isinstance(cause, socket.gaierror):
        return 'DNS_FAILURE'
    if isinstance(exc, ConnectionResetError) or isinstance(cause, ConnectionResetError):
        return 'CONNECTION_RESET'
    if isinstance(exc, TimeoutError) or isinstance(cause, TimeoutError):
        if str(exc) == 'request budget exhausted or run stopped':
            return 'REQUEST_BUDGET'
        return 'TIMEOUT'
    if isinstance(exc, ValueError):
        return 'SOURCE_CONTRACT'
    if isinstance(exc, NetworkPolicyError):
        return 'NETWORK_POLICY'
    if isinstance(exc, URLError):
        return 'NETWORK_ERROR'
    return 'SOURCE_UNAVAILABLE'


def _provider_host(ref):
    return {'greenhouse': 'boards-api.greenhouse.io', 'ashby': 'api.ashbyhq.com',
            'lever': 'api.lever.co', 'lever_eu': 'api.eu.lever.co'}[parse_source(ref)[0]]


class SourceCapacityError(ValueError):
    """A complete source/run cannot be retained within its explicit budget."""


class SourceStorageError(OSError):
    """Temporary candidate storage failed; no partial intake is permitted."""


def _deadline(reader):
    # Preserve injected-reader compatibility; production readers always expose
    # a deadline. This bounds cooperative CPU work, not arbitrary callbacks.
    limit = getattr(reader, 'deadline', None)
    if limit is not None and time.monotonic() >= limit:
        raise TimeoutError('source run deadline exceeded')


def _lock_budget(reader):
    limit = getattr(reader, 'deadline', None)
    remaining = 10 if limit is None else min(10, limit-time.monotonic())
    if remaining <= 0:
        raise TimeoutError('source run deadline exceeded')
    return remaining


def _bounded(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f'{name} must be an integer in 1..{maximum}')
    return value


def _budget(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 900:
        raise ValueError('timeout must be finite in (0, 900]')
    return time.monotonic() + value


def parse_source(ref):
    if not isinstance(ref, str) or ref.count(':') != 1:
        raise ValueError('source must be provider:board')
    provider, board = ref.split(':')
    if provider not in {'greenhouse', 'lever', 'lever_eu', 'ashby'} or not TOKEN.fullmatch(board):
        raise ValueError('supported providers: greenhouse, lever, lever_eu, ashby; use the exact board token')
    return provider, board


def add_source(workspace, ref, company=None):
    parse_source(ref)
    if company is not None and (not isinstance(company, str) or not 0 < len(company) <= 200):
        raise ValueError('company label must contain 1..200 characters')
    path = Path(workspace)/'data/sources.json'
    with file_lock(str(path)+'.lock'):
        config = read_json(path, missing={'schema_version': 1, 'sources': []})
        if not isinstance(config, dict) or not isinstance(config.get('sources'), list):
            raise ValueError('invalid source registry; restore it before adding')
        if any(not isinstance(x, dict) for x in config['sources']):
            raise ValueError('invalid source row')
        if any(x.get('ref') == ref for x in config['sources']):
            return {'already_registered': ref}
        if len(config['sources']) >= MAX_BOARDS:
            raise ValueError('source registry exceeds board budget')
        config['sources'].append({'ref': ref, 'company_label': company or ref.split(':')[1],
                                  'enabled': True, 'registered_at': utc_now().isoformat()})
        atomic_json(path, config)
    return {'registered': ref, 'submission_authorized': False}


def _sources(workspace):
    config = read_json(Path(workspace)/'data/sources.json', missing={'sources': []})
    if not isinstance(config, dict) or not isinstance(config.get('sources'), list) or len(config['sources']) > MAX_BOARDS:
        raise ValueError('invalid source registry')
    found = {}
    for source in config['sources']:
        if not isinstance(source, dict):
            raise ValueError('invalid source row')
        parse_source(source.get('ref'))
        if 'enabled' in source and type(source['enabled']) is not bool:
            raise ValueError('source enabled must be boolean')
        if ('company_label' in source and
                (not isinstance(source['company_label'], str) or len(source['company_label']) > 200)):
            raise ValueError('source company label must be bounded text')
        if source['ref'] in found:
            raise ValueError('duplicate source configuration')
        found[source['ref']] = source
    return [x for x in found.values() if x.get('enabled') is True]


def _queue_path(workspace, name):
    return Path(workspace)/'data/queues'/f'{name}-queue.json'


def _documents(workspace):
    out = {}
    for name in QUEUES:
        path = _queue_path(workspace, name)
        document = read_json(path, missing=[])
        rows(document)
        out[path] = document
    ledger = rows(read_json(Path(workspace)/'data/application-ledger.json', missing=[]))
    return out, ledger


def _all_rows(documents):
    return [(path, row) for path, document in documents.items() for row in rows(document)]


def posting_url(entry):
    for key in ('ats_url', 'application_url', 'posting_url', 'job_url', 'apply_url', 'url'):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _key(entry):
    try:
        return identity(posting_url(entry))
    except (ValueError, TypeError):
        return None


def _held(entry):
    if str(entry.get('status', '')).upper() in FINAL_OR_ACTIVE:
        return True
    if entry.get('holds') or entry.get('structurally_blocked') or entry.get('human_hold') or entry.get('revoked'):
        return True
    return False


def _identity_index(entries, reader=None):
    """Parse each immutable queue snapshot once, retaining duplicate evidence.

    This is deliberately scoped to one snapshot: caching across queue commits
    could hide a concurrently changed target or a newly introduced duplicate.
    """
    indexed, role_counts, posting_counts = [], Counter(), Counter()
    for offset, (path, row) in enumerate(entries):
        if offset % 32 == 0 and reader is not None:
            _deadline(reader)
        key = _key(row)
        indexed.append((path, row, key))
        role_counts[row.get('role_id')] += 1
        if key is not None:
            posting_counts[key] += 1
    return indexed, role_counts, posting_counts


def _terminal_ledger_index(ledger, reader=None):
    ids, keys = set(), set()
    for offset, row in enumerate(ledger):
        if offset % 32 == 0 and reader is not None:
            _deadline(reader)
        if str(row.get('status', '')).upper() in FINAL_OR_ACTIVE:
            ids.add(row.get('role_id'))
            key = _key(row)
            if key is not None:
                keys.add(key)
    return ids, keys


def _request_budget_blocks(reader, ref):
    """A cached complete board needs no request, even at the request cap."""
    cap = getattr(reader, 'max_requests', None)
    return (cap is not None and reader.requests >= cap
            and ref not in getattr(reader, 'cache', {})
            and ref not in getattr(reader, 'errors', {}))


class PublicBoardReader:
    """Per-run board cache: one fetch per GH/Ashby board; bounded Lever pages.

    The entire response is validated before it is usable. Partial pagination
    cannot be treated as a complete source, nor can it prove missing jobs dead.
    A 429 ends this reader's run; safe_http additionally persists host cooldown.
    ``requests`` bounds reader dispatches, not sockets or HTTP redirect hops.
    """
    def __init__(self, timeout=120, fetcher=None, max_requests=50, retry_timeouts=False):
        self.deadline = _budget(timeout)
        self.fetcher = fetcher
        self.max_requests = _bounded(max_requests, 'max_requests', 1000)
        self.requests = 0
        self.stopped = False
        self.blocked_by_host_cooldown = False
        self.cache = OrderedDict()
        self.cache_sizes = {}
        self.cache_bytes = 0
        self.cache_records = 0
        self.peak_cache_bytes = 0
        self.peak_cache_records = 0
        self.errors = {}
        self.retry_timeouts = retry_timeouts
        self.request_attempts = []

    def _json(self, url):
        # One escalated-timeout retry is permitted, still inside the run and
        # request budgets. Every dispatch uses safe_http's host policy; 429
        # and persisted holds are never retried. Discovery retains its
        # existing one-dispatch behavior unless explicitly enabled.
        for attempt in range(2 if self.retry_timeouts else 1):
            if self.stopped or self.requests >= self.max_requests:
                raise TimeoutError('request budget exhausted or run stopped')
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('source run deadline exceeded')
            self.requests += 1
            timeout = min(10 if self.retry_timeouts and attempt == 0 else 20, remaining)
            record = {'schema_version': 1, 'request_number': self.requests,
                      'host_scope': urlsplit(url).hostname, 'source_url': url,
                      'observed_at': utc_now().isoformat(), 'timeout_seconds': timeout,
                      'retry_index': attempt, 'transport_class': 'OK'}
            self.request_attempts.append(record)
            try:
                if self.fetcher is not None:
                    value = self.fetcher(url, timeout)
                else:
                    with urlopen(url, timeout=timeout) as response:
                        value = loads(response.read())
                if not isinstance(value, (dict, list)):
                    raise ValueError('expected object or array response')
                _deadline(self)
                return value
            except Exception as exc:
                record['transport_class'] = _transport_class(exc)
                record['exception_type'] = type(exc).__name__
                if isinstance(exc, HostRateLimited):
                    self.stopped = True
                    self.blocked_by_host_cooldown = True
                elif isinstance(exc, HTTPError) and exc.code == 429:
                    self.stopped = True
                retry = (self.retry_timeouts and attempt == 0
                         and record['transport_class'] == 'TIMEOUT'
                         and not self.stopped and self.requests < self.max_requests
                         and time.monotonic() < self.deadline)
                if not retry:
                    raise

    def read(self, ref):
        provider, board = parse_source(ref)
        if self.stopped:
            raise TimeoutError('run stopped after HTTP 429')
        _deadline(self)
        if ref in self.cache:
            self.cache.move_to_end(ref)
            return self.cache[ref]
        if ref in self.errors:
            raise ValueError('source failed earlier this run: '+self.errors[ref])
        try:
            if provider == 'greenhouse':
                endpoint = f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true'
                data = self._json(endpoint)
                jobs = data.get('jobs') if isinstance(data, dict) else None
                if not isinstance(jobs, list):
                    raise ValueError('Greenhouse response missing jobs')
            elif provider == 'ashby':
                endpoint = f'https://api.ashbyhq.com/posting-api/job-board/{board}'
                data = self._json(endpoint)
                jobs = data.get('jobs') if isinstance(data, dict) else None
                if not isinstance(jobs, list):
                    raise ValueError('Ashby response missing jobs')
            else:
                host = 'api.eu.lever.co' if provider == 'lever_eu' else 'api.lever.co'
                endpoint = f'https://{host}/v0/postings/{board}'
                jobs = []
                for offset in range(0, MAX_JOBS, PAGE_SIZE):
                    page = self._json(f'{endpoint}?mode=json&skip={offset}&limit={PAGE_SIZE}')
                    if not isinstance(page, list) or len(page) > PAGE_SIZE:
                        raise ValueError('Lever response violates page contract')
                    jobs.extend(page)
                    if len(page) < PAGE_SIZE:
                        break
                else:
                    raise ValueError('Lever board pagination incomplete at record cap')
            if len(jobs) > MAX_JOBS:
                raise ValueError('board exceeds record limit')
            normalized, normalized_bytes = {}, 0
            for offset, job in enumerate(jobs):
                if offset % 32 == 0:
                    _deadline(self)
                if not isinstance(job, dict):
                    raise ValueError('invalid source job row')
                if 'isListed' in job and type(job['isListed']) is not bool:
                    raise ValueError('source isListed must be boolean')
                if provider == 'ashby':
                    url = job.get('jobUrl')
                    key = identity(url) if isinstance(url, str) else None
                    title = job.get('title')
                else:
                    rid = job.get('id')
                    if isinstance(rid, bool) or not isinstance(rid, (str, int)) or not TOKEN.fullmatch(str(rid)):
                        raise ValueError('invalid posting identifier')
                    rid = str(rid)
                    if provider == 'greenhouse':
                        if not rid.isascii() or not rid.isdecimal():
                            raise ValueError('invalid Greenhouse posting identifier')
                        # Deterministic canonical provider identity; preserve advertised URL separately.
                        url = f'https://job-boards.greenhouse.io/{board}/jobs/{rid}'
                        title = job.get('title')
                    else:
                        host = 'jobs.eu.lever.co' if provider == 'lever_eu' else 'jobs.lever.co'
                        url = f'https://{host}/{board}/{rid}'
                        title = job.get('text')
                    key = identity(url)
                if key is None or key[:2] != (provider, board) or not isinstance(title, str) or not title.strip() or len(title) > 2000:
                    raise ValueError('source row identity/title mismatch')
                if key in normalized:
                    raise ValueError('duplicate source posting identity')
                if provider == 'ashby':
                    location = job.get('location')
                else:
                    container = job.get('location' if provider == 'greenhouse' else 'categories')
                    if container is not None and not isinstance(container, dict):
                        raise ValueError('source location/categories must be an object')
                    location = (container or {}).get('name' if provider == 'greenhouse' else 'location')
                if location is not None and (not isinstance(location, str) or len(location) > 2000):
                    raise ValueError('source location must be bounded text')
                for url_field in ('absolute_url', 'hostedUrl', 'jobUrl'):
                    advertised_value = job.get(url_field)
                    if advertised_value is not None and (not isinstance(advertised_value, str) or len(advertised_value) > 8192):
                        raise ValueError('advertised URL must be bounded text')
                advertised = job.get('absolute_url') or job.get('hostedUrl') or job.get('jobUrl')
                normalized[key] = {'identity': list(key), 'url': url, 'title': title,
                                   'location': location if isinstance(location, str) else '',
                                   'listed': job.get('isListed', True) is not False,
                                   'source_url': endpoint, 'record_sha256': digest(job),
                                   'advertised_url': advertised,
                                   'observed_at': utc_now().isoformat()}
                normalized_bytes += len(canonical(normalized[key]))
                if normalized_bytes > MAX_BOARD_BYTES:
                    raise SourceCapacityError('normalized board exceeds byte budget')
            _deadline(self)
            while self.cache and (self.cache_bytes + normalized_bytes > MAX_CACHE_BYTES
                                  or self.cache_records + len(normalized) > MAX_CACHE_RECORDS):
                old_ref, old_board = self.cache.popitem(last=False)
                self.cache_bytes -= self.cache_sizes.pop(old_ref)
                self.cache_records -= len(old_board)
            self.cache[ref] = normalized
            self.cache_sizes[ref] = normalized_bytes
            self.cache_bytes += normalized_bytes
            self.cache_records += len(normalized)
            self.peak_cache_bytes = max(self.peak_cache_bytes, self.cache_bytes)
            self.peak_cache_records = max(self.peak_cache_records, self.cache_records)
            return normalized
        except Exception as exc:
            self.errors[ref] = type(exc).__name__
            raise


def discover(workspace, *, max_new=200, timeout=120, titles=(), locations=(), reader=None,
             source_refs=None):
    _bounded(max_new, 'max_new', 2000)
    if (Path(workspace)/'DEMO_ONLY.json').exists() and reader is None:
        raise ValueError('synthetic workspace cannot perform live source reads')
    sources = _sources(workspace)
    if source_refs is not None:
        # The durable scheduler can narrow intake, never introduce an unregistered
        # endpoint or activate a disabled source. Preserve the caller's fair order.
        if (type(source_refs) not in (tuple, list) or not 1 <= len(source_refs) <= MAX_BOARDS
                or any(type(ref) is not str for ref in source_refs)
                or len(set(source_refs)) != len(source_refs)):
            raise ValueError('source selection must contain distinct registered references')
        registered = {source['ref']: source for source in sources}
        if any(ref not in registered for ref in source_refs):
            raise ValueError('source selection includes a disabled or unregistered reference')
        sources = [registered[ref] for ref in source_refs]
    reader = reader or PublicBoardReader(timeout)
    source_results, candidate_count, spool_bytes = [], 0, 0
    # TemporaryFile uses owner-only creation and is removed on every return,
    # exception, and normal process close. Never persist raw source bodies.
    with tempfile.TemporaryFile(mode='w+b', prefix='keel-discovery-') as spool:
        for source in sources:
            try:
                _deadline(reader)
                postings = reader.read(source['ref'])
                source_results.append({'ref': source['ref'], 'status': 'complete', 'postings': len(postings)})
                for index, (key, posting) in enumerate(postings.items()):
                    if index % 32 == 0:
                        _deadline(reader)
                    if not posting['listed']:
                        continue
                    if titles and not any(str(term).casefold() in posting['title'].casefold() for term in titles):
                        continue
                    if locations and not any(str(term).casefold() in posting['location'].casefold() for term in locations):
                        continue
                    rid = 'SRC-'+hashlib.sha256(':'.join(key).encode()).hexdigest()[:32]
                    row = {'role_id': rid, 'company': source.get('company_label') or key[1],
                           'title': posting['title'], 'location': posting['location'],
                           'application_url': posting['url'], 'ats': key[0], 'ats_board': source['ref'],
                           'source_identity': list(key), 'source_observation': posting,
                           'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': None,
                           'qualification_assessed': False, 'action_band': 'REVIEW',
                           'created_at': utc_now().isoformat(), 'holds': [],
                           'execution_authorized': False}
                    encoded = canonical(row)+b'\n'
                    if (candidate_count >= MAX_CANDIDATE_RECORDS
                            or spool_bytes + len(encoded) > MAX_CANDIDATE_BYTES):
                        raise SourceCapacityError('candidate spool budget exhausted')
                    try:
                        spool.write(encoded)
                    except OSError as exc:
                        raise SourceStorageError('candidate spool write failed') from exc
                    candidate_count += 1
                    spool_bytes += len(encoded)
            except (SourceCapacityError, TimeoutError) as exc:
                # Never present a resource-truncated run as complete, nor ingest
                # the prefix and silently starve later candidates on replay.
                return {'added': 0, 'status': 'HELD_HTTP_429' if reader.stopped else
                        'HELD_CAPACITY' if isinstance(exc, SourceCapacityError) else 'HELD_DEADLINE',
                        'sources': source_results, 'requests': reader.requests,
                        'candidate_records': candidate_count, 'candidate_spool_bytes': spool_bytes,
                        'submission_authorized': False}
            except SourceStorageError:
                return {'added': 0, 'status': 'HELD_STORAGE', 'sources': source_results,
                        'requests': reader.requests, 'submission_authorized': False}
            except Exception as exc:
                source_results.append({'ref': source['ref'], 'status': 'unavailable', 'reason': type(exc).__name__})
        if reader.stopped:
            return {'added': 0, 'status': 'HELD_HTTP_429', 'sources': source_results, 'requests': reader.requests}
        added, duplicates = [], 0
        with queue_lock(timeout=_lock_budget(reader), owner='public-source:commit'):
            try:
                _deadline(reader)
                documents, ledger = _documents(workspace)
                existing = [row for _, row in _all_rows(documents)] + ledger
                ids = {row.get('role_id') for row in existing}
                keys = {key for row in existing if (key := _key(row)) is not None}
                target = _queue_path(workspace, 'standard')
                document = documents[target]
                output = rows(document)
                spool.seek(0)
                for index, encoded in enumerate(spool):
                    if index % 32 == 0:
                        _deadline(reader)
                    row = loads(encoded)
                    key = tuple(row['source_identity'])
                    if row['role_id'] in ids or key in keys:
                        duplicates += 1
                        continue
                    if len(added) >= max_new:
                        break
                    output.append(row); added.append(row['role_id']); ids.add(row['role_id']); keys.add(key)
                _deadline(reader)
            except TimeoutError:
                return {'added': 0, 'status': 'HELD_DEADLINE', 'sources': source_results,
                        'requests': reader.requests, 'candidate_records': candidate_count,
                        'candidate_spool_bytes': spool_bytes, 'submission_authorized': False}
            if added:
                atomic_json(target, document)
        return {'status': 'PARTIAL_SOURCES' if any(x['status'] != 'complete' for x in source_results) else 'COMPLETE',
                'added': len(added), 'duplicate_observations': duplicates, 'role_ids': added,
                'sources': source_results, 'requests': reader.requests,
                'candidate_records': candidate_count, 'candidate_spool_bytes': spool_bytes,
                'candidate_retention': 'bounded_temporary_spool',
                'fit_or_eligibility_claimed': False, 'submission_authorized': False}


def _observation_matches(entry, key):
    observation = entry.get('posting_verification')
    return (isinstance(observation, dict) and key is not None
            and observation.get('identity') == list(key))


def posting_is_current(entry, key=None, *, now=None):
    """A preserved historical LIVE record is not current after a failed recheck.

    Legacy posting_verification records remain readable. New typed attempts
    must match the target and signal LIVE if at least as recent as the
    decisive observation. Malformed latest-attempt state fails closed.
    """
    key = _key(entry) if key is None else key
    now = now or utc_now()
    observation = entry.get('posting_verification')
    if (not _observation_matches(entry, key) or observation.get('verdict') != 'live'
            or not fresh(observation.get('observed_at'), POSTING_FRESH_SECONDS, now=now)):
        return False
    attempt = entry.get('verification_attempt')
    if attempt is None:
        return True
    if not isinstance(attempt, dict):
        return False
    if attempt.get('identity') != list(key):
        return False
    try:
        at = aware_time(attempt.get('observed_at'))
        previous = aware_time(observation.get('observed_at'))
        if at > now or attempt.get('signal') not in {'LIVE', 'AMBIGUOUS', 'NONE'}:
            return False
        if at >= previous and attempt.get('signal') != 'LIVE':
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def _next_time(entry, key):
    # Scheduling comes from the latest attempt, independently of the last
    # usable posting observation. Transport failures can pace retries without
    # destroying decisive evidence or changing a quarantine streak.
    observation = entry.get('verification_attempt')
    if observation is None:
        observation = entry.get('posting_verification') or {}
    if not isinstance(observation, dict):
        return 'invalid'
    # A row may be retargeted after a prior observation. Its old posting's
    # retry timestamp cannot defer verification of a different exact posting.
    # Legacy scheduling records without an identity retain their cooldown;
    # they do not establish current posting presence.
    if 'identity' in observation and (key is None or observation['identity'] != list(key)):
        return None
    stamp = observation.get('next_eligible_at')
    if not stamp:
        return None
    try:
        return aware_time(stamp)
    except (ValueError, TypeError, OverflowError):
        # Invalid scheduling state is visible, not silently actionable.
        return 'invalid'


def verify(workspace, *, limit=100, timeout=120, live=False, reader=None,
           role_ids=None, expected_rows=None, expected_inputs=None, run_id=None, commit_guard=None):
    _bounded(limit, 'limit', 1000)
    if role_ids is not None:
        if (not isinstance(role_ids, (list, tuple)) or not 1 <= len(role_ids) <= 25
                or any(not isinstance(rid, str) or not rid.strip() for rid in role_ids)
                or len(set(role_ids)) != len(role_ids)
                or not isinstance(expected_rows, dict) or set(expected_rows) != set(role_ids)):
            raise ValueError('bounded verification selection requires exact planned rows')
        if run_id is None:
            raise ValueError('bounded verification requires a retained run identity')
        from muse_bridge import INPUTS, secure_bytes
        policy_inputs = set(INPUTS) - {'data/application-ledger.json'} - {
            'data/queues/' + name + '-queue.json' for name in QUEUES}
        if (not isinstance(expected_inputs, dict) or set(expected_inputs) != policy_inputs
                or any(not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{64}', v) for v in expected_inputs.values())):
            raise ValueError('bounded verification requires the complete planned policy inputs')
    elif expected_rows is not None or expected_inputs is not None:
        raise ValueError('planned rows require a bounded role selection')
    if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', run_id)):
        raise ValueError('invalid verification run identity')
    if (Path(workspace)/'DEMO_ONLY.json').exists() and reader is None:
        raise ValueError('synthetic workspace cannot perform live verification')
    reader = reader or PublicBoardReader(timeout, retry_timeouts=True)
    run_id, now = run_id or uuid.uuid4().hex, utc_now()
    initial_flush = (flush_outbox(workspace, role_ids=role_ids) if role_ids is not None else flush_outbox(workspace)) if live else {'emitted': 0, 'pending': 0}
    with queue_lock(timeout=_lock_budget(reader), owner='public-verify:snapshot', recover=live):
        _deadline(reader)
        documents, ledger = _documents(workspace)
        if role_ids is not None and any(hashlib.sha256(secure_bytes(workspace, name)).hexdigest() != value
                                        for name, value in expected_inputs.items()):
            raise ValueError('policy inputs changed since bounded plan')
        selected = copy.deepcopy(_all_rows(documents))
    selected, id_counts, posting_counts = _identity_index(selected, reader)
    terminal_ids, terminal_keys = _terminal_ledger_index(ledger, reader)
    eligible, skipped = [], Counter()
    for index, (path, row, key) in enumerate(selected):
        if index % 32 == 0:
            _deadline(reader)
        rid = row.get('role_id')
        if role_ids is not None:
            if rid not in role_ids:
                skipped['outside_bounded_cohort'] += 1; continue
            if row != expected_rows[rid]:
                skipped['changed_since_plan'] += 1; continue
            from ready_gate import ledger_holds
            if ledger_holds(row, ledger):
                skipped['bounded_ledger_hold'] += 1; continue
        if not isinstance(rid, str) or not rid or id_counts[rid] != 1:
            skipped['duplicate_or_invalid_role_id'] += 1; continue
        if row.get('verification_event_pending'):
            skipped['telemetry_pending'] += 1; continue
        if _held(row) or rid in terminal_ids or key is not None and key in terminal_keys:
            skipped['held_or_active_or_terminal'] += 1; continue
        next_time = _next_time(row, key)
        if next_time == 'invalid':
            skipped['invalid_cooldown_state'] += 1; continue
        if next_time is not None and next_time > now:
            skipped['cooldown'] += 1; continue
        if key is None:
            skipped['missing_exact_identity'] += 1; continue
        if posting_counts[key] != 1:
            skipped['duplicate_exact_posting'] += 1; continue
        # Oldest observation first, then stable ID: failures cool down instead of starving the tail.
        stamp = (row.get('verification_attempt') or row.get('posting_verification') or {}).get('observed_at', '')
        try:
            # Canonical UTC text orders instants and gives equivalent spellings
            # the same key. Keep legacy missing/malformed fallback behavior;
            # this ordering key does not establish freshness or eligibility.
            stamp = aware_time(stamp).isoformat(timespec='microseconds')
        except (ValueError, TypeError, OverflowError):
            pass
        eligible.append((stamp, rid, path, row, key))
    eligible.sort(key=lambda x: (str(x[0]), x[1]))
    scan_fit = Counter()
    for _, _, _, row, _ in eligible:
        fit = row.get('fit_score')
        if isinstance(fit, bool) or not isinstance(fit, (int, float)) or not math.isfinite(fit):
            scan_fit['unknown_fit'] += 1
        elif fit >= main_floor():
            scan_fit['scannable_at_main_floor'] += 1
        else:
            scan_fit['scannable_below_main_floor'] += 1
    overflow = max(0, len(eligible)-limit)
    chosen = eligible[:limit]
    # Fix the same oldest-first cohort before batching. Process each complete
    # board once even when its records exceed the shared LRU retention budget;
    # observations and commits are restored to the original selected order.
    boards = OrderedDict()
    for item in chosen:
        boards.setdefault(':'.join(item[4][:2]), []).append(item)
    by_role, deferred, board_reads = {}, Counter(), 0
    for ref, cohort in boards.items():
        _deadline(reader)
        if reader.stopped:
            deferred['http_429'] += len(cohort)
            continue
        if _request_budget_blocks(reader, ref):
            deferred['request_budget'] += len(cohort)
            continue
        board_reads += 1
        request_start = len(getattr(reader, 'request_attempts', []))
        try:
            postings = reader.read(ref)
            failure = None
        except HostRateLimited:
            # The persisted transport hold is not fresh posting evidence or a
            # new failed verification. Preserve this cohort's old retry state.
            deferred['persisted_http_429'] += len(cohort)
            continue
        except Exception as exc:
            postings = {}
            # No board transport failure can identify a lead as ambiguous or
            # dead. In particular 404/410 on this endpoint is board evidence.
            failure = exc
            if isinstance(exc, HTTPError) and exc.code == 429:
                reader.stopped = True
        request_attempts = copy.deepcopy(getattr(reader, 'request_attempts', [])[request_start:])
        for index, (_, rid, path, snapshot, key) in enumerate(cohort):
            if index % 32 == 0:
                _deadline(reader)
            job = postings.get(key)
            result = VerificationResult(
                'NONE' if failure else 'LIVE' if job else 'AMBIGUOUS',
                _transport_class(failure) if failure else 'OK',
                {'kind': 'transport_failure' if failure else 'exact_published_posting' if job else
                    'posting_absent_from_complete_board', 'host_scope': _provider_host(ref),
                 'source_ref': ref, 'request_attempts': request_attempts},
                POSTING_FRESH_SECONDS if job else VERIFY_RETRY_SECONDS)
            verdict = {'LIVE': 'live', 'AMBIGUOUS': 'ambiguous', 'NONE': 'none'}[result.signal]
            reason = ('source_unavailable:'+type(failure).__name__ if failure else
                      'exact_published_posting' if job else
                      'posting_absent_from_board_not_death_evidence')
            observed = utc_now()
            observation = {'schema_version': 1, 'observation_id': run_id+':'+hashlib.sha256(rid.encode()).hexdigest()[:24],
                           'observed_at': observed.isoformat(), 'verdict': verdict, 'reason': reason,
                           'signal': result.signal, 'transport_class': result.transport_class,
                           'evidence': result.evidence, 'retry_after': result.retry_after,
                           'lead_attributed': result.signal != 'NONE',
                           'identity': list(key), 'source_record_sha256': job['record_sha256'] if job else None,
                           'source_url': job['source_url'] if job else None,
                           'next_eligible_at': (observed+timedelta(seconds=result.retry_after)).isoformat(),
                           'recheck_route': {'kind': 'public_board', 'source_ref': ref,
                                             'identity': list(key), 'host_scope': _provider_host(ref)},
                           'quarantine_admission': False,
                           'form_verified': False, 'acceptance_verified': False, 'execution_authorized': False}
            by_role[rid] = (path, snapshot, observation)
    observations = [by_role[item[1]] for item in chosen if item[1] in by_role]
    # A 429 stops all further dispatches. Prior successful reads still contain
    # their actual posting evidence; an unrelated board's rate limit cannot
    # retroactively turn those observations into lead ambiguity.
    transport_cohorts = {}
    for _, _, observation in observations:
        if observation['signal'] != 'NONE':
            continue
        scope = observation['evidence']['host_scope']
        transport = observation['transport_class']
        cohort_id = 'transport-'+digest([run_id, scope, transport])[:24]
        observation['transport_cohort_id'] = cohort_id
        aggregate = transport_cohorts.setdefault(cohort_id, {
            'schema_version': 1, 'cohort_id': cohort_id, 'host_scope': scope,
            'transport_class': transport, 'role_attempt_count': 0, 'source_refs': []})
        aggregate['role_attempt_count'] += 1
        ref = observation['evidence']['source_ref']
        if ref not in aggregate['source_refs']:
            aggregate['source_refs'].append(ref)
    # Never commit observations after a cooperative run deadline expired.
    _deadline(reader)
    committed, conflicts, errors = [], [], []
    committed_verdicts = Counter()
    if live and observations:
        # Crash-safe per file, not a transaction spanning multiple JSON files.
        # Each committed row contains its own replayable telemetry outbox.
        with queue_lock(timeout=_lock_budget(reader), owner='public-verify:commit'):
            _deadline(reader)
            current_documents, current_ledger = _documents(workspace)
            policy_matches = role_ids is None or all(
                hashlib.sha256(secure_bytes(workspace, name)).hexdigest() == value
                for name, value in expected_inputs.items())
            current, counts, current_keys = _identity_index(_all_rows(current_documents), reader)
            ledger_ids, ledger_keys = _terminal_ledger_index(current_ledger, reader)
            grouped = defaultdict(list)
            by_path_and_id = defaultdict(list)
            for index, (current_path, current_row, _) in enumerate(current):
                if index % 32 == 0:
                    _deadline(reader)
                by_path_and_id[(current_path, current_row.get('role_id'))].append(current_row)
            for index, (path, snapshot, observation) in enumerate(observations):
                if index % 32 == 0:
                    _deadline(reader)
                rid = snapshot['role_id']
                found = by_path_and_id[(path, rid)]
                key = tuple(observation['identity'])
                if (not policy_matches or counts[rid] != 1 or len(found) != 1 or found[0] != snapshot
                        or current_keys[key] != 1 or _held(found[0])
                        or rid in ledger_ids or key in ledger_keys):
                    conflicts.append(rid); continue
                row = found[0]
                if role_ids is not None:
                    from ready_gate import ledger_holds
                    if ledger_holds(row, current_ledger) or commit_guard is not None and not commit_guard(row):
                        conflicts.append(rid); continue
                row['verification_attempt'] = observation
                if observation['signal'] != 'NONE':
                    row['posting_verification'] = copy.deepcopy(observation)
                row['verification_event_pending'] = {'event_id': observation['observation_id'],
                                                     'event_type': 'verification_attempt', 'role_id': rid,
                                                     'source': 'public-board-verifier', 'details': observation}
                grouped[path].append(rid)
            for path, ids in grouped.items():
                try:
                    _deadline(reader)
                    # Rich attempt evidence must never create a queue that
                    # the bounded JSON reader can no longer reopen.
                    if len(canonical(current_documents[path])) > MAX_JSON_BYTES:
                        raise OSError('verification queue exceeds JSON read budget')
                    atomic_json(path, current_documents[path])
                    committed.extend(ids)
                    # Observed verdicts include conflicts and failed writes.
                    # Credit only the rows in this successfully persisted file.
                    committed_verdicts.update(by_role[rid][2]['verdict'] for rid in ids)
                except OSError as exc:
                    errors.append({'queue': path.name, 'reason': type(exc).__name__, 'not_committed': ids})
        flush = flush_outbox(workspace, role_ids=role_ids) if role_ids is not None else flush_outbox(workspace)
    else:
        # An idle or wholly deferred run has no row writes or new events. Do
        # not reread all queues twice just to commit and flush an empty batch.
        flush = initial_flush
    report = {'schema_version': 1, 'run_id': run_id, 'observed_at': now.isoformat(), 'dry_run': not live,
              'selected': len(chosen), 'deferred_by_limit': overflow, 'skipped': dict(skipped),
              'candidate_diagnostics': {'queue_rows': len(selected), 'scannable': len(eligible),
                                        'selected': len(chosen), 'unscannable': sum(skipped.values()),
                                        'scannable_at_main_floor': scan_fit['scannable_at_main_floor'],
                                        'scannable_below_main_floor': scan_fit['scannable_below_main_floor'],
                                        'unknown_fit': scan_fit['unknown_fit'], 'main_fit_floor': main_floor(),
                                        'promoted_supply_claimed': False},
              'observed': len(observations), 'deferred_without_attempt': dict(deferred),
              'selected_boards': len(boards), 'board_reads': board_reads,
              'verdicts': dict(Counter(o['verdict'] for _, _, o in observations)),
              'committed': len(committed), 'committed_verdicts': dict(committed_verdicts),
              'concurrent_conflicts': conflicts, 'write_errors': errors,
              'requests': reader.requests, 'telemetry': flush, 'rate_limit_hold': reader.stopped,
              'signals': dict(Counter(o['signal'] for _, _, o in observations)),
              'transport_classes': dict(Counter(o['transport_class'] for _, _, o in observations)),
              'transport_cohorts': list(transport_cohorts.values()),
              'transport_storm_threshold_policy': 'owner_decision_required; no quarantine or provider hold inferred',
              'request_attempts': copy.deepcopy(getattr(reader, 'request_attempts', [])),
              'request_count_scope': 'bounded_reader_dispatches',
              'promoted_to_ready': 0, 'submission_authorized': False,
              'observation_scope': 'posting_presence_only; form, eligibility and human approval remain separate'}
    if live:
        atomic_json(Path(workspace)/'data/verification-runs'/(run_id+'.json'), report)
    return report


def flush_outbox(workspace, logger=None, *, role_ids=None):
    """Durable events first; batch acknowledgments once per queue file.

    A crash between append and acknowledgment replays the same event ID.
    Concurrent row changes do not erase newer pending events. The queue file
    is rewritten once per acknowledgment batch, not once for every lead.
    """
    batch_logger = None
    if logger is None:
        import log_event
        logger = log_event.log
        batch_logger = log_event.log_batch
    with queue_lock(timeout=10, owner='verification-outbox:snapshot'):
        documents, _ = _documents(workspace)
        pending = [(path, row['role_id'], copy.deepcopy(row['verification_event_pending']))
                   for path, row in _all_rows(documents) if row.get('verification_event_pending')
                   and (role_ids is None or row.get('role_id') in role_ids)]
    accepted, errors = defaultdict(list), []
    batch_size = log_event.MAX_BATCH_EVENTS if batch_logger is not None else 1
    for start in range(0, len(pending), batch_size):
        group = pending[start:start + batch_size]
        try:
            requests = [{'event_type': event['event_type'], 'role_id': rid, 'source': event['source'],
                         'details': event['details'], 'event_id': event['event_id']}
                        for _, rid, event in group]
            if batch_logger is not None:
                receipts = batch_logger(requests)
            else:
                first = requests[0]
                receipts = [logger(first['event_type'], **{k: v for k, v in first.items() if k != 'event_type'})]
            if (type(receipts) is not list or len(receipts) != len(group)
                    or any(not isinstance(receipt, dict) or receipt.get('event_id') != event['event_id']
                           for receipt, (_, _, event) in zip(receipts, group))):
                raise ValueError('logger returned no durable receipt')
            for path, rid, event in group:
                accepted[path].append((rid, event))
        except Exception as exc:
            errors.extend({'role_id': rid, 'reason': type(exc).__name__} for _, rid, _ in group)
    emitted = 0
    for path, acknowledgments in accepted.items():
        try:
            with queue_lock(timeout=10, owner='verification-outbox:ack'):
                document = read_json(path)
                index = defaultdict(list)
                for row in rows(document):
                    index[row.get('role_id')].append(row)
                cleared = []
                for rid, event in acknowledgments:
                    matching = index[rid]
                    if len(matching) == 1 and matching[0].get('verification_event_pending') == event:
                        matching[0].pop('verification_event_pending')
                        cleared.append(rid)
                if cleared:
                    atomic_json(path, document)
                    emitted += len(cleared)
        except Exception as exc:
            errors.append({'queue': path.name, 'reason': type(exc).__name__})
    return {'emitted': emitted, 'pending': len(pending)-emitted, 'errors': errors}


def supply_report(workspace):
    with queue_lock(timeout=10, owner='supply:read', recover=False):
        documents, ledger = _documents(workspace)
        all_rows = _all_rows(documents)
    indexed, identities, posting_counts = _identity_index(all_rows)
    counts = Counter()
    terminal_ids, terminal_keys = _terminal_ledger_index(ledger)
    now = utc_now()
    questions = defaultdict(list)
    for _, row, key in indexed:
        rid = row.get('role_id')
        if not isinstance(rid, str) or not rid or identities[rid] != 1:
            counts['identity_conflict'] += 1
        elif _held(row) or rid in terminal_ids or key is not None and key in terminal_keys:
            counts['held_active_or_terminal'] += 1
        elif row.get('verification_event_pending'):
            counts['telemetry_pending'] += 1
        elif key is None:
            counts['missing_exact_posting_identity'] += 1
        elif posting_counts[key] != 1:
            counts['identity_conflict'] += 1
        elif posting_is_current(row, key, now=now):
            counts['posting_verified_form_and_approval_separate'] += 1
        elif (next_time := _next_time(row, key)) == 'invalid':
            counts['invalid_cooldown'] += 1
        elif next_time and next_time > now:
            counts['verification_cooldown'] += 1
        else:
            counts['actionable_verification'] += 1
        unresolved = row.get('unresolved') or []
        if isinstance(unresolved, list):
            for question in unresolved:
                # Exact text only. Do not merge commitments, options or differently worded attestations.
                if isinstance(question, str) and question.strip():
                    if isinstance(rid, str) and rid:
                        questions[question].append(rid)
    return {'observed_at': utc_now().isoformat(), 'queue_rows': len(all_rows), 'ledger_rows': len(ledger),
            'mutually_exclusive_supply_states': dict(counts),
            'conservation_ok': sum(counts.values()) == len(all_rows),
            'registered_sources': len(_sources(workspace)),
            'exact_question_groups': [{'question': q, 'roles': sorted(set(ids)),
                                       'question_sha256': hashlib.sha256(q.encode()).hexdigest(),
                                       'auto_answer_authorized': False}
                                      for q, ids in sorted(questions.items(), key=lambda x: -len(x[1]))],
            'note': 'Posting presence is not form completeness, applicant eligibility, READY or submission approval.'}


def validate_preparation_packet(workspace, packet_path):
    """Check current review inputs without recovering queues or granting authority."""
    import apply_loop
    import packet_contract
    import ready_gate
    from safe_io import contained_path
    workspace = Path(workspace).absolute()
    with queue_lock(timeout=10, owner='validate-packet', recover=False):
        packet = read_json(contained_path(workspace / 'data/launch-packets', packet_path))
        role_id = packet['role_id']
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError('packet role_id is required')
        # Missing queues cannot establish that the target is unique. Unlike
        # intake, validation must not interpret absent canonical files as empty.
        documents = { _queue_path(workspace, name): read_json(_queue_path(workspace, name))
                      for name in QUEUES }
        ledger = rows(read_json(workspace / 'data/application-ledger.json'))
        current = _all_rows(documents)
        matches = [(path, row) for path, row in current if row.get('role_id') == role_id]
        if len(matches) != 1:
            raise ValueError('role_id missing or duplicated')
        path, entry = matches[0]
        posting = _key(entry)
        if posting is None or sum(_key(row) == posting for _, row in current) != 1:
            raise ValueError('posting identity missing or duplicated across queues')
        if ready_gate.ledger_holds(entry, ledger):
            raise ValueError('role or posting identity has unresolved ledger history')
        origin = path.name.removesuffix('-queue.json')
        if (origin not in {'standard', 'strategic'} or _held(entry)
                or ready_gate.entry_hold_reasons(entry)):
            raise ValueError('role has an active, terminal or explicit hold state')
        if not posting_is_current(entry, posting):
            raise ValueError('fresh exact posting verification required')
        packet_contract.validate(packet, entry, read_json(workspace / 'data/answer_bank.json'),
                                 read_json(workspace / 'data/policy.json'), workspace,
                                 apply_loop._materials_for(entry, origin))
    return {'valid_preparation_packet': True, 'execution_authorized': False}


def prepare_role(workspace, role_id, resume, *, _offline_fixture=False):
    """An explicit operator selection prepares one review packet; never changes READY.

    Resume must already be inside the selected workspace. Human holds remain
    holds. After network/form work, all queue, answer and material inputs are
    checked again before a packet can be returned as current.
    """
    if (Path(workspace)/'DEMO_ONLY.json').exists() and not _offline_fixture:
        raise ValueError('synthetic workspace cannot perform live preparation')
    import apply_loop
    import packet_contract
    import launch_lock
    from safe_io import contained_path, file_digest
    workspace = Path(workspace).absolute()
    material = contained_path(workspace, resume)
    file_digest(material)
    if material.suffix.lower() not in {'.pdf', '.docx', '.txt', '.md'}:
        raise ValueError('resume must be a PDF, DOCX, TXT or Markdown file')
    relative = str(material.relative_to(workspace))
    def selection():
        documents, ledger = _documents(workspace)
        matches = [(path, row) for path, row in _all_rows(documents) if row.get('role_id') == role_id]
        if len(matches) != 1:
            raise ValueError('role_id missing or duplicated')
        path, row = matches[0]
        posting = _key(row)
        if posting is None or sum(_key(other) == posting for _, other in _all_rows(documents)) != 1:
            raise ValueError('posting identity missing or duplicated across queues')
        ledger_ids, ledger_keys = _terminal_ledger_index(ledger)
        if role_id in ledger_ids or posting in ledger_keys:
            raise ValueError('role or exact posting has an active or terminal ledger outcome')
        if _held(row):
            raise ValueError('role has an active, terminal or explicit hold state')
        observation = row.get('posting_verification') or {}
        if not posting_is_current(row, posting):
            raise ValueError('fresh exact posting verification required; run verify --live')
        if observation.get('identity') != list(_key(row) or []):
            raise ValueError('posting identity changed since verification')
        return documents, path, row, posting

    # Refused acquisition must not invalidate a previously reviewed packet.
    # Do not recover pending queue transactions during this initial inspection.
    with queue_lock(timeout=10, owner='prepare-role:inspect', recover=False):
        _, original_path, row, posting = selection()
        original = copy.deepcopy(row)
    task = 'prepare-'+uuid.uuid4().hex
    acquired = False

    def require_lease():
        lease = launch_lock.check(role_id)
        if not lease or lease.get('task_id') != task or lease.get('role_id') != role_id:
            raise ValueError('preparation lease lost')

    try:
        acquired, info = launch_lock.prelaunch_guard(
            role_id, task, original.get('company', ''), original.get('title', ''),
            ledger_path=str(workspace/'data/application-ledger.json'))
        if not acquired:
            raise ValueError('preparation lease refused: '+str(info.get('status')))
        with queue_lock(timeout=10, owner='prepare-role:select', recover=False):
            documents, path, row, posting = selection()
            if path != original_path or row != original:
                raise ValueError('role changed during preparation lease acquisition')
            require_lease()
            row['materials'] = {**(row.get('materials') or {}), 'resume': relative}
            row['preparation_selection'] = {'selected_at': utc_now().isoformat(), 'scope': 'review_only'}
            atomic_json(path, documents[path])
            selected = copy.deepcopy(row)
        # Preparation-only path: build the modern packet contract directly.
        # (apply_loop.build_packet emits the legacy launch-packet schema,
        # which packet_contract.validate rejects. The 0.4.0 candidate routed
        # through build_packet; reverted here — the validate call below is
        # unchanged and requires the modern schema.)
        bank = apply_loop.load_answer_bank()
        policy = apply_loop.load_policy()
        materials = apply_loop._materials_for(selected, 'standard')
        intel = {"questions": [], "ats": "unknown",
                 "source_url": packet_contract.source_url(selected)}
        packet = packet_contract.prepare(selected, bank, policy,
                                         str(workspace), materials, intel)
        with queue_lock(timeout=10, owner='prepare-role:validate', recover=False):
            documents, ledger = _documents(workspace)
            current_rows = _all_rows(documents)
            matches = [row for _, row in current_rows if row.get('role_id') == role_id]
            ledger_ids, ledger_keys = _terminal_ledger_index(ledger)
            if (len(matches) != 1 or matches[0] != selected
                    or sum(_key(row) == posting for _, row in current_rows) != 1
                    or role_id in ledger_ids or posting in ledger_keys):
                raise ValueError('role or ledger changed during preparation')
            if not posting_is_current(matches[0], posting):
                raise ValueError('posting verification expired during preparation')
            packet_contract.validate(packet, matches[0], apply_loop.load_answer_bank(), apply_loop.load_policy(),
                                     str(workspace), apply_loop._materials_for(matches[0], 'standard'))
            require_lease()
            # Publish only the validated candidate. A failed or fenced builder
            # must neither overwrite nor unlink a prior/successor packet.
            packet_path = str(workspace / 'data' / 'launch-packets' / f"{role_id}.json")
            atomic_json(Path(packet_path), packet)
        return {'packet': packet_path, 'status': packet['status'], 'execution_authorized': False,
                'review_requirements': packet['review_requirements']}
    finally:
        # The guard can be interrupted after it publishes our lease but before
        # returning. Task-bound release also refuses to remove a successor's lease.
        if acquired or (launch_lock.check(role_id) or {}).get('task_id') == task:
            ok, info = launch_lock.release(role_id, task)
            if not ok:
                raise RuntimeError('preparation lease release failed: '+str(info))
