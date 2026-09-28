"""Real offline CLI/actuator integration on synthetic canonical workspaces."""
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
OFFLINE = '''
import runpy, sys
def deny(event, args):
    if event.startswith('socket.'):
        raise RuntimeError('network forbidden in question resolution')
sys.addaudithook(deny)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


class QuestionResolverCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-qresolve-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.questions = {
            'first_name': ('What is your first name?', 'Example'),
            'last_name': ('What is your last name?', 'Applicant'),
            'email': ('What is your email address?', 'person@example.com'),
            'phone': ('What is your phone number?', '+1-555-0100'),
            'timezone': ('What is your time zone?', 'America/Los_Angeles'),
        }
        self.bank = {'answers': {key: {'value': answer, 'scope': 'global',
            'question': question, 'provenance': "the applicant's own words " + date.today().isoformat()}
            for key, (question, answer) in self.questions.items()}}
        self.rows = [{'role_id': 'fixture-' + key, 'company': 'Example Employer',
                      'title': 'Example Role', 'fit_score': 70,
                      'status': 'NEEDS-INPUT', 'unresolved': [question],
                      'status_reason': 'input required', 'queue_notes': [],
                      'last_verify_attempt': '2026-01-01'}
                     for key, (question, _) in self.questions.items()]
        self.write('data/answer_bank.json', self.bank)
        self.write('data/queues/needs_input-queue.json', self.rows)
        self.write('data/queues/standard-queue.json', [])
        self.write('data/application-ledger.json', [])

    def write(self, name, value):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def read(self, name):
        return json.loads((self.home / name).read_text())

    def cli(self, *args, role='operator'):
        environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run([sys.executable, '-B', '-S', '-c', OFFLINE, str(ROOT / 'keel.py'),
            '--home', str(self.home), '--role', role, 'qresolve', *args],
            env=environment, cwd=ROOT, capture_output=True, text=True, timeout=60)

    def success(self, *args):
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def snapshot(self):
        return {str(path.relative_to(self.home)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.home.rglob('*') if path.is_file()}

    def authorize(self):
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': True})

    def test_bare_cli_is_readonly_and_five_facts_are_proposed(self):
        before = self.snapshot()
        report = self.success()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report['cards_seen'], 5)
        self.assertFalse(report['auto_apply_enabled'])
        self.assertEqual({d['action'] for d in report['decisions']}, {'draft'})
        self.assertTrue(all(d['evidence'][0]['pointer'].startswith('/answers/') for d in report['decisions']))

    def test_live_default_attaches_drafts_without_mutating_bank_or_queues(self):
        before = {p: (self.home / p).read_bytes() for p in self.snapshot()}
        report = self.success('--live')
        self.assertEqual(report['metrics']['drafted'], 5)
        self.assertEqual(report['canonical_writes'], 0)
        self.assertTrue(all((self.home / p).read_bytes() == raw for p, raw in before.items()))
        cards = self.read('hidden_files/input-tray.json')['cards']
        self.assertEqual(len(cards), 5)
        self.assertTrue(all(c['draft']['owner'] == 'qresolve' for c in cards))
        again = self.success('--live')
        self.assertEqual(again['canonical_writes'], 0)
        self.assertTrue(all(c['draft']['evidence'] for c in self.read('hidden_files/input-tray.json')['cards']))

    def test_explicit_authorization_applies_through_real_actuator_and_replay_does_nothing(self):
        self.authorize()
        bank_before = (self.home / 'data/answer_bank.json').read_bytes()
        report = self.success('--live')
        self.assertEqual(report['metrics']['auto_applied'], 5, report)
        self.assertEqual(report['canonical_writes'], 5)
        after = self.read('data/queues/needs_input-queue.json')
        self.assertTrue(all(row['unresolved'] == [] for row in after))
        self.assertTrue(all(row['status'] == 'NEEDS-INPUT' for row in after))
        self.assertTrue(all(row['last_verify_attempt'] is None for row in after))
        self.assertEqual((self.home / 'data/answer_bank.json').read_bytes(), bank_before)
        journal = [json.loads(line) for line in (self.home / 'hidden_files/qresolve-resolutions.jsonl').read_text().splitlines()]
        self.assertEqual([row['action'] for row in journal], ['INTENT', 'auto_applied'] * 5)
        self.assertTrue(all(row['evidence'] for row in journal))
        backups = list((self.home / 'data/queues').glob('_backup-tray-answer-*'))
        self.assertEqual(len(backups), 5)
        self.assertTrue(all((path / 'answer_bank.json').is_file() for path in backups))
        self.assertEqual(self.read('hidden_files/input-tray.json')['cards'], [])
        replay = self.success('--live')
        self.assertEqual(replay['cards_seen'], 0)
        self.assertEqual(replay['metrics']['auto_applied'], 0)

    def test_missing_provenance_conflicts_expiry_and_new_authority_remain_held(self):
        self.authorize()
        self.bank['answers']['first_name'].pop('provenance')
        self.bank['answers']['last_name']['expires'] = '2000-01-01'
        self.bank['answers']['email_conflict'] = {**self.bank['answers']['email'], 'value': 'different@example.com'}
        self.bank['answers']['phone']['provenance'] = "the applicant's own words 2001-01-01"
        self.bank['answers']['timezone']['scope'] = 'employer:Different Employer'
        self.write('data/answer_bank.json', self.bank)
        report = self.success('--live')
        self.assertEqual(report['canonical_writes'], 0)
        self.assertFalse(any(d['action'] == 'auto_apply' for d in report['decisions']))
        self.assertEqual(self.read('data/queues/needs_input-queue.json'), self.rows)

    def test_structural_essay_consent_and_judgment_never_auto_apply(self):
        self.authorize()
        prompts = ['Application route is apply-by-email', 'Why do you want this role?',
                   'Do you consent to interview recording?', 'When can you start?']
        rows = [{**self.rows[0], 'role_id': 'blocked-' + str(i), 'unresolved': [q]}
                for i, q in enumerate(prompts)]
        self.write('data/queues/needs_input-queue.json', rows)
        self.bank['answers'] = {str(i): {**self.bank['answers']['first_name'],
                                          'question': q, 'value': 'Approved quotation'}
                                for i, q in enumerate(prompts)}
        self.write('data/answer_bank.json', self.bank)
        report = self.success('--live')
        self.assertEqual(report['canonical_writes'], 0)
        self.assertFalse(any(d['action'] == 'auto_apply' for d in report['decisions']))
        self.assertEqual(self.read('data/queues/needs_input-queue.json'), rows)

    def test_incomplete_intent_blocks_automation_and_new_scope_is_not_hidden(self):
        self.authorize()
        folder = self.home / 'hidden_files'
        folder.mkdir(exist_ok=True)
        (folder / 'qresolve-resolutions.jsonl').write_text(json.dumps({'decision_id': 'a' * 64, 'action': 'INTENT'}) + '\n')
        report = self.success('--live')
        self.assertTrue(report['pending_intent'])
        self.assertEqual(report['canonical_writes'], 0)
        self.assertEqual(len(self.read('hidden_files/input-tray.json')['cards']), 5)

    def test_resolved_fingerprint_does_not_hide_changed_or_new_employer_obligation(self):
        self.authorize()
        self.success('--live')
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': False})
        self.rows[0]['role_id'] = 'new-role'
        self.rows[0]['company'] = 'Another Employer'
        self.write('data/queues/needs_input-queue.json', [self.rows[0]])
        report = self.success('--live')
        self.assertEqual(report['cards_seen'], 1)
        self.assertEqual(report['decisions'][0]['action'], 'draft')
        self.assertEqual(len(self.read('hidden_files/input-tray.json')['cards']), 1)

    def test_invalid_limits_config_and_symlink_fail_without_canonical_writes(self):
        before = self.snapshot()
        for limit in ('0', '501', '-1'):
            self.assertEqual(self.cli('--max-cards', limit).returncode, 2)
        self.assertEqual(self.snapshot(), before)
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': 'true'})
        self.assertEqual(self.cli('--live').returncode, 2)
        (self.home / 'hidden_files/qresolve-config.json').unlink()
        (self.home / 'data/answer_bank.json').unlink()
        (self.home / 'data/answer_bank.json').symlink_to(ROOT / 'engines/answer_bank.example.json')
        self.assertEqual(self.cli('--live').returncode, 2)
        self.assertEqual(self.read('data/queues/needs_input-queue.json'), self.rows)

    def test_nonoperator_role_cannot_enable_live_resolution(self):
        self.assertEqual(self.cli('--live', role='discovery').returncode, 2)

    def test_timeout_after_partial_write_is_unknown_and_retains_pending_intent(self):
        self.authorize()
        script = '''
import json, os, pathlib, subprocess, sys
os.environ['KEEL_HOME'] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import qresolve
home = pathlib.Path(sys.argv[2])
def uncertain(command, **kwargs):
    decision = json.loads(pathlib.Path(command[-1]).read_text())['decision']
    journal = home / 'hidden_files/qresolve-resolutions.jsonl'
    journal.write_text(json.dumps({'action':'INTENT','decision_id':decision['decision_id']})+'\\n')
    path = home / 'data/queues/needs_input-queue.json'
    rows = json.loads(path.read_text())
    rows[0]['unresolved'] = []
    path.write_text(json.dumps(rows))
    raise subprocess.TimeoutExpired(command, 60)
qresolve.subprocess.run = uncertain
print(json.dumps(qresolve.run(live=True)))
'''
        result = subprocess.run([sys.executable, '-B', '-S', '-c', script,
                                  str(ROOT / 'engines'), str(self.home)],
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'HOLD')
        self.assertIsNone(report['canonical_writes'])
        self.assertTrue(report['outcome_uncertain'])
        self.assertTrue(report['pending_intent'])
        self.assertEqual(report['metrics']['held_or_unconfirmed'], 1)
        self.assertEqual(report['metrics']['auto_applied'], 0)


if __name__ == '__main__':
    unittest.main()
