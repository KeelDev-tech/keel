"""Local report setup is portable and status normalization grants no success."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from engines.ledger_append import canon_ledger_status

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('value', [None, True, 1, {}, [], 'accepted', 'success',
                                  'submitted successfully', 'x' * 65])
def test_ambiguous_status_is_unknown(value):
    assert canon_ledger_status(value) == 'UNKNOWN'


def test_claim_is_not_provider_confirmation():
    assert canon_ledger_status(' submitted ') == 'SUBMITTED'
    assert canon_ledger_status('submission_claimed') == 'SUBMISSION_CLAIMED'


@pytest.mark.parametrize('private', [False, True])
def test_paths_resolve_without_private_module_import(tmp_path, private):
    env = dict(os.environ)
    env['KEEL_HOME'] = str(tmp_path / 'local')
    env.pop('KEEL_JOB_PIPELINE_DIR', None)
    if private:
        env['KEEL_JOB_PIPELINE_DIR'] = str(tmp_path / 'explicit')
    env['PYTHONPATH'] = str(ROOT / 'engines') + os.pathsep + env.get('PYTHONPATH', '')
    command = ('import json, ledger_queue_reconciler_report as r; '
               'print(json.dumps([r.LIVE_LEDGER_PATH, r.LIVE_QUEUE_PATHS, '
               'r._build_parser().parse_args([]).queue_dir]))')
    result = subprocess.run([sys.executable, '-B', '-c', command], env=env,
                            capture_output=True, text=True, check=True)
    ledger, queues, directory = json.loads(result.stdout)
    base = tmp_path / ('explicit' if private else 'local/data')
    expected_ledger = base / ('ledger/application-ledger.json' if private else 'application-ledger.json')
    assert ledger == str(expected_ledger)
    expected_queue = base / ('queue' if private else 'queues')
    assert directory == str(expected_queue)
    assert queues['standard'] == str(expected_queue / 'standard-queue.json')
    assert not (tmp_path / 'local').exists()
