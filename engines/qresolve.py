"""Local evidence-backed tray proposals; automation is explicitly opt-in.

Scores are deterministic admission rules, not calibrated probabilities. Only
the sanctioned tray actuator may clear blockers. Lower-trust logs are research
context, never a source of newly manufactured applicant answers.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import queue_io
from qresolve_corpus import Corpus
from qresolve_semantics import canonical_fingerprint, classify

POLICY_VERSION = 'qresolve.v1'
MAX_BYTES = 16 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _root(workspace=None):
    from keel_paths import HOME
    root = Path(workspace or HOME).absolute()
    if root != Path(HOME).absolute():
        raise ValueError('qresolve requires a fresh process with the selected KEEL_HOME')
    _safe_path(root)
    if not root.is_dir():
        raise ValueError('initialize the workspace first')
    return root


def _safe_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('qresolve refuses symlink paths')
    return path


def _read(path, default=None, *, lines=False):
    path = _safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('qresolve metadata must be a bounded regular file')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('qresolve metadata exceeds its read bound')
    if lines:
        if raw and not raw.endswith(b'\n'):
            raise ValueError('qresolve journal has an incomplete tail')
        return [queue_io.strict_loads(line) for line in raw.splitlines() if line.strip()]
    return queue_io.strict_loads(raw)


def _config(root):
    config = _read(root / 'hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': False})
    if (type(config) is not dict or type(config.get('AUTO_APPLY_FACTS', False)) is not bool
            or set(config) - {'schema', 'AUTO_APPLY_FACTS', 'authorization_note'}):
        raise ValueError('invalid qresolve configuration')
    if 'schema' in config and config['schema'] != 'keel.qresolve.config.v1':
        raise ValueError('invalid qresolve configuration schema')
    return config


def _contexts(root):
    result = {}
    for name in ('needs_input', 'standard'):
        rows = _read(root / 'data/queues' / (name + '-queue.json'), [])
        if type(rows) is not list:
            raise ValueError('invalid canonical queue')
        for row in rows:
            if type(row) is not dict or not isinstance(row.get('role_id'), str):
                raise ValueError('canonical queue requires role IDs')
            if row['role_id'] in result:
                raise ValueError('duplicate canonical role ID')
            result[row['role_id']] = row
    return result


def _for_card(card, contexts):
    ids = sorted({row['role_id'] for row in card.get('leads', [])})
    if not ids or any(key not in contexts for key in ids):
        raise ValueError('card does not bind to canonical leads')
    return [contexts[key] for key in ids]


def _pending(root):
    events = _read(root / 'hidden_files/qresolve-resolutions.jsonl', [], lines=True)
    pending = set()
    for event in events:
        if type(event) is not dict or not isinstance(event.get('decision_id'), str):
            raise ValueError('invalid qresolve journal event')
        action = event.get('action')
        if action == 'INTENT':
            pending.add(event['decision_id'])
        elif action == 'auto_applied':
            pending.discard(event['decision_id'])
    return pending


def _fresh(hit, today):
    try:
        age = (today - date.fromisoformat(hit['provenance_date'])).days
        return hit.get('own_words') is True and 0 <= age <= 90
    except (KeyError, TypeError, ValueError):
        return False


def plan_card(card, contexts, corpus, config, *, pending=False, today=None):
    """Plan from canonical inputs. No file writes, inferred answers or network."""
    label = classify(card, contexts)
    question = card.get('norm') or card.get('question') or ''
    decision = {
        'policy_version': POLICY_VERSION, 'card_key': card['key'],
        'fingerprint': canonical_fingerprint(question), 'class': label['class'],
        'confidence': 0.0, 'rationale': label['rationale'], 'route': label['route'],
        'action': 'park', 'answer': None, 'bank_key': None, 'evidence': [],
        'context_sha256': digest(sorted(contexts, key=lambda row: row['role_id'])),
        'config_sha256': digest(config),
        'target_role_ids': sorted({row['role_id'] for row in contexts}),
    }
    if label['class'] == 'STRUCTURAL':
        decision['action'] = 'route'
    else:
        hits = corpus.retrieve(card, contexts)
        # A source repeated in several logs is not an independent authority.
        exact = [hit for hit in hits if hit.get('match') == 'exact'
                 and hit.get('draft_eligible') is True and hit.get('answer')
                 and hit.get('source') and hit.get('pointer') and hit.get('provenance')]
        values = {hit['answer'] for hit in exact}
        if len(values) > 1:
            decision['rationale'] = 'conflicting scoped answers require review'
            decision['evidence'] = exact[:20]
        elif exact:
            hit = next((item for item in exact if item.get('eligible')), exact[0])
            allowed = (label['class'] in {'FACT', 'JUDGMENT'} or
                       (label['class'] == 'TRENT-ONLY' and hit.get('approved_verbatim') is True))
            if allowed:
                decision.update(answer=hit['answer'], bank_key=hit.get('bank_key'),
                                evidence=[hit], action='draft', confidence=0.80,
                                rationale='quoted scoped answer; human approval remains required')
                if (label['class'] == 'FACT' and hit.get('eligible') is True
                        and _fresh(hit, today or datetime.now(timezone.utc).date())):
                    decision['confidence'] = 0.98
                    if config.get('AUTO_APPLY_FACTS') is True and not pending:
                        decision.update(action='auto_apply',
                                        rationale='fresh scoped factual quote meets the explicit reuse policy')
            else:
                decision['rationale'] = 'human-only question lacks explicitly approved verbatim wording'
        if not decision['evidence']:
            decision['evidence'] = hits[:10]
        if pending:
            decision.update(action='park', answer=None, bank_key=None,
                            rationale='unfinished application intent requires reconciliation')
    if corpus.errors:
        decision.update(action='park', answer=None, bank_key=None, confidence=0.0,
                        rationale='evidence corpus is incomplete or invalid')
    decision['evidence_sha256'] = digest(decision['evidence'])
    decision['decision_id'] = digest(decision)
    return decision


def validate_application(request, card, targets, bank_path):
    """Recompute in the actuator's queue lock; uploaded decisions grant nothing."""
    root = _root()
    if Path(bank_path).absolute() != root / 'data/answer_bank.json':
        raise ValueError('qresolve requires the canonical bank')
    if (type(request) is not dict or set(request) != {'schema', 'decision'}
            or request['schema'] != 'keel.qresolve.request.v1'):
        raise ValueError('invalid qresolve application request')
    corpus = Corpus(root)
    contexts = list({row['role_id']: row for _, row, _ in targets}.values())
    decision = plan_card(card, contexts, corpus, _config(root), pending=bool(_pending(root)))
    if (decision != request['decision'] or decision['action'] != 'auto_apply'
            or not decision['evidence'] or not corpus.verify_snapshot()):
        raise ValueError('qresolve application evidence or authority changed')
    return decision


def _snapshot_cards(root):
    import input_tray_digest as tray
    if Path(tray.QDIR).absolute() != root / 'data/queues':
        raise ValueError('tray and resolver workspace paths disagree')
    return sorted(tray.collect_cards().values(),
                  key=lambda card: (-card['unblock_fit'], -(card.get('oldest_parked_h') or 0), card['key']))


def inspect(workspace=None, *, max_cards=50):
    if type(max_cards) is not int or not 1 <= max_cards <= 500:
        raise ValueError('max_cards must be between 1 and 500')
    root = _root(workspace)
    config = _config(root)
    contexts = _contexts(root)
    cards = _snapshot_cards(root)
    corpus = Corpus(root)
    pending = bool(_pending(root))
    decisions = [plan_card(card, _for_card(card, contexts), corpus, config, pending=pending)
                 for card in cards[:max_cards]]
    if not corpus.verify_snapshot() or contexts != _contexts(root) or config != _config(root):
        raise ValueError('canonical evidence changed during inspection')
    return {'schema': 'keel.qresolve.report.v1', 'mode': 'dry_run',
            'cards_seen': len(decisions), 'cards_remaining': max(0, len(cards) - len(decisions)),
            'decisions': decisions, 'corpus_diagnostics': corpus.errors,
            'auto_apply_enabled': config.get('AUTO_APPLY_FACTS', False),
            'pending_intent': pending, 'network_calls': 0, 'model_calls': 0,
            'submission_authorized': False, 'canonical_writes': 0,
            'actual_ready_transitions': None, 'actual_credit_savings': None}


def _write_metadata(path, value):
    """Caller holds queue_lock. Metadata only; never a queue or bank path."""
    path = _safe_path(path)
    if path.exists():
        old = _read(path)
        backup = path.parent / ('_qresolve-backup-' + digest(old) + '-' + path.name)
        _safe_path(backup)
        if not backup.exists():
            queue_io.atomic_write_json(str(backup), old)
    queue_io.atomic_write_json(str(path), value)


def _append_metrics(path, value):
    path = _safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('metrics must be a regular file')
        stream.write(json.dumps(value, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    queue_io._dir_fsync(str(path.parent))


def run(workspace=None, *, live=False, max_cards=50):
    root = _root(workspace)
    if not live:
        return inspect(root, max_cards=max_cards)
    report = inspect(root, max_cards=max_cards)
    with queue_io.queue_lock(owner='qresolve:proposals'):
        # Recompute after lock acquisition. A proposal is never an authority token.
        report = inspect(root, max_cards=max_cards)
        folder = _safe_path(root / 'hidden_files')
        folder.mkdir(mode=0o700, exist_ok=True)
        _write_metadata(folder / 'qresolve-proposals.json', {
            'schema': 'keel.qresolve.proposals.v1', 'decisions': report['decisions']})
    receipts = []
    for decision in report['decisions']:
        if decision['action'] != 'auto_apply':
            continue
        request_path = folder / ('qresolve-request-' + decision['decision_id'] + '.json')
        with queue_io.queue_lock(owner='qresolve:request'):
            _write_metadata(request_path, {'schema': 'keel.qresolve.request.v1', 'decision': decision})
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR', 'KEEL_TRAY_MIN_FIT'}}
        environment.update(KEEL_HOME=str(root), PYTHONDONTWRITEBYTECODE='1')
        try:
            child = subprocess.run([sys.executable, '-B', '-S',
                                    str(Path(__file__).with_name('tray_answer.py')),
                                    '--live', '--qresolve-request', str(request_path)],
                                   env=environment, capture_output=True, text=True, timeout=60, check=False)
            receipt = json.loads(child.stdout.strip().splitlines()[-1])
            if (type(receipt) is not dict or receipt.get('schema') != 'keel.qresolve.apply.v1'
                    or receipt.get('decision_id') != decision['decision_id']
                    or receipt.get('status') not in {'APPLIED', 'NO_CHANGE', 'HOLD'}):
                raise ValueError('invalid actuator receipt')
            if receipt['status'] == 'APPLIED' and (
                    any(type(receipt.get(key)) is not int or receipt[key] < 0
                        for key in ('changed_leads', 'removed_blockers', 'fully_unblocked'))
                    or not isinstance(receipt.get('role_ids'), list)
                    or receipt['changed_leads'] != len(set(receipt['role_ids']))
                    or not 0 < receipt['changed_leads'] <= receipt['removed_blockers']
                    or receipt['fully_unblocked'] > receipt['changed_leads']):
                raise ValueError('invalid actuator accounting')
            if child.returncode != 0 and receipt.get('status') == 'APPLIED':
                raise ValueError('actuator did not complete')
        except (OSError, ValueError, TypeError, IndexError, subprocess.TimeoutExpired):
            receipt = {'schema': 'keel.qresolve.apply.v1', 'status': 'HOLD',
                       'decision_id': decision['decision_id'], 'reason': 'actuator_outcome_unconfirmed'}
        receipts.append(receipt)
        if receipt['status'] == 'HOLD':
            break
    uncertain = any(item.get('status') == 'HOLD' for item in receipts)
    try:
        report['pending_intent'] = bool(_pending(root))
    except (OSError, ValueError, TypeError, KeyError):
        report['pending_intent'] = None
        uncertain = True
    report.update(mode='live', receipts=receipts, status='HOLD' if uncertain else 'COMPLETE',
                  outcome_uncertain=uncertain)
    report['observed_changed_leads'] = sum(item.get('changed_leads', 0) for item in receipts
                                           if item.get('status') == 'APPLIED')
    report['canonical_writes'] = None if uncertain else report['observed_changed_leads']
    metrics = {'ts': datetime.now(timezone.utc).isoformat(), 'cards_seen': report['cards_seen'],
               'auto_applied': sum(item.get('status') == 'APPLIED' for item in receipts),
               'drafted': sum(d['action'] == 'draft' for d in report['decisions']),
               'parked': sum(d['action'] in {'park', 'route'} for d in report['decisions']),
               'avg_confidence': (sum(d['confidence'] for d in report['decisions']) /
                                  len(report['decisions']) if report['decisions'] else 0),
               'held_or_unconfirmed': sum(item.get('status') == 'HOLD' for item in receipts),
               'outcome_uncertain': uncertain, 'pending_intent': report['pending_intent'],
               'unblock_fit_unlocked': None, 'actual_ready_transitions': None,
               'note': 'Draft/park counts describe planned decisions; applied removals are not READY transitions.'}
    with queue_io.queue_lock(owner='qresolve:metrics'):
        # Refresh the existing tray surface without advancing its delivery
        # watermark. This persists metadata only, never queue/bank state.
        import input_tray_digest as tray
        payload = tray.write_tray_json(tray.collect_cards(), set(),
                             tray.load(tray.FAM_HIST, {}), tray.system_blocked_counts(),
                             datetime.now(timezone.utc).isoformat())
        metrics['attached_drafts'] = sum(isinstance(card.get('draft'), dict)
                                         and card['draft'].get('owner') == 'qresolve'
                                         for card in payload['cards'])
        _append_metrics(folder / 'qresolve-metrics.jsonl', metrics)
    report['metrics'] = metrics
    return report


def decorate_cards(cards):
    """Refresh persisted proposals for the existing tray without hiding blockers."""
    root = _root()
    saved = _read(root / 'hidden_files/qresolve-proposals.json', {})
    if not saved:
        return cards
    if saved.get('schema') != 'keel.qresolve.proposals.v1':
        raise ValueError('invalid qresolve proposal store')
    wanted = {row['card_key']: row for row in saved['decisions']}
    prior = _read(root / 'hidden_files/input-tray.json', {})
    human_drafts = {row['key']: row for row in prior.get('cards', [])
                    if isinstance(row.get('draft'), dict) and row['draft'].get('owner') == 'human'}
    contexts, config, corpus = _contexts(root), _config(root), Corpus(root)
    pending = bool(_pending(root))
    result = []
    for original in cards:
        card = dict(original)
        human = human_drafts.get(card['key'])
        if (human and card.get('status') != 'SYSTEM-BLOCKED'
                and human.get('norm') == card.get('norm')
                and {row['role_id'] for row in human.get('leads', [])} ==
                    {row['role_id'] for row in card.get('leads', [])}):
            card['draft'] = human['draft']
        previous = wanted.get(card['key'])
        if previous is not None:
            current = plan_card(card, _for_card(card, contexts), corpus, config, pending=pending)
            if (current == previous and current['action'] == 'draft'
                    and card.get('status') != 'SYSTEM-BLOCKED'):
                # A normal bank suggestion can be strengthened with provenance.
                # A human-authored draft with another value is left untouched.
                old = card.get('draft')
                if not old or old.get('owner') != 'human':
                    card['draft'] = {'owner': 'qresolve', 'bank_key': current['bank_key'],
                                     'value': current['answer'], 'evidence': current['evidence'],
                                     'confidence': current['confidence'],
                                     'decision_id': current['decision_id']}
            elif card.get('draft', {}) and card['draft'].get('owner') != 'human':
                card['draft'] = None
            card['qresolve'] = {'class': current['class'], 'action': current['action'],
                               'rationale': current['rationale'], 'route': current['route']}
        result.append(card)
    if not corpus.verify_snapshot():
        raise ValueError('qresolve evidence changed during tray decoration')
    return result


def decorate_card(card):
    return decorate_cards([card])[0]


def console_report(report):
    """Counts only: schedulers may retain stdout in broadly visible logs."""
    counts = {action: sum(row['action'] == action for row in report['decisions'])
              for action in ('draft', 'auto_apply', 'park', 'route')}
    return {'schema': 'keel.qresolve.console.v1',
            'status': 'HOLD' if report.get('status') == 'HOLD' else 'OK',
            'mode': 'live' if report['mode'] == 'live' else 'dry_run',
            'cards_seen': int(report['cards_seen']),
            'cards_remaining': int(report['cards_remaining']),
            'auto_apply_enabled': report['auto_apply_enabled'] is True,
            'pending_intent': report['pending_intent'],
            'outcome_uncertain': report.get('outcome_uncertain', False) is True,
            'canonical_writes': report['canonical_writes'],
            'decision_counts': counts,
            'metrics': {key: report.get('metrics', {}).get(key) for key in
                        ('auto_applied', 'drafted', 'parked', 'attached_drafts',
                         'held_or_unconfirmed')},
            'submission_authorized': False,
            'private_review': 'hidden_files/input-tray.json and hidden_files/qresolve-proposals.json'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='save drafts; reuse FACTs only with explicit config authorization')
    parser.add_argument('--max-cards', type=int, default=50)
    args = parser.parse_args(argv)
    try:
        report = run(live=args.live, max_cards=args.max_cards)
        print(json.dumps(console_report(report), indent=2))
        return 1 if report.get('status') == 'HOLD' else 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print(json.dumps({'schema': 'keel.qresolve.report.v1', 'status': 'HOLD',
                          'reason': 'qresolve_input_or_state_invalid'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
