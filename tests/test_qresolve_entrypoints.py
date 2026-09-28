"""Offline fresh-process tests of the public approval/recovery CLI boundaries."""
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
        raise RuntimeError('network forbidden')
sys.addaudithook(deny)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


class QuestionResolutionEntrypoints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        question = 'What is your first name?'
        self.write('data/answer_bank.json', {'answers': {'first_name': {
            'value': 'PRIVATE-APPLICANT', 'scope': 'global', 'question': question,
            'provenance': "the applicant's own words " + date.today().isoformat()}}})
        self.write('data/queues/needs_input-queue.json', [{
            'role_id': 'synthetic-role', 'company': 'PRIVATE-EMPLOYER',
            'title': 'Fixture Role', 'fit_score': 80, 'status': 'NEEDS-INPUT',
            'unresolved': [question], 'status_reason': 'input required', 'queue_notes': []}])
        self.write('data/queues/standard-queue.json', [])
        self.write('data/application-ledger.json', [])

    def write(self, name, value):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def cli(self, command, *args, role='operator'):
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL'}}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, '-B', '-S', '-c', OFFLINE,
            str(ROOT / 'keel.py'), '--home', str(self.home), '--role', role,
            command, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        for marker in ('PRIVATE-APPLICANT', 'PRIVATE-EMPLOYER'):
            self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stdout + result.stderr)
        return result

    def snapshot(self):
        return {str(p.relative_to(self.home)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file()}

    def test_approve_exact_draft_and_recover_inspection_preserve_bank(self):
        self.assertEqual(self.cli('qresolve', '--live').returncode, 0)
        proposal = json.loads((self.home / 'hidden_files/qresolve-proposals.json').read_text())
        decision = proposal['decisions'][0]['decision_id']
        before = self.snapshot()
        dry = self.cli('qresolve-approve', '--decision-id', decision)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(self.snapshot(), before)
        recovered = self.cli('qresolve-recover')
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(self.snapshot(), before)
        bank = (self.home / 'data/answer_bank.json').read_bytes()
        applied = self.cli('qresolve-approve', '--decision-id', decision, '--live')
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(json.loads(applied.stdout)['status'], 'APPLIED')
        self.assertEqual((self.home / 'data/answer_bank.json').read_bytes(), bank)
        replay = self.cli('qresolve-approve', '--decision-id', decision, '--live')
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertEqual(json.loads(replay.stdout)['status'], 'NO_CHANGE')
        self.assertEqual(self.cli('qresolve').returncode, 0)

    def test_role_refusal_and_invalid_inputs_have_no_writes(self):
        before = self.snapshot()
        for role in ('discovery', 'preparation', 'evidence', 'security', 'reliability', 'qa'):
            for command in ('qresolve-recover', 'qresolve-approve'):
                result = self.cli(command, '--decision-id', 'a' * 64, '--live', role=role)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for command in ('qresolve-recover', 'qresolve-approve'):
            result = self.cli(command, '--decision-id', '../PRIVATE-APPLICANT')
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_live_planning_rejects_external_lock_diagnostic_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside) / 'sentinel'
            sentinel.write_bytes(b'external file must remain unchanged')
            folder = self.home / 'hidden_files'
            folder.mkdir()
            (folder / 'queue.lock.meta').symlink_to(sentinel)
            result = self.cli('qresolve', '--live')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_bytes(), b'external file must remain unchanged')
            self.assertFalse((folder / 'qresolve-proposals.json').exists())


if __name__ == '__main__':
    unittest.main()
