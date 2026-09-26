"""Exact public posting identity. A published posting is not application acceptance.

The three supported API shapes are pinned by fixtures and primary documentation
in docs/daybreak-source-contracts.md. Title overlap is deliberately not evidence.
"""
from __future__ import annotations
import re
from urllib.parse import urlsplit, parse_qs
from safe_http import validate_url, NetworkPolicyError
from safe_io import canonical

TOKEN = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")


def identity(url: str):
    """Return (provider, board, posting_id), or None. Never substring-match hosts."""
    try:
        p = urlsplit(validate_url(url))
    except (NetworkPolicyError, TypeError) as exc:
        raise ValueError("invalid public posting URL") from exc
    host = p.hostname
    parts = p.path.strip('/').split('/')
    if host in {'boards.greenhouse.io', 'job-boards.greenhouse.io'}:
        if p.path == '/embed/job_app':
            q = parse_qs(p.query)
            if len(q.get('for', [])) == len(q.get('token', [])) == 1:
                board, rid = q['for'][0], q['token'][0]
                if TOKEN.fullmatch(board) and rid.isascii() and rid.isdecimal():
                    return 'greenhouse', board, rid
        if len(parts) == 4 and parts[0] == 'boards':
            parts = parts[1:]
        if len(parts) == 3 and parts[1] == 'jobs' and TOKEN.fullmatch(parts[0]) and parts[2].isascii() and parts[2].isdecimal():
            return 'greenhouse', parts[0], parts[2]
    providers = {'jobs.lever.co': 'lever', 'jobs.eu.lever.co': 'lever_eu',
                 'jobs.ashbyhq.com': 'ashby'}
    if host in providers:
        if len(parts) == 3 and parts[-1] == 'apply':
            parts.pop()
        if len(parts) == 2 and all(TOKEN.fullmatch(x) for x in parts):
            return providers[host], parts[0], parts[1]
    return None


def plan(url: str):
    key = identity(url)
    if key is None:
        return None
    provider, board, rid = key
    if provider == 'greenhouse':
        endpoint = f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{rid}?questions=true'
    elif provider in {'lever', 'lever_eu'}:
        host = 'api.eu.lever.co' if provider == 'lever_eu' else 'api.lever.co'
        endpoint = f'https://{host}/v0/postings/{board}/{rid}?mode=json'
    else:
        endpoint = f'https://api.ashbyhq.com/posting-api/job-board/{board}'
    return key, endpoint


def observe(url: str, payload):
    """Validate identity using the requested API's actual record, not its title."""
    key = identity(url)
    if key is None:
        return {'verdict': 'ambiguous', 'reason': 'unsupported_exact_identity'}
    provider, board, rid = key
    canonical(payload)  # reject NaN, unsupported values and invalid JSON shapes
    if provider in {'greenhouse', 'lever', 'lever_eu'}:
        if not isinstance(payload, dict):
            raise ValueError('posting response must be an object')
        actual = payload.get('id')
        if isinstance(actual, bool) or not isinstance(actual, (str, int)) or str(actual) != rid:
            return {'verdict': 'ambiguous', 'reason': 'posting_id_mismatch'}
        title = payload.get('title' if provider == 'greenhouse' else 'text')
        record = payload
    else:
        if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list):
            raise ValueError('Ashby response has no jobs array')
        if len(payload['jobs']) > 20000:
            raise ValueError('board exceeds 20000 records')
        matches = []
        for record in payload['jobs']:
            if not isinstance(record, dict):
                raise ValueError('invalid board row')
            other = record.get('jobUrl') or record.get('job_url')
            if not isinstance(other, str):
                continue
            try:
                if identity(other) == key:
                    matches.append(record)
            except ValueError:
                continue
        if len(matches) != 1:
            return {'verdict': 'ambiguous', 'reason': 'posting_not_uniquely_present'}
        record = matches[0]
        title = record.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 2000:
        return {'verdict': 'ambiguous', 'reason': 'posting_title_missing_or_invalid'}
    return {'verdict': 'live', 'reason': 'exact_published_posting',
            'identity': list(key), 'title': title, 'acceptance_verified': False,
            'form_verified': False}
