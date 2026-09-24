import hashlib
import pytest

from keel_muse.common import atomic_json, digest
from keel_muse.reconciliation import make_demo_inputs, propose_receipt, demo


def replace_receipt(inputs, **changes):
    receipt = {**inputs['receipt'], **changes}
    path = inputs['kwargs']['root'] / 'replacement.json'
    atomic_json(path, receipt)
    inputs['binding']['path'] = path.name
    inputs['binding']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    inputs['kwargs']['expected_binding_sha256'] = digest(inputs['binding'])


def test_receipt_rehearsal_uses_existing_unknown_attempt(tmp_path):
    result = demo(tmp_path / 'demo')
    assert result['status'] == 'PASS'
    assert len(result['checks']) == 6
    assert result['review']['source_authenticated'] is False


@pytest.mark.parametrize('change', [
    {'attempt_id': 'another-attempt'}, {'attempt_fence': 88},
    {'revision_sha256': 'f' * 64}, {'account_id': 'another-account'},
    {'application_id': 'another-application'}, {'observed_at': 103},
    {'observed_at': 108}, {'evidence_sha256': '0' * 64},
    {'observation': 'SAFE_TO_RETRY'}, {'execution_authorized': True}])
def test_mismatched_or_escalating_receipt_rejected_without_journal_write(tmp_path, change):
    inputs = make_demo_inputs(tmp_path / 'case')
    before = inputs['journal'].snapshot()
    replace_receipt(inputs, **change)
    with pytest.raises(ValueError):
        propose_receipt(inputs['journal'], inputs['binding'], **inputs['kwargs'])
    assert inputs['journal'].snapshot() == before


def test_negative_observation_never_allows_retry(tmp_path):
    inputs = make_demo_inputs(tmp_path / 'case')
    replace_receipt(inputs, observation='NOT_OBSERVED')
    result = propose_receipt(inputs['journal'], inputs['binding'], **inputs['kwargs'])
    assert result['proposal']['proposal'] == 'MANUAL_INVESTIGATION'
    assert result['retry_authorized'] is False
    assert inputs['journal'].snapshot()['state']['jobs']['job']['phase'] == 'UNKNOWN'


def test_changed_proof_bytes_block_even_if_normalization_is_unchanged(tmp_path):
    inputs = make_demo_inputs(tmp_path / 'case')
    (inputs['kwargs']['root'] / 'observation.txt').write_text('Changed evidence')
    with pytest.raises(ValueError):
        propose_receipt(inputs['journal'], inputs['binding'], **inputs['kwargs'])


def test_stale_recovery_checkpoint_blocks_receipt(tmp_path):
    inputs = make_demo_inputs(tmp_path / 'case')
    inputs['kwargs']['expected_checkpoint_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        propose_receipt(inputs['journal'], inputs['binding'], **inputs['kwargs'])
