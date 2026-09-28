"""Exact, one-use approval of a persisted QRESOLVE quotation.

The operator's explicit CLI invocation approves the existing draft for its exact
current targets. It is not an identity assertion, a new applicant statement, or
permission to broaden bank scope. No queues or bank files are written here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path

import queue_io
from qresolve_corpus import Corpus, _read


def load_proposal(decision_id, bank_path):
    """Load only the canonical persisted proposal, never caller-provided text."""
    from qresolve import _root
    root = _root()
    if Path(bank_path).absolute() != root / 'data/answer_bank.json':
        raise ValueError('approval requires the canonical bank')
    if (not isinstance(decision_id, str) or len(decision_id) != 64
            or any(char not in '0123456789abcdef' for char in decision_id)):
        raise ValueError('invalid_decision_id')
    proposals = queue_io.strict_loads(_read(
        root / 'hidden_files/qresolve-proposals.json', 16 * 1024 * 1024)[0])
    if (not isinstance(proposals, dict)
            or set(proposals) != {'schema', 'decisions'}
            or proposals.get('schema') != 'keel.qresolve.proposals.v1'
            or not isinstance(proposals.get('decisions'), list)
            or any(not isinstance(row, dict) for row in proposals['decisions'])):
        raise ValueError('invalid_proposals')
    matches = [row for row in proposals['decisions']
               if row.get('decision_id') == decision_id]
    if len(matches) != 1:
        raise ValueError('missing_or_duplicate_proposal')
    return {'schema': 'keel.qresolve.approval.request.v1', 'decision': matches[0]}


def validate_approval(request, card, targets, bank_path):
    """Recompute under queue→bank locks; approval cannot repair stale evidence."""
    from qresolve import _root, _config, _pending, plan_card
    root = _root()
    if (not isinstance(request, dict)
            or set(request) != {'schema', 'decision'}
            or request.get('schema') != 'keel.qresolve.approval.request.v1'
            or not isinstance(request.get('decision'), dict)):
        raise ValueError('invalid_approval_request')
    decision_id = request['decision'].get('decision_id')
    if load_proposal(decision_id, bank_path) != request:
        raise ValueError('persisted_proposal_changed')
    corpus = Corpus(root)
    config = _config(root)
    contexts = list({row['role_id']: row for _, row, _ in targets}.values())
    decision = plan_card(card, contexts, corpus, config, pending=bool(_pending(root)))
    # Protected certifications, essays, consent and structural blockers retain
    # their existing applicant-owned routes even if a bank quote was drafted.
    if (decision != request['decision'] or decision.get('action') != 'draft'
            or decision.get('class') not in {'FACT', 'JUDGMENT'}
            or not isinstance(decision.get('answer'), str) or not decision['answer'].strip()
            or not decision.get('bank_key') or len(decision.get('evidence', [])) != 1):
        raise ValueError('draft_not_approvable_or_changed')
    hit = decision['evidence'][0]
    if (hit.get('draft_eligible') is not True or hit.get('match') != 'exact'
            or hit.get('tier') != 'bank' or hit.get('answer') != decision['answer']
            or not hit.get('provenance') or not hit.get('scope_status')
            or any(status not in {'resolved', 'legacy'} for status in hit['scope_status'])
            or not corpus.verify_snapshot() or config != _config(root)
            or load_proposal(decision_id, bank_path) != request):
        raise ValueError('draft_evidence_or_scope_changed')
    return decision


def approval_receipt(decision):
    """Private durable receipt, separate from the untouched source provenance."""
    from qresolve import digest
    hit = decision['evidence'][0]
    receipt = {
        'schema': 'keel.qresolve.approval.v1', 'mode': 'exact_draft',
        'authorization': 'explicit_operator_command',
        'ts': datetime.now(timezone.utc).isoformat(),
        'decision_id': decision['decision_id'],
        'fingerprint': decision['fingerprint'],
        'answer_sha256': hashlib.sha256(decision['answer'].encode('utf-8')).hexdigest(),
        'context_sha256': decision['context_sha256'],
        'evidence_sha256': decision['evidence_sha256'],
        'config_sha256': decision['config_sha256'],
        'target_role_ids': decision['target_role_ids'],
        'scope': 'exact_current_targets_only',
        'source_provenance': hit['provenance'],
        'source_scope': hit['scope'],
        'source_legacy': hit['legacy'],
        'bank_modified': False, 'submission_authorized': False,
    }
    receipt['approval_id'] = digest(receipt)
    return receipt
