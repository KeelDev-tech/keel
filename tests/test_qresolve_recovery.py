"""Offline process-death recovery tests against the real answer actuator."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / 'engines'
CHILD = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
def offline(event, args):
    if event.startswith('socket.'):
        raise RuntimeError('network forbidden')
sys.addaudithook(offline)
from pathlib import Path
import qresolve, qresolve_recovery, tray_answer
root = Path(os.environ['KEEL_HOME'])
mode = sys.argv[2]
if mode == 'recover':
    report = qresolve_recovery.recover(decision_id=sys.argv[3] or None,
                                      live=sys.argv[4] == 'live')
    print(json.dumps(report))
elif mode == 'apply':
    request = root / 'request.json'
    if not request.exists():
        decision = qresolve.inspect()['decisions'][0]
        request.write_text(json.dumps({'schema': 'keel.qresolve.request.v1', 'decision': decision}))
    crash_at = sys.argv[3]
    original_write, original_append = tray_answer.queue_io.atomic_write_json, tray_answer._qresolve_append
    writes = 0
    def fault_write(path, value):
        global writes
        canonical = str(path) in {tray_answer.NI_Q, tray_answer.STD_Q}
        if canonical and crash_at == 'before_queue':
            os._exit(73)
        result = original_write(path, value)
        if canonical:
            writes += 1
            if crash_at == 'first_queue' and writes == 1:
                os._exit(73)
            if crash_at == 'both_queues' and writes == 2:
                os._exit(73)
        if str(path).endswith('qresolve-resolved.json') and crash_at == 'resolved':
            os._exit(73)
        return result
    def fault_append(path, value):
        result = original_append(path, value)
        if value['action'] == 'INTENT' and crash_at == 'intent':
            os._exit(73)
        if value['action'] == 'auto_applied' and crash_at == 'complete':
            os._exit(73)
        return result
    tray_answer.queue_io.atomic_write_json = fault_write
    tray_answer._qresolve_append = fault_append
    tray_answer.apply_qresolve(str(request), str(root / 'data/answer_bank.json'), live=True)
elif mode == 'crash_recovery':
    original = tray_answer._qresolve_append
    def fault_append(path, value):
        if sys.argv[4] == 'partial':
            with open(path, 'ab') as stream:
                stream.write(b'{"action":"recovered_')
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(73)
        result = original(path, value)
        os._exit(73)
    tray_answer._qresolve_append = fault_append
    qresolve_recovery.recover(decision_id=sys.argv[3], live=True)
elif mode == 'lock_check':
    original = tray_answer._qresolve_append
    def checked(path, value):
        assert getattr(tray_answer.queue_io._state, 'depth', 0) > 0
        assert str(root / 'data/answer_bank.json') + '.lock' in tray_answer.safe_io._state.held
        return original(path, value)
    tray_answer._qresolve_append = checked
    print(json.dumps(qresolve_recovery.recover(decision_id=sys.argv[3], live=True)))
'''


class QresolveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.question = 'What is your email address?'
        self.write('data/answer_bank.json', {'answers': {'email': {
            'value': 'private-fixture@example.com', 'scope': 'global', 'question': self.question,
            'provenance': "the applicant's own words " + date.today().isoformat()}}})
        for queue, rid in [('needs_input', 'fixture-ni'), ('standard', 'fixture-std')]:
            self.write(f'data/queues/{queue}-queue.json', [{
                'role_id': rid, 'company': 'Example', 'title': 'Example', 'fit_score': 70,
                'status': 'NEEDS-INPUT', 'unresolved': [self.question],
                'status_reason': 'input required', 'queue_notes': [],
                'last_verify_attempt': '2026-01-01'}])
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': True})
        self.write('data/application-ledger.json', [])

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def read(self, relative):
        return json.loads((self.home / relative).read_text())

    def child(self, *args):
        env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
        env.update(KEEL_HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-B', '-S', '-c', CHILD, str(ENGINES), *args],
                              env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)

    def crash(self, boundary):
        child = self.child('apply', boundary)
        self.assertEqual(child.returncode, 73, child.stdout + child.stderr)
        return self.read('request.json')['decision']['decision_id']

    def recover(self, decision_id='', live=False):
        child = self.child('recover', decision_id, 'live' if live else 'dry')
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertNotIn('private-fixture@example.com', child.stdout)
        self.assertNotIn(str(self.home), child.stdout)
        self.assertNotIn(self.question, child.stdout)
        return json.loads(child.stdout)

    def journal(self):
        path = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def snapshot(self):
        return {str(path.relative_to(self.home)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                 path.stat().st_mtime_ns)
                for path in self.home.rglob('*') if path.is_file()}

    def canonical(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in (self.home / 'data').glob('**/*.json')}

    def test_default_empty_and_pending_inspection_make_no_files_or_changes(self):
        before = self.snapshot()
        self.assertEqual(self.recover()['reason'], 'no_pending_intent')
        self.assertEqual(self.snapshot(), before)
        did = self.crash('intent')
        before = self.snapshot()
        report = self.recover(did)
        self.assertEqual(report['status'], 'RECOVERABLE')
        self.assertEqual(report['decisions'][0]['proposed_action'], 'cancelled_unwritten')
        self.assertEqual(report['audit_writes'], 0)
        self.assertEqual(self.snapshot(), before)

    def test_death_after_intent_closes_only_audit_and_retry_is_revalidated(self):
        did = self.crash('intent')
        before = self.canonical()
        report = self.recover(did, live=True)
        self.assertEqual((report['status'], report['audit_writes'], report['canonical_writes']),
                         ('RECOVERED', 1, 0))
        self.assertEqual(self.canonical(), before)
        self.assertEqual(self.journal()[-1]['action'], 'cancelled_unwritten')
        before_journal = self.journal()
        self.assertEqual(self.recover(did, live=True)['reason'], 'no_pending_intent')
        self.assertEqual(self.journal(), before_journal)
        # Integration requires the shared sequential parser in the actuator.
        result = self.child('apply', 'complete')
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.assertEqual([row['action'] for row in self.journal()],
                         ['INTENT', 'cancelled_unwritten', 'INTENT', 'auto_applied'])

    def test_death_between_queue_commits_stays_held_without_changes(self):
        did = self.crash('first_queue')
        before = self.canonical(), self.journal()
        report = self.recover(did, live=True)
        self.assertEqual(report['status'], 'HOLD')
        self.assertEqual(report['decisions'][0]['reason'], 'partial_or_changed_queues')
        self.assertEqual((self.canonical(), self.journal()), before)

    def test_complete_queues_without_resolved_receipt_stay_held(self):
        did = self.crash('both_queues')
        report = self.recover(did, live=True)
        self.assertEqual(report['status'], 'HOLD')
        self.assertEqual(report['decisions'][0]['reason'], 'completed_receipt_missing')
        self.assertEqual([row['action'] for row in self.journal()], ['INTENT'])

    def test_complete_receipt_recovers_audit_and_completed_replay_is_noop(self):
        did = self.crash('resolved')
        before = self.canonical()
        resolved_before = (self.home / 'hidden_files/qresolve-resolved.json').read_bytes()
        report = self.recover(did, live=True)
        self.assertEqual(report['status'], 'RECOVERED')
        self.assertEqual(self.journal()[-1]['action'], 'recovered_applied')
        self.assertEqual(self.journal()[-1]['original_action'], 'auto_applied')
        self.assertEqual(self.journal()[-1]['changed_leads'], 2)
        self.assertFalse(self.journal()[-1]['ready_verified'])
        self.assertEqual(self.canonical(), before)
        self.assertEqual((self.home / 'hidden_files/qresolve-resolved.json').read_bytes(), resolved_before)
        self.assertEqual(self.recover(did, live=True)['reason'], 'already_completed')
        result = self.child('apply', '')
        self.assertEqual(json.loads(result.stdout)['reason'], 'already_recorded')
        self.assertEqual(self.canonical(), before)

    def test_death_after_completion_needs_no_recovery(self):
        did = self.crash('complete')
        before = self.snapshot()
        self.assertEqual(self.recover(did)['reason'], 'already_completed')
        self.assertEqual(self.snapshot(), before)

    def test_recovery_death_after_durable_closure_is_idempotent(self):
        did = self.crash('resolved')
        result = self.child('crash_recovery', did, 'complete')
        self.assertEqual(result.returncode, 73, result.stderr)
        before = self.journal()
        self.assertEqual(self.recover(did, live=True)['reason'], 'already_completed')
        self.assertEqual(self.journal(), before)

    def test_recovery_partial_journal_append_remains_held_and_is_never_truncated(self):
        did = self.crash('resolved')
        result = self.child('crash_recovery', did, 'partial')
        self.assertEqual(result.returncode, 73, result.stderr)
        path = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        before = path.read_bytes(), self.canonical()
        self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
        self.assertEqual((path.read_bytes(), self.canonical()), before)

    def test_missing_or_modified_backup_prevents_closure(self):
        did = self.crash('intent')
        backup = self.home / 'data/queues' / self.journal()[0]['backup'] / 'needs_input-queue.json'
        backup.write_text('[]')
        self.assertEqual(self.recover(did, live=True)['decisions'][0]['reason'], 'backup_mismatch')
        backup.unlink()
        self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
        self.assertEqual([row['action'] for row in self.journal()], ['INTENT'])

    def test_changed_bank_or_unrelated_queue_revision_stays_held(self):
        did = self.crash('intent')
        bank = self.read('data/answer_bank.json')
        self.write('data/answer_bank.json', {'answers': {}})
        self.assertEqual(self.recover(did)['decisions'][0]['reason'], 'bank_changed')
        self.write('data/answer_bank.json', bank)
        rows = self.read('data/queues/needs_input-queue.json')
        rows[0]['fit_score'] += 1
        self.write('data/queues/needs_input-queue.json', rows)
        self.assertEqual(self.recover(did)['decisions'][0]['reason'], 'partial_or_changed_queues')

    def test_changed_receipt_evidence_or_target_hash_cannot_complete(self):
        did = self.crash('resolved')
        original = self.read('hidden_files/qresolve-resolved.json')
        for key, value in [('evidence', []), ('answer', 'altered'), ('target_post_sha256', {})]:
            with self.subTest(key=key):
                resolved = json.loads(json.dumps(original))
                resolved[did][key] = value
                self.write('hidden_files/qresolve-resolved.json', resolved)
                self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
        self.assertEqual([row['action'] for row in self.journal()], ['INTENT'])

    def test_human_approval_is_preserved_and_mismatch_is_held(self):
        did = self.crash('resolved')
        records, resolved = self.journal(), self.read('hidden_files/qresolve-resolved.json')
        approval = {'schema': 'keel.qresolve.approval.v1', 'decision_id': did, 'mode': 'exact_draft'}
        records[0]['approval'] = approval
        journal = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        journal.write_text(''.join(json.dumps(row) + '\n' for row in records))
        self.assertEqual(self.recover(did)['status'], 'HOLD')
        resolved[did]['approval'] = approval
        self.write('hidden_files/qresolve-resolved.json', resolved)
        self.assertEqual(self.recover(did, live=True)['status'], 'RECOVERED')
        self.assertEqual(self.journal()[-1]['approval'], approval)
        self.assertEqual(self.journal()[-1]['original_action'], 'human_applied')

    def test_live_requires_an_exact_id_and_closure_holds_both_locks(self):
        did = self.crash('intent')
        before = self.snapshot()
        self.assertEqual(self.recover(live=True)['reason'], 'decision_id_required')
        self.assertEqual(self.recover('../private', live=True)['reason'], 'invalid_decision_id')
        self.assertEqual(self.snapshot(), before)
        result = self.child('lock_check', did)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'RECOVERED')

    def test_unsafe_sources_and_backup_traversal_hold(self):
        did = self.crash('intent')
        bank = self.home / 'data/answer_bank.json'
        original = bank.read_bytes()
        bank.unlink()
        os.mkfifo(bank)
        self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
        bank.unlink()
        bank.write_bytes(original)
        records = self.journal()
        records[0]['backup'] = '../outside'
        journal = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        journal.write_text(''.join(json.dumps(row) + '\n' for row in records))
        self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
        self.assertEqual(self.journal(), records)

    def test_lock_diagnostic_symlinks_are_refused_before_any_overwrite(self):
        did = self.crash('intent')
        sentinel = self.home / 'external-sentinel'
        sentinel.write_text('must remain untouched')
        for relative in ('hidden_files/queue.lock', 'hidden_files/queue.lock.meta',
                         'hidden_files/queue_lock_waits.jsonl', 'data/answer_bank.json.lock'):
            with self.subTest(relative=relative):
                path = self.home / relative
                old = path.read_bytes() if path.exists() else None
                if path.exists():
                    path.unlink()
                path.symlink_to(sentinel)
                before = self.journal(), self.canonical()
                try:
                    self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
                    self.assertEqual(sentinel.read_text(), 'must remain untouched')
                    self.assertEqual((self.journal(), self.canonical()), before)
                finally:
                    path.unlink()
                    if old is not None:
                        path.write_bytes(old)

    def test_forged_recovery_closure_and_duplicate_intent_are_held(self):
        did = self.crash('intent')
        records = self.journal()
        journal = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        for extra in ({'action': 'cancelled_unwritten', 'decision_id': did,
                       'intent_sha256': 'a' * 64}, records[0]):
            with self.subTest(action=extra['action']):
                journal.write_text(''.join(json.dumps(row) + '\n' for row in records + [extra]))
                before = journal.read_bytes()
                self.assertEqual(self.recover(did, live=True)['status'], 'HOLD')
                self.assertEqual(journal.read_bytes(), before)

    def test_cancel_retry_then_second_crash_is_still_pending(self):
        did = self.crash('intent')
        self.assertEqual(self.recover(did, live=True)['status'], 'RECOVERED')
        self.crash('intent')
        report = self.recover(did)
        self.assertEqual(report['status'], 'RECOVERABLE')
        self.assertEqual([row['action'] for row in self.journal()],
                         ['INTENT', 'cancelled_unwritten', 'INTENT'])


if __name__ == '__main__':
    unittest.main()
