"""Read-only host diagnostics; simulated metadata never changes real ownership."""
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from keel_next.__main__ import main
from keel_next.workflow import doctor, storage_ancestor_report
from keel_observability.store import ObservationError, _ancestors


@pytest.mark.parametrize('fault,reason', [
    ('owner', 'untrusted_ancestor_owner'),
    ('writable', 'writable_nonsticky_ancestor_forbidden'),
    ('symlink', 'symlink_ancestor_forbidden'),
])
def test_diagnostic_matches_production_guard_without_writes(tmp_path, fault, reason):
    home = tmp_path / 'missing' / 'nested'
    original = Path.lstat

    def metadata(path):
        info = original(path)
        # Model a supported host except for one specified ancestor. No chmod,
        # chown, database constructor or guard replacement is involved.
        uid, mode = 0, info.st_mode
        if path == tmp_path:
            if fault == 'owner':
                uid = os.getuid() + 10000
            elif fault == 'writable':
                mode = stat.S_IFDIR | 0o777
            else:
                mode = stat.S_IFLNK | 0o777
        return SimpleNamespace(st_uid=uid, st_mode=mode)

    with patch.object(Path, 'lstat', metadata):
        with pytest.raises(ObservationError, match=reason):
            _ancestors(tmp_path / 'observations.sqlite3')
        report = storage_ancestor_report(home)
    assert report['status'] == 'BLOCKED'
    assert report['reason'] == reason
    assert not report['storage_opened'] and not report['execution_authorized']
    assert not home.parent.exists()
    if fault == 'owner':
        row = next(row for row in report['ancestors'] if row['path'] == str(tmp_path))
        assert row['uid'] == os.getuid() + 10000 and not row['owner_allowed']


def test_accepted_ancestors_do_not_claim_storage_or_execution_readiness(tmp_path):
    original = Path.lstat

    def metadata(path):
        info = original(path)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode)

    home = tmp_path / 'not-created'
    with patch.object(Path, 'lstat', metadata):
        report = storage_ancestor_report(home)
    assert report['status'] == 'ANCESTORS_ACCEPTED'
    assert not report['storage_opened'] and not report['execution_authorized']
    assert not home.exists()


def test_unreadable_ancestor_is_unavailable(tmp_path):
    with patch.object(Path, 'lstat', side_effect=PermissionError('denied')):
        report = storage_ancestor_report(tmp_path / 'new')
    assert report['status'] == 'UNAVAILABLE'
    assert report['reason'] == 'PermissionError'
    assert not report['storage_opened']


def test_doctor_inspects_no_state_and_reports_storage_boundary(tmp_path):
    home = tmp_path / 'new'
    with patch('keel_muse.coordinator.Coordinator', side_effect=AssertionError('no writable coordinator')):
        report = doctor(home)
    assert not home.exists()
    assert report['advanced_storage_ancestors'] == storage_ancestor_report(home)
    assert report['execution_authorized'] is False


def test_cli_keeps_failure_exit_and_prints_ownership_evidence(tmp_path, capsys):
    failure = {'status': 'BLOCKED', 'reason': 'untrusted_ancestor_owner',
               'ancestors': [{'path': '/synthetic', 'uid': 12345}],
               'storage_opened': False, 'execution_authorized': False}
    with patch('keel_next.__main__.demo', side_effect=ObservationError('untrusted_ancestor_owner')), \
         patch('keel_next.__main__.storage_ancestor_report', return_value=failure):
        assert main(['demo', '--home', str(tmp_path / 'new')]) == 2
    err = capsys.readouterr().err
    assert json.loads(err.splitlines()[-1])['advanced_storage_ancestors'] == failure
    assert not (tmp_path / 'new').exists()
