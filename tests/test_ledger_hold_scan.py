"""Per-census caching retains ordered fail-closed ledger semantics."""
import copy
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
from hypothesis import given, strategies as st
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import ready_gate
from safe_io import rows


def reference(entry, ledger):
    records = rows(ledger)
    rid = entry.get('role_id')
    company = ready_gate._identity_label(entry.get('company'))
    title = ready_gate._identity_label(entry.get('role_title') or entry.get('title'))
    posting = ready_gate._posting_key(entry)
    for record in records:
        status = record.get('status')
        if not isinstance(status, str) or not status.strip():
            raise ValueError('ledger outcome is missing or malformed')
        other_company = ready_gate._identity_label(record.get('company'))
        other_title = ready_gate._identity_label(record.get('role_title') or record.get('title'))
        if (bool(rid and record.get('role_id') == rid)
                or posting is not None and posting == ready_gate._posting_key(record)
                or bool(company and title and company == other_company and title == other_title)):
            return True
    return False


def outcome(call):
    try:
        return ('return', call())
    except (ValueError, TypeError) as exc:
        return (type(exc).__name__, str(exc))


URL = 'https://job-boards.greenhouse.io/synthetic/jobs/1'
values = st.one_of(st.none(), st.booleans(), st.integers(-1, 2),
                   st.sampled_from(['', ' ', 'one', 'ONE!', 'two', 'SUBMITTED', 'NEW_STATE', URL]),
                   st.just([]), st.just({}))
records = st.dictionaries(st.sampled_from(['role_id', 'company', 'title', 'role_title',
                                         'status', 'application_url']), values, max_size=6)


@given(st.lists(records, max_size=8), st.lists(records, min_size=1, max_size=8))
def test_matches_original_across_repeated_queries(ledger, entries):
    before = copy.deepcopy(ledger)
    scan = ready_gate.LedgerHoldScan(ledger)
    for entry in entries + list(reversed(entries)):
        expected = outcome(lambda: reference(entry, ledger))
        assert outcome(lambda: scan.holds(entry)) == expected
        assert outcome(lambda: ready_gate.ledger_holds(entry, ledger)) == expected
    assert ledger == before


@pytest.mark.parametrize('bad', [{'status': None}, {'status': 'DONE', 'company': []},
                                 {'status': 'DONE', 'title': {}}])
def test_early_match_and_malformed_row_order(bad):
    match = {'role_id': 'same', 'status': 'UNFAMILIAR_OUTCOME'}
    entry = {'role_id': 'same'}
    scan = ready_gate.LedgerHoldScan([match, bad])
    assert scan.holds(entry) is True
    with pytest.raises(ValueError):
        scan.holds({'role_id': 'different'})
    assert scan.holds(entry) is True
    with pytest.raises(ValueError):
        ready_gate.LedgerHoldScan([bad, match]).holds(entry)


def test_posting_parse_remains_lazy_after_role_match():
    scan = ready_gate.LedgerHoldScan([{'role_id': 'same', 'status': 'SUBMITTED', 'application_url': URL}])
    with patch.object(ready_gate, '_posting_key', wraps=ready_gate._posting_key) as parsed:
        assert scan.holds({'role_id': 'same', 'application_url': URL})
        assert parsed.call_count == 1  # Only the query entry, not the ledger row.
        assert scan.holds({'role_id': 'different', 'application_url': URL})
        assert parsed.call_count == 3


@pytest.mark.parametrize('ledger', [None, {}, {'rows': []}, [None], {'entries': [None]}])
def test_invalid_container_matches_original(ledger):
    assert outcome(lambda: ready_gate.LedgerHoldScan(ledger).holds({})) == outcome(lambda: reference({}, ledger))


def test_expensive_parsing_is_linear_in_rows_plus_queries():
    n = 40
    ledger = [{'role_id': f'old-{i}', 'status': 'SUBMITTED', 'company': f'Old {i}',
               'title': 'Engineer', 'application_url': URL + str(i)} for i in range(n)]
    entries = [{'role_id': f'new-{i}', 'company': f'New {i}', 'title': 'Engineer',
                'application_url': URL.replace('/synthetic/', '/other/') + str(i)} for i in range(n)]
    with patch.object(ready_gate, '_posting_key', wraps=ready_gate._posting_key) as parsed:
        scan = ready_gate.LedgerHoldScan(ledger)
        assert not any(scan.holds(e) for e in entries)
        assert parsed.call_count == 2 * n
    # A new snapshot must not reuse cache from previous reads.
    ledger[0]['role_id'] = entries[0]['role_id']
    assert ready_gate.LedgerHoldScan(ledger).holds(entries[0])


@pytest.mark.parametrize('entry,record', [
    ({'role_id': 'new', 'company': ' EXAMPLE, Inc. ', 'title': 'Software   Engineer'},
     {'role_id': 'old', 'company': 'example inc', 'title': 'software engineer'}),
    ({'role_id': 'new', 'application_url': URL},
     {'role_id': 'old', 'ats_url': 'https://boards.greenhouse.io/embed/job_app?for=synthetic&token=1'}),
])
def test_positive_twin_and_posting_alias(entry, record):
    record['status'] = 'UNRECOGNIZED_OUTCOME'
    scan = ready_gate.LedgerHoldScan([record])
    assert scan.holds(entry) is reference(entry, [record]) is True
    assert scan.holds(entry) is True
