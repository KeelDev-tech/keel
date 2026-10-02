"""Real CLI validation of synthetic preparation packets and current queues."""
import json
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engines'))
import packet_contract
from safe_io import atomic_json, utc_now


class ValidatePacketCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='keel-validate-packet-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.now = utc_now()
        self.entry = {'role_id': 'fixture-role', 'company': 'Fixture', 'title': 'Fixture role',
                      'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': 75,
                      'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/1',
                      'materials': {'resume': 'resume.txt'},
                      'posting_verification': {'identity': ['greenhouse', 'fixture', '1'],
                                              'verdict': 'live', 'observed_at': self.now.isoformat()}}
        self.bank = {'answers': {'first_name': 'Synthetic', 'last_name': 'Applicant',
                                 'email': 'synthetic@fixture.invalid'}}
        self.bank['_provenance'] = {key: packet_contract.answer_receipt(value, 'synthetic fixture', now=self.now)
                                   for key, value in self.bank['answers'].items()}
        self.policy = {'main_fit_floor': 75}
        (self.home / 'resume.txt').write_text('Synthetic review material')
        for name in ('standard', 'strategic', 'needs_input', 'rejected'):
            self.queue(name, [self.entry] if name == 'standard' else [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        atomic_json(self.home / 'data/answer_bank.json', self.bank)
        atomic_json(self.home / 'data/policy.json', self.policy)
        self.packet = packet_contract.prepare(self.entry, self.bank, self.policy, self.home,
            self.entry['materials'], {'questions': []}, now=self.now)
        self.path = self.home / 'data/launch-packets/fixture.json'
        atomic_json(self.path, self.packet)

    def queue(self, name, rows):
        atomic_json(self.home / f'data/queues/{name}-queue.json', rows)

    def run_cli(self, expected=0):
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'keel.py'), '--home', str(self.home),
                                 'validate-packet', str(self.path)], cwd=ROOT,
                                env={**os.environ, 'KEEL_HOME': str(self.home / 'unused-default')}, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.home / 'unused-default').exists())
        for relative, body in before.items():
            self.assertEqual((self.home / relative).read_bytes(), body, str(relative))
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def test_current_packet_validates_without_execution_authority(self):
        self.assertEqual(self.run_cli(), {'valid_preparation_packet': True, 'execution_authorized': False})

    def test_missing_role_is_rejected(self):
        self.queue('standard', [])
        self.run_cli(2)

    def test_duplicate_role_across_queues_is_rejected(self):
        self.queue('strategic', [self.entry])
        self.run_cli(2)

    def test_duplicate_posting_under_another_role_is_rejected(self):
        self.queue('strategic', [{**self.entry, 'role_id': 'other-role'}])
        self.run_cli(2)

    def test_current_hold_is_rejected(self):
        self.entry['human_hold'] = True
        self.queue('standard', [self.entry])
        packet = packet_contract.prepare(self.entry, self.bank, self.policy, self.home,
            self.entry['materials'], {'questions': []}, now=self.now)
        atomic_json(self.path, packet)
        self.run_cli(2)

    def test_changed_entry_is_rejected(self):
        self.queue('standard', [{**self.entry, 'title': 'Changed role'}])
        self.run_cli(2)

    def test_new_terminal_ledger_outcome_is_rejected(self):
        atomic_json(self.home / 'data/application-ledger.json',
                    [{**self.entry, 'role_id': 'other-role', 'status': 'UNKNOWN_OUTCOME'}])
        self.run_cli(2)

    def test_missing_canonical_queue_is_rejected(self):
        (self.home / 'data/queues/strategic-queue.json').unlink()
        self.run_cli(2)

    def test_strategic_queue_current_packet_is_valid(self):
        self.queue('standard', [])
        self.queue('strategic', [self.entry])
        self.assertFalse(self.run_cli()['execution_authorized'])

    def test_needs_input_queue_remains_held(self):
        self.queue('standard', [])
        self.queue('needs_input', [self.entry])
        self.run_cli(2)

    def test_expired_packet_is_rejected(self):
        self.packet['created_at'] = (self.now - timedelta(hours=3)).isoformat()
        self.packet['expires_at'] = (self.now - timedelta(hours=1)).isoformat()
        from safe_io import digest
        self.packet['integrity_sha256'] = digest({k: v for k, v in self.packet.items() if k != 'integrity_sha256'})
        atomic_json(self.path, self.packet)
        self.run_cli(2)

    def test_changed_material_is_rejected(self):
        (self.home / 'resume.txt').write_text('Changed synthetic material')
        self.run_cli(2)

    def test_pending_queue_recovery_is_refused_without_mutation(self):
        journal = self.home / 'hidden_files/queue.lock.transactions/pending.json'
        atomic_json(journal, {'synthetic_pending_transaction': True})
        result = self.run_cli(2)
        self.assertIn('recovery required', result['error'])


if __name__ == '__main__':
    unittest.main()
