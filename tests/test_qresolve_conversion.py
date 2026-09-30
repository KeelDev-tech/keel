"""Offline conversion checks using synthetic queues and the real operator CLI.

These fixtures establish local boundaries, not a reconciliation of the private
pipeline's reported overnight sweep or a claim of production READY supply.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engines.queue_intake import FIT_BAR

ROOT = Path(__file__).resolve().parents[1]
AUDITED_ENTRY = '''
import runpy, sys
from pathlib import Path
def deny_network(event, args):
    if event.startswith('socket.'):
        raise RuntimeError('network forbidden in conversion fixture')
sys.addaudithook(deny_network)
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name='__main__')
'''
# Install the same audit hook in the real actuator child, where Python audit
# hooks are otherwise not inherited. This changes process bootstrapping only;
# planning, revalidation, locking, transaction writes and receipts run normally.
OFFLINE = '''
import subprocess, sys
original_run = subprocess.run
child_entry = ''' + repr(AUDITED_ENTRY) + '''
def offline_child(command, *args, **kwargs):
    if not isinstance(command, list) or command[:3] != [sys.executable, '-B', '-S']:
        raise RuntimeError('unexpected child process in conversion fixture')
    return original_run([*command[:3], '-c', child_entry, *command[3:]], *args, **kwargs)
subprocess.run = offline_child
''' + AUDITED_ENTRY


class QresolveConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-conversion-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.question = 'What is your email address?'
        self.row = {
            'role_id': 'PRIVATE-CONVERSION-ROLE',
            'company': 'PRIVATE-CONVERSION-EMPLOYER',
            'title': 'Synthetic Role', 'fit_score': FIT_BAR,
            'ats_url': 'https://example.invalid/application',
            'status': 'NEEDS-INPUT', 'unresolved': [self.question],
            'status_reason': 'input required', 'queue_notes': [],
            'last_verify_attempt': '2026-01-01',
        }
        self.bank = {'answers': {'email': {
            'value': 'PRIVATE-CONVERSION-ANSWER@example.com',
            'scope': 'global', 'question': self.question,
            'provenance': "the applicant's own words " + date.today().isoformat(),
        }}}
        self.write('data/answer_bank.json', self.bank)
        self.write('data/queues/needs_input-queue.json', [self.row])
        for name in ('standard', 'strategic', 'rejected'):
            self.write(f'data/queues/{name}-queue.json', [])
        self.write('data/application-ledger.json', [])
        (self.home / 'data/employer-blocklist.md').write_text(
            '# Synthetic empty employer blocklist\n', encoding='utf-8')
        self.write('hidden_files/qresolve-config.json', {'AUTO_APPLY_FACTS': True})

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def read(self, relative):
        return json.loads((self.home / relative).read_text(encoding='utf-8'))

    def snapshot(self):
        return {
            str(path.relative_to(self.home)): (
                hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in self.home.rglob('*') if path.is_file()
        }

    def canonical(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in (self.home / 'data').rglob('*') if path.is_file()}

    def cli(self, command, *args, role='operator', overrides=None):
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'LANG', 'LC_ALL'}}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        environment.update(overrides or {})
        return subprocess.run(
            [sys.executable, '-B', '-S', '-c', OFFLINE, str(ROOT / 'keel.py'),
             '--home', str(self.home), '--role', role, command, *args],
            env=environment, cwd=ROOT, capture_output=True, text=True, timeout=60)

    def success(self, command, *args, **kwargs):
        result = self.cli(command, *args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for marker in ('PRIVATE-CONVERSION-ROLE', 'PRIVATE-CONVERSION-EMPLOYER',
                       'PRIVATE-CONVERSION-ANSWER', str(self.home)):
            self.assertNotIn(marker, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def journal(self):
        path = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def make_ready(self):
        # A fixture represents a subsequent verifier commit. QRESOLVE itself
        # must never manufacture this transition or start a browser lane.
        row, = self.read('data/queues/needs_input-queue.json')
        row.update(status='READY', status_reason='synthetic verifier commit',
                   action_band='APPLY', ats_url='https://example.invalid/application')
        self.write('data/queues/needs_input-queue.json', [])
        self.write('data/queues/standard-queue.json', [row])

    def test_lower_tray_visibility_override_cannot_lower_canonical_clearance_floor(self):
        self.row['fit_score'] = FIT_BAR - 1
        self.write('data/queues/needs_input-queue.json', [self.row])
        before = self.canonical()
        report = self.success('qresolve', '--live', overrides={'KEEL_TRAY_MIN_FIT': '60'})
        self.assertEqual(report['cards_seen'], 1)
        self.assertEqual(report['decision_counts']['auto_apply'], 0)
        self.assertEqual(report['canonical_writes'], 0)
        self.assertEqual(self.canonical(), before)
        cards = self.read('hidden_files/input-tray.json')['cards']
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['qresolve']['action'], 'park')
        proposal, = self.read('hidden_files/qresolve-proposals.json')['decisions']
        self.assertEqual(proposal['fit_floor'], FIT_BAR)
        self.assertEqual(proposal['route'], 'held_fit_policy')
        self.assertFalse((self.home / 'hidden_files/qresolve-resolutions.jsonl').exists())

    def test_canonical_floor_fact_clears_without_ready_promotion_and_binds_target_state(self):
        bank_before = (self.home / 'data/answer_bank.json').read_bytes()
        report = self.success('qresolve', '--live', overrides={'KEEL_TRAY_MIN_FIT': '60'})
        self.assertEqual(report['metrics']['auto_applied'], 1)
        row, = self.read('data/queues/needs_input-queue.json')
        self.assertEqual(row['unresolved'], [])
        self.assertEqual(row['status'], 'NEEDS-INPUT')
        self.assertIsNone(row['last_verify_attempt'])
        self.assertEqual(self.read('data/queues/standard-queue.json'), [])
        self.assertEqual((self.home / 'data/answer_bank.json').read_bytes(), bank_before)
        intent, completion = self.journal()
        self.assertEqual((intent['action'], completion['action']), ('INTENT', 'auto_applied'))
        self.assertEqual(intent['target_resolution_state'], completion['target_resolution_state'])
        state = completion['target_resolution_state'][self.row['role_id']]
        self.assertEqual(state['fit_score'], FIT_BAR)
        self.assertTrue(state['fit_eligible'])
        self.assertEqual(state['remaining_blockers'], 0)
        self.assertEqual(state['status'], 'NEEDS-INPUT')
        self.assertEqual(state['status_updated'], row['status_updated'])
        self.assertRegex(state['identity_sha256'], r'^[0-9a-f]{64}$')

    def test_mixed_visibility_card_applies_only_to_current_eligible_roles(self):
        lower = {**self.row, 'role_id': 'lower-fit-fixture', 'fit_score': FIT_BAR - 1}
        self.write('data/queues/needs_input-queue.json', [self.row, lower])
        report = self.success('qresolve', '--live', overrides={'KEEL_TRAY_MIN_FIT': '60'})
        self.assertEqual(report['canonical_writes'], 1)
        rows = {row['role_id']: row for row in self.read('data/queues/needs_input-queue.json')}
        self.assertEqual(rows[self.row['role_id']]['unresolved'], [])
        self.assertEqual(rows[lower['role_id']], lower)
        intent, completion = self.journal()
        self.assertEqual(intent['target_role_ids'], [self.row['role_id']])
        self.assertEqual(completion['role_ids'], [self.row['role_id']])
        card, = self.read('hidden_files/input-tray.json')['cards']
        self.assertEqual(card['leads'][0]['role_id'], lower['role_id'])
        self.assertEqual(card['qresolve']['route'], 'held_fit_policy')

    def test_provisional_sweep_labels_cannot_gain_authority_from_own_words_or_approval(self):
        self.bank['answers']['email'].update(
            provisional=True, approved_verbatim=True,
            notes='Overnight bank sweep; reconciliation report still owed')
        self.write('data/answer_bank.json', self.bank)
        before = self.canonical()
        report = self.success('qresolve', '--live')
        self.assertEqual(report['canonical_writes'], 0)
        self.assertEqual(report['decision_counts']['auto_apply'], 0)
        self.assertEqual(self.canonical(), before)
        proposal, = self.read('hidden_files/qresolve-proposals.json')['decisions']
        self.assertNotEqual(proposal['action'], 'auto_apply')
        hit, = proposal['evidence']
        self.assertTrue(hit['provisional'])
        self.assertFalse(hit['eligible'])
        self.assertFalse(hit['own_words'])
        self.assertFalse(hit['approved_verbatim'])

    def test_known_fact_siblings_clear_one_obligation_at_a_time_before_verification(self):
        phone_question = 'What is your phone number?'
        self.row['unresolved'] = [self.question, phone_question]
        self.bank['answers']['phone'] = {
            'value': '+1-555-0100', 'scope': 'global', 'question': phone_question,
            'provenance': "the applicant's own words " + date.today().isoformat(),
        }
        timezone_question = 'What is your time zone?'
        self.bank['answers']['timezone'] = {
            'value': 'America/Los_Angeles', 'scope': 'global', 'question': timezone_question,
            'provenance': "the applicant's own words " + date.today().isoformat(),
        }
        unrelated = {**self.row, 'role_id': 'unrelated-fixture', 'unresolved': [timezone_question]}
        self.write('data/queues/needs_input-queue.json', [self.row, unrelated])
        self.write('data/answer_bank.json', self.bank)
        report = self.success('qresolve', '--live')
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(report['metrics']['auto_applied'], 2)
        self.assertEqual(report['metrics']['deferred_changed_targets'], 1)
        after = {value['role_id']: value for value in self.read('data/queues/needs_input-queue.json')}
        row = after[self.row['role_id']]
        self.assertEqual(after[unrelated['role_id']]['unresolved'], [])
        self.assertEqual(len(row['unresolved']), 1)
        self.assertIn(row['unresolved'][0], (self.question, phone_question))
        self.assertEqual(row['status'], 'NEEDS-INPUT')
        self.assertEqual(row['last_verify_attempt'], self.row['last_verify_attempt'])
        first, = [value for value in self.journal() if value['action'] == 'auto_applied'
                  and self.row['role_id'] in value['role_ids']]
        self.assertEqual(first['removed_blockers'], 1)
        self.assertEqual(first['fully_unblocked'], 0)
        self.assertEqual(first['target_resolution_state'][self.row['role_id']]['remaining_blockers'], 1)
        report = self.success('qresolve', '--live')
        self.assertEqual(report['metrics']['auto_applied'], 1)
        row = next(value for value in self.read('data/queues/needs_input-queue.json')
                   if value['role_id'] == self.row['role_id'])
        self.assertEqual(row['unresolved'], [])
        self.assertEqual(row['status'], 'NEEDS-INPUT')
        self.assertIsNone(row['last_verify_attempt'])
        self.assertEqual(self.journal()[-1]['fully_unblocked'], 1)
        self.assertEqual(self.read('data/queues/standard-queue.json'), [])

    def test_alo_availability_standing_gate_strips_even_approved_banked_draft(self):
        self.row.update(company='Alo Yoga', unresolved=['What is your availability?'])
        self.bank['answers'] = {'availability': {
            'value': 'PRIVATE-CONVERSION-ANSWER weekdays', 'scope': 'global',
            'question': self.row['unresolved'][0], 'approved_verbatim': True,
            'provenance': "the applicant's own words " + date.today().isoformat(),
        }}
        self.write('data/queues/needs_input-queue.json', [self.row])
        self.write('data/answer_bank.json', self.bank)
        before = self.canonical()
        report = self.success('qresolve', '--live')
        self.assertEqual(report['canonical_writes'], 0)
        self.assertEqual(report['decision_counts']['auto_apply'], 0)
        self.assertEqual(self.canonical(), before)
        card, = self.read('hidden_files/input-tray.json')['cards']
        self.assertIsNone(card.get('draft'))
        proposal, = self.read('hidden_files/qresolve-proposals.json')['decisions']
        self.assertEqual(proposal['class'], 'TRENT-ONLY')
        self.assertEqual(proposal['action'], 'park')
        self.assertIsNone(proposal['answer'])

    def test_supply_readonly_makes_no_files_and_reports_unknown_historical_ready_timing(self):
        self.success('qresolve', '--live')
        records = self.journal()
        resolved = datetime.now(timezone.utc) - timedelta(days=2)
        records[0]['ts'] = (resolved - timedelta(seconds=1)).isoformat()
        records[1]['ts'] = resolved.isoformat()
        path = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        path.write_text(''.join(json.dumps(value) + '\n' for value in records), encoding='utf-8')
        self.make_ready()
        before = self.snapshot()
        report = self.success('qresolve-supply')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(report['execution_authorized'])
        self.assertEqual(report['counts']['resolved_distinct_leads'], 1)
        self.assertEqual(report['counts']['ready_eligible_by_queue_gates'], 1)
        self.assertIsNone(report['resolved_to_ready_24h']['rate'])
        self.assertEqual(report['resolved_to_ready_24h']['unknown'], 1)
        self.assertEqual(report['coverage']['legacy_bank_sweep_distinct_leads'], None)

    def test_supply_live_records_only_metadata_and_replay_preserves_one_observation(self):
        self.success('qresolve', '--live')
        self.make_ready()
        before, canonical = self.snapshot(), self.canonical()
        report = self.success('qresolve-supply', '--live')
        self.assertFalse(report['execution_authorized'])
        self.assertEqual(self.canonical(), canonical)
        after = self.snapshot()
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertTrue(changed <= {
            'hidden_files/qresolve-ready-observations.jsonl', 'hidden_files/queue.lock',
            'hidden_files/queue.lock.meta', 'hidden_files/queue_lock_waits.jsonl',
        }, changed)
        observations = self.home / 'hidden_files/qresolve-ready-observations.jsonl'
        first = observations.read_bytes()
        saved, = [json.loads(line) for line in first.decode().splitlines()]
        self.assertEqual(saved['role_id'], self.row['role_id'])
        self.assertEqual(saved['remaining_blockers'], 0)
        self.assertEqual(saved['status'], 'READY')
        self.success('qresolve-supply', '--live')
        self.assertEqual(observations.read_bytes(), first)
        self.assertEqual(self.canonical(), canonical)

    def test_supply_requires_operator_role_before_live_or_readonly_work(self):
        before = self.snapshot()
        for args in ((), ('--live',)):
            with self.subTest(args=args):
                result = self.cli('qresolve-supply', *args, role='discovery')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_pending_queue_transaction_is_refused_without_recovery_or_canonical_mutation(self):
        self.write('hidden_files/queue.lock.transactions/pending.json', {
            'fixture': 'incomplete transaction; no automatic recovery authorized'})
        canonical = self.canonical()
        for command in ('qresolve', 'qresolve-supply'):
            for args in ((), ('--live',)):
                with self.subTest(command=command, args=args):
                    result = self.cli(command, *args)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.canonical(), canonical)
                    self.assertTrue((self.home / 'hidden_files/queue.lock.transactions/pending.json').exists())
                    for marker in ('PRIVATE-CONVERSION-ROLE', 'PRIVATE-CONVERSION-ANSWER'):
                        self.assertNotIn(marker, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
