"""Real offline exact-draft approval, provenance and stale-evidence regressions."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = '''
import runpy, sys
def deny(event, args):
    if event.startswith('socket.'):
        raise RuntimeError('network forbidden in approval')
sys.addaudithook(deny)
sys.path.insert(0, sys.argv[1])
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


class QresolveApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='keel-qresolve-approval-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.bank_name = 'data/answer_bank.json'
        self.queue_name = 'data/queues/needs_input-queue.json'
        self.proposal_name = 'hidden_files/qresolve-proposals.json'
        self.journal_name = 'hidden_files/qresolve-resolutions.jsonl'
        self.rows = []
        self.bank = {}
        self.environment = {key: value for key, value in os.environ.items()
                            if key in {'PATH', 'LANG', 'LC_ALL'}}
        self.environment.update(KEEL_HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        self.fixture()

    def write(self, name, value):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def read(self, name):
        return json.loads((self.home / name).read_text())

    def fixture(self, question='What is your email address?', key='email', scope='global'):
        self.question, self.bank_key = question, key
        self.rows = [{'role_id': 'example-role', 'company': 'Example Employer',
                      'title': 'Example Role', 'fit_score': 80, 'status': 'NEEDS-INPUT',
                      'unresolved': [question], 'queue_notes': [],
                      'status_reason': 'input required', 'last_verify_attempt': '2026-01-01'}]
        self.provenance = "the applicant's own words " + datetime.now(timezone.utc).date().isoformat()
        self.bank = {'answers': {key: {'value': 'existing private quotation',
                    'question': question, 'provenance': self.provenance}},
                    '_provenance': {key: {'source': 'original source must remain untouched'}}}
        if scope is not None:
            self.bank['answers'][key]['scope'] = scope
        self.write(self.bank_name, self.bank)
        self.write(self.queue_name, self.rows)
        self.write('data/queues/standard-queue.json', [])
        self.write('data/application-ledger.json', [])

    def command(self, engine, *args):
        return subprocess.run([sys.executable, '-B', '-S', '-c', RUNNER,
            str(ROOT / 'engines'), str(ROOT / 'engines' / engine), *args],
            env=self.environment, cwd=ROOT, capture_output=True, text=True, timeout=30)

    def propose(self):
        result = self.command('qresolve.py', '--live')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        decisions = self.read(self.proposal_name)['decisions']
        self.assertEqual(len(decisions), 1)
        return decisions[0]

    def approve(self, decision, live=True, extra=()):
        decision_id = decision if isinstance(decision, str) else decision['decision_id']
        result = self.command('tray_answer.py', '--approve-qresolve', decision_id,
                              *(('--live',) if live else ()), *extra)
        self.assertNotIn('existing private quotation', result.stdout + result.stderr)
        self.assertNotIn(self.provenance, result.stdout + result.stderr)
        return result, json.loads(result.stdout)

    def events(self):
        path = self.home / self.journal_name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.home.rglob('*') if path.is_file()}

    def test_approval_keeps_bank_and_source_and_records_exact_targets(self):
        decision = self.propose()
        bank_before = (self.home / self.bank_name).read_bytes()
        result, receipt = self.approve(decision)
        self.assertEqual((result.returncode, receipt['status']), (0, 'APPLIED'))
        self.assertFalse(receipt['ready_verified'])
        self.assertEqual(receipt['changed_leads'], 1)
        self.assertEqual(bank_before, (self.home / self.bank_name).read_bytes())
        row = self.read(self.queue_name)[0]
        self.assertEqual(row['status'], 'NEEDS-INPUT')
        self.assertEqual(row['unresolved'], [])
        self.assertIsNone(row['last_verify_attempt'])
        self.assertIn('explicit approval of existing bank quotation', row['queue_notes'][0])
        self.assertNotIn("applicant's own words", row['queue_notes'][0])
        events = self.events()
        self.assertEqual([event['action'] for event in events], ['INTENT', 'human_applied'])
        approval = events[-1]['approval']
        self.assertEqual(events[0]['approval'], approval)
        self.assertEqual(approval['source_provenance'], self.provenance)
        self.assertEqual(approval['scope'], 'exact_current_targets_only')
        self.assertEqual(approval['target_role_ids'], ['example-role'])
        self.assertFalse(approval['bank_modified'])
        self.assertFalse(approval['submission_authorized'])
        self.assertEqual(approval['answer_sha256'], hashlib.sha256(
            b'existing private quotation').hexdigest())
        for key in ('decision_id', 'fingerprint', 'context_sha256',
                    'evidence_sha256', 'config_sha256'):
            self.assertEqual(approval[key], decision[key])
        resolved = self.read('hidden_files/qresolve-resolved.json')[decision['decision_id']]
        self.assertEqual(resolved['approval'], approval)

    def test_judgment_approval_is_current_target_only(self):
        self.fixture('When can you start?', 'availability', 'employer:Example Employer')
        decision = self.propose()
        self.assertEqual((decision['class'], decision['action']), ('JUDGMENT', 'draft'))
        self.assertEqual(self.approve(decision)[1]['status'], 'APPLIED')
        self.assertEqual(self.events()[-1]['class'], 'JUDGMENT')
        # Reusing the old ID after another target appears cannot approve it.
        self.rows[0]['role_id'] = 'new-role'
        self.write(self.queue_name, self.rows)
        self.assertEqual(self.approve(decision)[1]['reason'], 'already_recorded')
        self.assertEqual(self.read(self.queue_name)[0]['unresolved'], [self.question])

    def test_legacy_safe_draft_gets_one_time_approval_without_scope_promotion(self):
        self.fixture(scope=None)
        decision = self.propose()
        self.assertEqual(decision['action'], 'draft')
        before = (self.home / self.bank_name).read_bytes()
        self.assertEqual(self.approve(decision)[1]['status'], 'APPLIED')
        self.assertTrue(self.events()[-1]['approval']['source_legacy'])
        self.assertEqual(before, (self.home / self.bank_name).read_bytes())

    def test_dry_run_changes_nothing_including_lock_files(self):
        decision = self.propose()
        before = self.snapshot()
        result, receipt = self.approve(decision, live=False)
        self.assertEqual((result.returncode, receipt['status'], receipt['reason']),
                         (0, 'NO_CHANGE', 'dry_run'))
        self.assertEqual(before, self.snapshot())

    def test_replay_is_noop_even_after_proposal_file_is_replaced(self):
        decision = self.propose()
        self.assertEqual(self.approve(decision)[1]['status'], 'APPLIED')
        self.write(self.proposal_name, {'schema': 'keel.qresolve.proposals.v1', 'decisions': []})
        queues, events = self.read(self.queue_name), self.events()
        for live in (True, False):
            result, receipt = self.approve(decision, live=live)
            self.assertEqual((result.returncode, receipt['status'], receipt['reason']),
                             (0, 'NO_CHANGE', 'already_recorded'))
        self.assertEqual(queues, self.read(self.queue_name))
        self.assertEqual(events, self.events())

    def test_changed_bank_answer_provenance_scope_or_expiry_holds(self):
        for field, value in (('value', 'changed answer'), ('provenance', 'another source'),
                             ('scope', 'employer:Another Employer'), ('expires', '2000-01-01')):
            with self.subTest(field=field):
                self.fixture()
                decision = self.propose()
                self.bank['answers'][self.bank_key][field] = value
                self.write(self.bank_name, self.bank)
                before = (self.home / self.queue_name).read_bytes()
                self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
                self.assertEqual(before, (self.home / self.queue_name).read_bytes())
        self.assertFalse(any(e['action'] == 'INTENT' for e in self.events()))

    def test_changed_target_context_question_or_card_membership_holds(self):
        for change in ('company', 'question', 'new_target', 'notes'):
            with self.subTest(change=change):
                self.fixture()
                decision = self.propose()
                if change == 'company':
                    self.rows[0]['company'] = 'Different Employer'
                elif change == 'question':
                    self.rows[0]['unresolved'] = ['What is your first name?']
                elif change == 'notes':
                    self.rows[0]['queue_notes'] = ['New context']
                else:
                    self.rows.append({**self.rows[0], 'role_id': 'another-target'})
                self.write(self.queue_name, self.rows)
                before = (self.home / self.queue_name).read_bytes()
                self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
                self.assertEqual(before, (self.home / self.queue_name).read_bytes())

    def test_changed_config_and_tampered_persisted_proposal_hold(self):
        decision = self.propose()
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': True})
        self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': False})
        proposals = self.read(self.proposal_name)
        proposals['decisions'][0]['answer'] = 'forged quote'
        self.write(self.proposal_name, proposals)
        self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
        self.assertEqual(self.read(self.queue_name), self.rows)

    def test_protected_certifications_essays_and_structural_routes_refused(self):
        for question in ('I certify that I personally completed this OpenAI application',
                         'Do you consent to interview recording?', 'Why do you want this role?',
                         'Application route is apply-by-email'):
            with self.subTest(question=question):
                self.fixture(question, 'protected')
                self.bank['answers']['protected']['approved_verbatim'] = True
                self.write(self.bank_name, self.bank)
                decision = self.propose()
                self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
                self.assertEqual(self.read(self.queue_name), self.rows)

    def test_explicit_scope_mismatch_and_quarantine_never_approve(self):
        for scope in ('employer:Another Employer', 'ambiguous'):
            self.fixture(scope=scope)
            decision = self.propose()
            self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
            self.assertEqual(self.read(self.queue_name), self.rows)
        self.fixture()
        self.bank['_quarantined'] = {self.bank_key: {'reason': 'held'}}
        self.write(self.bank_name, self.bank)
        self.assertEqual(self.approve(self.propose())[1]['status'], 'HOLD')

    def test_pending_intent_holds_and_cancelled_unwritten_permits_fresh_validation(self):
        decision = self.propose()
        journal = self.home / self.journal_name
        intent = {'decision_id': decision['decision_id'], 'action': 'INTENT'}
        intent_hash = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        journal.write_text(json.dumps(intent) + '\n')
        self.assertEqual(self.approve(decision)[1]['reason'], 'incomplete_prior_intent')
        with journal.open('a') as stream:
            stream.write(json.dumps({'decision_id': decision['decision_id'],
                                     'action': 'cancelled_unwritten', 'intent_sha256': intent_hash}) + '\n')
        self.assertEqual(self.approve(decision)[1]['status'], 'APPLIED')

    def test_recovered_completion_prevents_duplicate_approval(self):
        decision = self.propose()
        journal = self.home / self.journal_name
        intent = {'decision_id': decision['decision_id'], 'action': 'INTENT'}
        intent_hash = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        journal.write_text(json.dumps(intent) + '\n' + json.dumps({
            'decision_id': decision['decision_id'], 'action': 'recovered_applied',
            'intent_sha256': intent_hash}) + '\n')
        self.assertEqual(self.approve(decision)[1]['reason'], 'already_recorded')
        self.assertEqual(self.read(self.queue_name), self.rows)

    def test_invalid_ids_missing_or_duplicate_proposals_and_conflicting_arguments(self):
        decision = self.propose()
        for value in ('bad-id', 'a' * 64):
            self.assertEqual(self.approve(value)[1]['status'], 'HOLD')
        self.write(self.proposal_name, {'schema': 'keel.qresolve.proposals.v1',
                                       'decisions': [decision, decision]})
        self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
        for extra in (('--answer', 'replacement'), ('--scope', 'global'),
                      ('--bank-new', 'new'), ('--bank-key', 'email'), ('--key', 'anything')):
            result = self.command('tray_answer.py', '--approve-qresolve', decision['decision_id'], *extra)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read(self.queue_name), self.rows)

    def test_symlink_proposal_is_rejected_without_mutation(self):
        decision = self.propose()
        path = self.home / self.proposal_name
        elsewhere = self.home / 'copied-proposals.json'
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
        self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
        self.assertEqual(self.read(self.queue_name), self.rows)

    def test_noncanonical_bank_rejected_before_external_lock_created(self):
        decision = self.propose()
        outside = self.home / 'other-bank.json'
        outside.write_bytes((self.home / self.bank_name).read_bytes())
        before = self.snapshot()
        self.assertEqual(self.approve(decision, extra=('--bank', str(outside)))[1]['status'], 'HOLD')
        self.assertEqual(before, self.snapshot())

    def test_symlink_lock_diagnostics_refused_without_external_overwrite(self):
        decision = self.propose()
        outside = self.home / 'untouched.txt'
        outside.write_text('untouched')
        for name in ('hidden_files/queue.lock', 'hidden_files/queue.lock.meta',
                     'hidden_files/queue_lock_waits.jsonl', 'data/answer_bank.json.lock'):
            with self.subTest(name=name):
                path = self.home / name
                old = path.read_bytes() if path.exists() else None
                path.unlink(missing_ok=True)
                path.symlink_to(outside)
                try:
                    self.assertEqual(self.approve(decision)[1]['status'], 'HOLD')
                    self.assertEqual(outside.read_text(), 'untouched')
                    self.assertEqual(self.read(self.queue_name), self.rows)
                finally:
                    path.unlink()
                    if old is not None:
                        path.write_bytes(old)


if __name__ == '__main__':
    unittest.main()
