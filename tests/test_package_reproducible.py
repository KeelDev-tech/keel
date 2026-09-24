#!/usr/bin/env python3
"""Tests for package.sh reproducibility + payload + scrub behavior (K36/K37).

Runs the REAL package.sh against a small clean fixture tree (via a symlink,
since package.sh anchors on its own directory) with HOME pointed at an empty
fake home so the fallback shape-based scrub pattern applies.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

KEEL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_SH = os.path.join(KEEL_ROOT, 'package.sh')
BACKUP_SH = os.path.join(KEEL_ROOT, 'package_backup-20260918-k36k37-packaging.sh')
FIXED_DT = (2020, 1, 1, 0, 0, 0)
# Split so this source file itself never contains an email-shaped token that
# the fallback scrub pattern would flag when tests/ is packaged.
PROBE_ADDR = 'probe@' + 'example.com'


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = 'wb' if isinstance(content, bytes) else 'w'
    with open(path, mode) as fh:
        fh.write(content)


class PackagePortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='keel-pkg-test-')
        self.fakehome = os.path.join(self.tmp, 'fakehome')
        os.makedirs(self.fakehome)
        fx = os.path.join(self.tmp, 'fixture')
        os.makedirs(fx)
        self.fx = fx
        os.symlink(PACKAGE_SH, os.path.join(fx, 'package.sh'))
        _write(fx, 'VERSION', '0.9.9-test\n')
        _write(fx, 'README.md', '# fixture\n')
        _write(fx, 'LICENSE', 'fixture license\n')
        _write(fx, 'engines/clean.py', 'print("clean")\n')
        _write(fx, 'engines/run.sh', '#!/usr/bin/env bash\necho hi\n')
        _write(fx, 'docs/guide.md', '# guide\n')
        _write(fx, 'monitors/watch.py', 'print("watch")\n')
        _write(fx, 'worker-charter/notes.md', '# notes\n')
        _write(fx, 'sample_data/rows.csv', 'a,b\n1,2\n')
        _write(fx, 'tests/test_clean.py', 'import unittest\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pkg(self, *args):
        env = dict(os.environ)
        env['HOME'] = self.fakehome
        return subprocess.run(
            ['bash', os.path.join(self.fx, 'package.sh')] + list(args),
            cwd=self.fx, env=env, capture_output=True, text=True, timeout=180)

    def artifact(self):
        return os.path.join(self.fx, 'dist', 'keel-0.9.9-test.zip')

    def sha256(self, path):
        with open(path, 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def build_ok(self):
        result = self.run_pkg()
        self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
        return result

    def test_two_builds_are_byte_identical(self):
        self.build_ok()
        first = self.sha256(self.artifact())
        os.remove(self.artifact())
        self.build_ok()
        second = self.sha256(self.artifact())
        self.assertEqual(first, second)
        with zipfile.ZipFile(self.artifact()) as archive:
            infos = archive.infolist()
            for info in infos:
                self.assertEqual(info.date_time, FIXED_DT, msg=info.filename)
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in infos}
            self.assertEqual(modes['engines/run.sh'], 0o755)
            self.assertEqual(modes['engines/clean.py'], 0o644)

    def test_manifest_verifies_and_tamper_detected(self):
        self.build_ok()
        result = self.run_pkg('--verify', self.artifact())
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['verified_integrity'])
        with zipfile.ZipFile(self.artifact()) as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        tampered = self.artifact() + '.tampered.zip'
        with zipfile.ZipFile(tampered, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, body in entries.items():
                if name == 'engines/clean.py':
                    body = body + b'# tampered\n'
                archive.writestr(name, body)
        result = self.run_pkg('--verify', tampered)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('manifest mismatch', result.stdout)

    def test_verify_rejects_missing_manifest(self):
        bare = os.path.join(self.tmp, 'bare.zip')
        with zipfile.ZipFile(bare, 'w') as archive:
            archive.writestr('a.txt', b'hi')
        result = self.run_pkg('--verify', bare)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MANIFEST.json missing', result.stdout)

    def test_payload_includes_monitors_and_worker_charter(self):
        self.build_ok()
        with zipfile.ZipFile(self.artifact()) as archive:
            names = archive.namelist()
        self.assertIn('monitors/watch.py', names)
        self.assertIn('worker-charter/notes.md', names)
        self.assertIn('MANIFEST.json', names)
        with zipfile.ZipFile(self.artifact()) as archive:
            manifest = json.loads(archive.read('MANIFEST.json'))
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['version'], '0.9.9-test')
        self.assertEqual(set(manifest['files']) | {'MANIFEST.json'}, set(names))
        for name, record in manifest['files'].items():
            with zipfile.ZipFile(self.artifact()) as archive:
                body = archive.read(name)
            self.assertEqual(len(body), record['bytes'])
            self.assertEqual(hashlib.sha256(body).hexdigest(), record['sha256'])

    def test_scrub_exclusions_absent_from_payload(self):
        _write(self.fx, 'engines/__pycache__/x.pyc', b'\x00\x01compiled')
        _write(self.fx, 'engines/briefs/b.txt', 'brief\n')
        _write(self.fx, 'engines/answer_bank.json', '{"k": "v"}\n')
        _write(self.fx, 'engines/employer_form_patterns.json', '{}\n')
        _write(self.fx, 'tests/__pycache__/y.pyc', b'\x00\x01compiled')
        self.build_ok()
        with zipfile.ZipFile(self.artifact()) as archive:
            names = archive.namelist()
        for name in names:
            self.assertNotIn('__pycache__', name)
            self.assertNotIn('.pytest_cache', name)
            self.assertFalse(name.startswith('engines/briefs'))
            self.assertFalse(name.startswith('data/'))
            self.assertFalse(name.startswith('dist/'))
        self.assertNotIn('engines/answer_bank.json', names)
        self.assertNotIn('engines/employer_form_patterns.json', names)

    def test_scrub_blocks_personal_data_shape(self):
        _write(self.fx, 'engines/probe.txt', 'reach us at %s\n' % PROBE_ADDR)
        result = self.run_pkg()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('BLOCKED', result.stdout)
        self.assertFalse(os.path.exists(self.artifact()))

    def test_symlink_in_payload_refused(self):
        os.symlink('clean.py', os.path.join(self.fx, 'engines', 'link.py'))
        result = self.run_pkg()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink', result.stderr.lower())

    def test_existing_output_refused_unless_forced(self):
        self.build_ok()
        result = self.run_pkg()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)
        result = self.run_pkg('--force')
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_scrub_block_preserved_verbatim(self):
        def scrub_section(path):
            lines = open(path).read().splitlines()
            start = next(i for i, line in enumerate(lines)
                         if line.startswith('# Scrub anything that must never ship'))
            end = None
            seen_exit = False
            for i in range(start, len(lines)):
                if 'exit 1' in lines[i]:
                    seen_exit = True
                if seen_exit and lines[i] == 'fi':
                    end = i
                    break
            self.assertIsNotNone(end)
            section = lines[start:end + 1]
            return [line.replace('/tmp/keel-pkg', '@STAGE@')
                        .replace('$STAGE', '@STAGE@')
                        .replace('"', '') for line in section]

        self.assertTrue(os.path.exists(BACKUP_SH))
        self.assertEqual(scrub_section(BACKUP_SH), scrub_section(PACKAGE_SH))
        text = open(PACKAGE_SH).read()
        for token in ('engines/__pycache__',
                      'engines/briefs',
                      'engines/answer_bank.json',
                      'engines/employer_form_patterns.json',
                      'SCRUB_FILE="$HOME/.config/keel/scrub-patterns"',
                      'BLOCKED: personal-data hits in package payload:',
                      "grep -v '/package.sh$'"):
            self.assertIn(token, text)
        self.assertIn("grep -v '^PATTERN=\\|^SCRUB_FILE=\\|^# Personal patterns'",
                      text)


if __name__ == '__main__':
    unittest.main()
