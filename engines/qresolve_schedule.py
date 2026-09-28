"""Bounded, deterministic inspection turns; never answer authority or receipts.

At least half of each batch is reserved for the oldest inspection turns. The
remaining capacity prefers new/changed inputs in the caller's value ranking.
New arrivals get the current turn, so even continuous high-value arrivals cannot
indefinitely jump cards already waiting. Only completed live inspections advance
the supplied state; previews call the same pure planner without persisting it.
"""
from __future__ import annotations

import re

SCHEMA = 'keel.qresolve.schedule.v1'
MAX_CARDS = 20_000
MAX_GENERATION = 2 ** 53 - 1
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def empty_state():
    return {'schema': SCHEMA, 'generation': 0, 'entries': {}}


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def validate(state):
    """Reject damaged/unknown state instead of silently resetting fair turns."""
    if (type(state) is not dict or set(state) != {'schema', 'generation', 'entries'}
            or state['schema'] != SCHEMA or type(state['generation']) is not int
            or not 0 <= state['generation'] <= MAX_GENERATION
            or type(state['entries']) is not dict or len(state['entries']) > MAX_CARDS):
        raise ValueError('invalid qresolve scheduling state')
    generation = state['generation']
    for identity, row in state['entries'].items():
        if (not _hash(identity) or type(row) is not dict
                or set(row) != {'revision', 'first_seen', 'last_scanned'}
                or type(row['first_seen']) is not int
                or not 1 <= row['first_seen'] <= generation
                or type(row['last_scanned']) is not int
                or not 0 <= row['last_scanned'] <= generation
                or (row['last_scanned'] != 0 and row['last_scanned'] < row['first_seen'])
                or (row['revision'] is not None and not _hash(row['revision']))
                or (row['revision'] is None) != (row['last_scanned'] == 0)):
            raise ValueError('invalid qresolve scheduling entry')
    return state


def plan(candidates, state, max_cards):
    """Return selected indices, a replacement state, and counts-only diagnostics.

``candidates`` are unique ``(identity_sha256, revision_sha256)`` pairs in value
order. Revisions describe the last inspected inputs, not merely the last seen
inputs: changed cards retain priority until they are actually inspected.
Inactive entries are discarded from this scheduling-only file. Application
intents/receipts are independent and are never consulted or altered here.
"""
    validate(state)
    if type(max_cards) is not int or not 1 <= max_cards <= 500:
        raise ValueError('max_cards must be between 1 and 500')
    if type(candidates) is not list or len(candidates) > MAX_CARDS:
        raise ValueError('qresolve scheduling capacity exceeded')
    if state['generation'] == MAX_GENERATION:
        raise ValueError('qresolve scheduling generation exhausted')
    generation = state['generation'] + 1
    entries, order, revisions = {}, {}, {}
    for index, candidate in enumerate(candidates):
        if (type(candidate) not in (list, tuple) or len(candidate) != 2
                or not all(_hash(value) for value in candidate)
                or candidate[0] in entries):
            raise ValueError('invalid or duplicate qresolve scheduling candidate')
        identity, revision = candidate
        entries[identity] = dict(state['entries'].get(identity, {
            'revision': None, 'first_seen': generation, 'last_scanned': 0}))
        order[identity], revisions[identity] = index, revision
    oldest = sorted(entries, key=lambda identity: (
        entries[identity]['last_scanned'] or entries[identity]['first_seen'] - 1,
        entries[identity]['first_seen'], order[identity]))
    changed = [identity for identity in entries
               if entries[identity]['revision'] != revisions[identity]]
    fair_count = min(len(oldest), (max_cards + 1) // 2)
    selected = oldest[:fair_count]
    selected_set = set(selected)
    priority_count = 0
    for identity in changed:
        if len(selected) >= max_cards:
            break
        if identity not in selected_set:
            selected.append(identity)
            selected_set.add(identity)
            priority_count += 1
    for identity in oldest:
        if len(selected) >= max_cards:
            break
        if identity not in selected_set:
            selected.append(identity)
            selected_set.add(identity)
    for identity in selected:
        entries[identity].update(revision=revisions[identity], last_scanned=generation)
    replacement = {'schema': SCHEMA, 'generation': generation, 'entries': entries}
    return [order[identity] for identity in selected], replacement, {
        'schema': SCHEMA, 'generation': generation, 'active_cards': len(candidates),
        'new_or_changed_cards': len(changed), 'fairness_reserved': fair_count,
        'change_priority_selected': priority_count,
        'state_entries': len(entries), 'state_capacity': MAX_CARDS,
        'state_advanced': False,
    }
