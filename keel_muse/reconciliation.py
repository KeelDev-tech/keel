"""Pinned receipt files to existing exact-attempt review proposals.

Reading and hashing an external observation establishes byte identity only.
The source and its claims remain unauthenticated. No retry, submission, hold
clearance or canonical outcome write is exposed by this module.
"""
from pathlib import Path
import hashlib

from keel_loki.recovery import RecoveryJournal
from .common import (FLAGS, MuseError, atomic_json, canonical, clone, decode_json,
                     digest, new_home, require_dict, require_hash, require_id)
from .sources import PrivateFileRoot


def propose_receipt(journal, binding, *, root, expected_target,
                    expected_binding_sha256, expected_checkpoint_sha256, now):
    if not isinstance(journal, RecoveryJournal):
        raise MuseError('existing_recovery_journal_required')
    binding = clone(binding)
    require_dict(binding, {'path', 'sha256', 'evidence_path', 'evidence_sha256'})
    if digest(binding) != require_hash(expected_binding_sha256):
        raise MuseError('receipt_binding_changed')
    require_dict(expected_target, {'origin', 'account_id', 'application_id'})
    for value in expected_target.values():
        if type(value) is not str or not value or len(value) > 2048:
            raise MuseError('host_target_invalid')
    before = journal.snapshot(expected_checkpoint_sha256=expected_checkpoint_sha256)
    with PrivateFileRoot(root) as files:
        raw = files.read(binding['path'], expected_sha256=require_hash(binding['sha256']))
        proof = files.read(binding['evidence_path'], expected_sha256=require_hash(binding['evidence_sha256']))
        if not proof:
            raise MuseError('empty_external_evidence')
        receipt = decode_json(raw)
        require_dict(receipt, {'schema', 'workspace_id', 'job_id', 'attempt_id',
            'revision_sha256', 'attempt_fence', 'observation', 'observed_at',
            'origin', 'account_id', 'application_id', 'source_kind', 'evidence_sha256'})
        if receipt['schema'] != 'keel.muse.external-receipt.v1':
            raise MuseError('receipt_schema_invalid')
        if any(receipt[key] != value for key, value in expected_target.items()):
            raise MuseError('receipt_target_mismatch')
        if receipt['source_kind'] not in {'employer_portal', 'confirmation_email', 'manual_observation'}:
            raise MuseError('receipt_source_kind_invalid')
        if receipt['evidence_sha256'] != binding['evidence_sha256']:
            raise MuseError('receipt_evidence_binding_mismatch')
        evidence = {key: receipt[key] for key in ('workspace_id', 'job_id', 'attempt_id',
            'revision_sha256', 'attempt_fence', 'observation', 'observed_at', 'evidence_sha256')}
        evidence['schema'] = 'keel.loki.reconciliation-evidence.v1'
        proposal = journal.reconciliation_proposal(receipt['job_id'], evidence,
            expected_checkpoint_sha256=expected_checkpoint_sha256,
            expected_evidence_sha256=digest(evidence), now=now)
        # Recheck actual evidence after the journal projection. Neither a
        # source rewrite nor a new journal event may reuse the earlier result.
        files.read(binding['path'], expected_sha256=binding['sha256'])
        files.read(binding['evidence_path'], expected_sha256=binding['evidence_sha256'])
        files.check_current()
        after = journal.snapshot(expected_checkpoint_sha256=expected_checkpoint_sha256)
    if before != after:
        raise MuseError('recovery_state_changed')
    return {'schema': 'keel.muse.receipt-review.v1', 'status': 'REVIEW_ONLY',
        'proposal': proposal, 'receipt_sha256': binding['sha256'],
        'external_evidence_sha256': binding['evidence_sha256'],
        'target_sha256': digest(expected_target), 'binding_sha256': digest(binding),
        'checkpoint_sha256': expected_checkpoint_sha256,
        'byte_identity_checked': True, 'source_authenticated': False,
        'observation_authenticated': False, 'truth_independently_verified': False,
        'journal_unchanged': True, 'unknown_cleared': False, 'retry_authorized': False,
        'canonical_writes': 0, 'journal_writes': 0, **FLAGS}


def make_demo_inputs(home):
    home = new_home(home)
    journal = RecoveryJournal(home / 'recovery', 'synthetic-receipts', now=100)
    journal.register('job', 'a' * 64, now=101)
    journal.approve('job', 'a' * 64, 'b' * 64, now=102)
    lease = journal.claim('job', 'worker', now=103)
    started = journal.start('job', 'worker', lease['fence'], now=104)
    journal.record_429(now=105)
    files = new_home(home / 'files')
    proof = b'Synthetic portal observation; never an actual employer receipt.\n'
    evidence_sha = hashlib.sha256(proof).hexdigest()
    with (files / 'observation.txt').open('xb') as stream:
        stream.write(proof)
    (files / 'observation.txt').chmod(0o600)
    target = {'origin': 'https://fixture.invalid', 'account_id': 'synthetic-account',
              'application_id': 'synthetic-application'}
    receipt = {'schema': 'keel.muse.external-receipt.v1', 'workspace_id': journal.workspace_id,
        'job_id': 'job', 'attempt_id': started['attempt_id'], 'revision_sha256': 'a' * 64,
        'attempt_fence': lease['fence'], 'observation': 'CONFIRMED', 'observed_at': 106,
        **target, 'source_kind': 'manual_observation', 'evidence_sha256': evidence_sha}
    atomic_json(files / 'receipt.json', receipt)
    binding = {'path': 'receipt.json', 'sha256': hashlib.sha256((files / 'receipt.json').read_bytes()).hexdigest(),
               'evidence_path': 'observation.txt', 'evidence_sha256': evidence_sha}
    return {'journal': RecoveryJournal.open_readonly(home / 'recovery', journal.workspace_id),
        'binding': binding, 'kwargs': {'root': files, 'expected_target': target,
        'expected_binding_sha256': digest(binding),
        'expected_checkpoint_sha256': journal.snapshot()['checkpoint_sha256'], 'now': 107},
        'receipt': receipt, 'home': home}


def demo(home):
    inputs = make_demo_inputs(home)
    before = inputs['journal'].snapshot()
    report = propose_receipt(inputs['journal'], inputs['binding'], **inputs['kwargs'])
    after = inputs['journal'].snapshot()
    checks = {'actual_receipt_bytes_pinned': report['byte_identity_checked'],
        'existing_exact_attempt_review': report['proposal']['status'] == 'REVIEW_ONLY',
        'journal_unchanged': before == after,
        'unknown_preserved': after['state']['jobs']['job']['phase'] == 'UNKNOWN',
        'global_429_preserved': after['state']['rate_limited'],
        'no_retry_or_execution_grant': report['retry_authorized'] is False
            and report['execution_authorized'] is False}
    result = {'schema': 'keel.muse.receipt-demo.v1', 'status': 'PASS' if all(checks.values()) else 'FAIL',
        'synthetic': True, 'checks': checks, 'review': report, 'canonical_writes': 0,
        'real_browser_actions': 0, 'real_model_calls': 0, **FLAGS}
    atomic_json(inputs['home'] / 'result.json', result)
    return result
