"""Exercise malformed preparation inputs through the real, offline operator CLI."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ABSENT = object()
RAW_MATERIAL = 'RAW-SYNTHETIC-MATERIAL-SENTINEL'


class OperatorDiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='keel-doctor-fixture-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'workspace with spaces'
        self.home = self.root / 'empty home'
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {'KEEL_HOME', 'PYTHONPATH', 'PYTHONHOME'}}
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        self.assertEqual(self.cli('init').returncode, 0)
        self.bank_path = self.workspace / 'data/answer_bank.json'
        self.unconfirmed_bank = self.bank_path.read_bytes()
        for key, value in [('first_name', 'Synthetic'), ('last_name', 'Fixture'),
                           ('email', 'synthetic' + '@' + 'example.invalid')]:
            result = self.cli('confirm-answer', '--key', key, '--value', value,
                              '--source', 'synthetic applicant assertion')
            self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('resume.txt', 'cover.txt'):
            (self.workspace / 'data/resumes' / name).write_text('Synthetic fixture only.\n')

    def cli(self, *arguments):
        return subprocess.run([sys.executable, '-S', '-B', str(ROOT / 'keel.py'),
                               '--home', str(self.workspace), *arguments],
                              cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=30)

    def canonical_json(self):
        return {path.relative_to(self.workspace).as_posix(): path.read_bytes()
                for path in self.workspace.rglob('*.json')}

    def queue(self, materials=ABSENT, *, origin='strategic', status='READY'):
        entry = {'role_id': 'SYNTHETIC-1', 'status': status,
                 'ats_url': 'https://example.org/job'}
        if origin == 'strategic':
            entry['resume_version'] = 'resume.txt'
        if materials is not ABSENT:
            entry['materials'] = materials
        for name in ('standard', 'strategic', 'needs_input'):
            path = self.workspace / 'data/queues' / (name + '-queue.json')
            path.write_text(json.dumps([entry] if name == origin else []) + '\n')

    def assert_report(self, result, *, ready, capabilities, reports=1):
        self.assertEqual(result.returncode, 0 if ready else 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertNotIn(RAW_MATERIAL, result.stdout)
        remaining, decoded = result.stdout.lstrip(), []
        decoder = json.JSONDecoder()
        while remaining:
            report, end = decoder.raw_decode(remaining)
            decoded.append(report)
            remaining = remaining[end:].lstrip()
        self.assertEqual(len(decoded), reports)
        report = decoded[0]
        self.assertIs(report['ready_for_local_preparation'], ready)
        self.assertIs(report['provider_verification_available'], False)
        checks = {item['check']: item for item in report['checks']}
        self.assertEqual(len(report['checks']), 9)
        self.assertEqual(set(checks), {'Applicant assertions', 'Workspace policy',
                         'standard queue', 'strategic queue', 'needs_input queue',
                         'READY preparation inputs', 'Queue identity',
                         'Ledger structure', 'Public boundary'})
        self.assertEqual(checks['Public boundary']['status'], 'PASS')
        self.assertIn('no paid APIs or application submission', checks['Public boundary']['detail'])
        self.assertIsInstance(report['observation_warnings'], list)
        self.assertIsInstance(report['profile_answer_review'], dict)
        self.assertEqual('capabilities' in report, capabilities)
        if capabilities:
            self.assertIs(report['capabilities']['application_submission']['authorized'], False)
            self.assertIs(report['capabilities']['publication_authorized'], False)
        return checks

    def doctor(self, *, ready, capabilities=False):
        before = self.canonical_json()
        result = self.cli('doctor', *(['--capabilities'] if capabilities else []))
        self.assertEqual(self.canonical_json(), before)
        return self.assert_report(result, ready=ready, capabilities=capabilities)

    def test_wrong_material_types_refuse_even_with_valid_strategic_fallback(self):
        cases = [('string', RAW_MATERIAL), ('empty string', ''), ('list', [RAW_MATERIAL]),
                 ('empty list', []), ('true', True), ('false', False),
                 ('integer', 1), ('zero', 0), ('float', 1.5), ('zero float', 0.0)]
        for index, (name, materials) in enumerate(cases):
            status = ('READY', 'READY-FOR-BROWSER')[index % 2]
            for capabilities in (False, True):
                with self.subTest(materials=name, status=status, capabilities=capabilities):
                    self.queue(materials, status=status)
                    checks = self.doctor(ready=False, capabilities=capabilities)
                    self.assertEqual(checks['Applicant assertions']['status'], 'PASS')
                    self.assertEqual(checks['READY preparation inputs']['status'], 'ACTION_REQUIRED')
                    self.assertEqual(checks['READY preparation inputs']['detail'],
                                     'SYNTHETIC-1: materials must be an object')

    def test_other_inspected_queues_report_wrong_types_for_both_ready_statuses(self):
        for origin in ('standard', 'needs_input'):
            for status in ('READY', 'READY-FOR-BROWSER'):
                for materials in (True, False):
                    with self.subTest(origin=origin, status=status, materials=materials):
                        self.queue(materials, origin=origin, status=status)
                        checks = self.doctor(ready=False, capabilities=materials)
                        self.assertEqual(checks['READY preparation inputs']['detail'],
                                         'SYNTHETIC-1: materials must be an object')

    def test_absent_null_and_empty_object_keep_strategic_fallback(self):
        for name, materials in [('absent', ABSENT), ('null', None), ('object', {})]:
            for capabilities in (False, True):
                with self.subTest(materials=name, capabilities=capabilities):
                    self.queue(materials)
                    checks = self.doctor(ready=True, capabilities=capabilities)
                    self.assertEqual(checks['READY preparation inputs']['status'], 'PASS')

    def test_valid_mapping_and_nonready_rows_keep_local_preparation_controls(self):
        for origin in ('standard', 'strategic'):
            for capabilities in (False, True):
                with self.subTest(origin=origin, capabilities=capabilities):
                    self.queue({'resume': 'data/resumes/resume.txt',
                                'cover_letter': 'data/resumes/cover.txt'},
                               origin=origin, status='READY-FOR-BROWSER')
                    checks = self.doctor(ready=True, capabilities=capabilities)
                    self.assertEqual(checks['READY preparation inputs']['status'], 'PASS')
        self.queue(RAW_MATERIAL, origin='needs_input', status='NEEDS-INPUT')
        for capabilities in (False, True):
            with self.subTest(nonready=True, capabilities=capabilities):
                checks = self.doctor(ready=True, capabilities=capabilities)
                self.assertEqual(checks['READY preparation inputs']['status'], 'PASS')

    def test_standard_missing_resume_stays_action_required(self):
        for name, materials in [('absent', ABSENT), ('null', None), ('object', {})]:
            for capabilities in (False, True):
                with self.subTest(materials=name, capabilities=capabilities):
                    self.queue(materials, origin='standard')
                    checks = self.doctor(ready=False, capabilities=capabilities)
                    self.assertEqual(checks['READY preparation inputs']['detail'],
                                     'SYNTHETIC-1: resume missing')

    def test_material_paths_remain_contained(self):
        outside = self.root / 'outside.txt'
        outside.write_text('Synthetic outside fixture.\n')
        (self.workspace / 'data/resumes/link.txt').symlink_to(outside)
        for resume in ('../outside.txt', str(outside), 'data/resumes/link.txt'):
            with self.subTest(resume=resume):
                self.queue({'resume': resume}, origin='standard')
                checks = self.doctor(ready=False, capabilities=True)
                self.assertEqual(checks['READY preparation inputs']['status'], 'ACTION_REQUIRED')

    def test_valid_materials_do_not_supply_missing_applicant_assertions(self):
        self.bank_path.write_bytes(self.unconfirmed_bank)
        self.queue({'resume': 'data/resumes/resume.txt'})
        for capabilities in (False, True):
            with self.subTest(capabilities=capabilities):
                checks = self.doctor(ready=False, capabilities=capabilities)
                self.assertEqual(checks['Applicant assertions']['status'], 'ACTION_REQUIRED')
                for key in ('first_name', 'last_name', 'email'):
                    self.assertIn(key, checks['Applicant assertions']['detail'])

    def test_start_emits_all_reports_for_truthy_and_falsey_malformed_materials(self):
        python = self.root / 'stdlib python'
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -S -B "$@"\n')
        python.chmod(0o700)
        env = dict(self.env, KEEL_PYTHON=str(python))
        for materials in (True, []):
            with self.subTest(materials=materials):
                self.queue(materials)
                before = self.canonical_json()
                result = subprocess.run(['bash', str(ROOT / 'start.sh'), str(self.workspace)],
                                        cwd=self.root, env=env, capture_output=True,
                                        text=True, timeout=30)
                # Downstream diagnostics may create lock metadata; canonical JSON is immutable.
                self.assertEqual(self.canonical_json(), before)
                checks = self.assert_report(result, ready=False, capabilities=True, reports=3)
                self.assertEqual(checks['READY preparation inputs']['detail'],
                                 'SYNTHETIC-1: materials must be an object')


if __name__ == '__main__':
    unittest.main()
