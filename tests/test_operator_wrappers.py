"""Exercise the shipped setup/start wrappers from outside the source tree."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools import package


class OperatorWrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle_directory = tempfile.TemporaryDirectory(prefix='keel-wrapper-package-')
        cls.addClassCleanup(cls.bundle_directory.cleanup)
        cls.archive = Path(cls.bundle_directory.name) / 'source.zip'
        package.build(cls.archive)
        package.verify(cls.archive)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='keel-wrapper-caller-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.code = self.root / 'extracted source'
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(self.code)
        self.caller = self.root / 'unrelated caller'
        self.caller.mkdir()
        self.home = self.root / 'empty home'
        self.home.mkdir()
        # Exercise real Python without third-party site packages or bytecode writes.
        self.python = self.root / 'stdlib python'
        self.python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -S -B "$@"\n')
        self.python.chmod(0o700)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {'KEEL_HOME', 'PYTHONPATH', 'PYTHONHOME'}}
        self.env.update(HOME=str(self.home), KEEL_PYTHON=str(self.python),
                        PYTHONDONTWRITEBYTECODE='1')

    def snapshot(self, excluded=()):
        result = {}
        for path in self.root.rglob('*'):
            if any(path == item or path.is_relative_to(item) for item in excluded):
                continue
            name = path.relative_to(self.root).as_posix()
            result[name] = ('directory' if path.is_dir() else
                            hashlib.sha256(path.read_bytes()).hexdigest())
        return result

    def cli(self, workspace, *arguments):
        return subprocess.run([str(self.python), str(self.code / 'keel.py'),
                               '--home', str(workspace), *arguments],
                              cwd=self.caller, env=self.env, capture_output=True,
                              text=True, timeout=30)

    def initialize(self, workspace, confirmed=False):
        result = self.cli(workspace, 'init')
        self.assertEqual(result.returncode, 0, result.stderr)
        if confirmed:
            for key, value in [('first_name', 'Synthetic'), ('last_name', 'Fixture'),
                               ('email', 'synthetic' + '@' + 'example.invalid')]:
                result = self.cli(workspace, 'confirm-answer', '--key', key,
                                  '--value', value, '--source', 'synthetic test')
                self.assertEqual(result.returncode, 0, result.stderr)

    def wrapper(self, name, *arguments, workspace_env=None):
        env = dict(self.env)
        if workspace_env is not None:
            env['KEEL_HOME'] = str(workspace_env)
        return subprocess.run(['bash', str(self.code / name), *map(str, arguments)],
                              cwd=self.caller, env=env, capture_output=True,
                              text=True, timeout=30)

    def assert_start(self, result, *, ready):
        self.assertEqual(result.returncode, 0 if ready else 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        decoder, remaining, reports = json.JSONDecoder(), result.stdout.lstrip(), []
        while remaining:
            report, end = decoder.raw_decode(remaining)
            reports.append(report)
            remaining = remaining[end:].lstrip()
        self.assertEqual(len(reports), 3)
        self.assertIs(reports[0]['ready_for_local_preparation'], ready)
        self.assertIs(reports[0]['provider_verification_available'], False)

    def test_setup_relative_argument_and_environment_use_caller_directory(self):
        for selection in ('argument', 'environment'):
            with self.subTest(selection=selection):
                relative = 'new parent/' + selection + ' workspace'
                expected = self.caller / relative
                allowed = self.caller / 'new parent'
                before = self.snapshot(excluded=(allowed,))
                result = (self.wrapper('setup.sh', relative) if selection == 'argument'
                          else self.wrapper('setup.sh', workspace_env=relative))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report, _ = json.JSONDecoder().raw_decode(result.stdout)
                self.assertEqual(Path(report['workspace']), expected)
                bank = json.loads((expected / 'data/answer_bank.json').read_text())
                self.assertTrue(all(value is None for value in bank['answers'].values()))
                self.assertEqual(bank['attestation_scope']['preauthorized_attestation_keys'], [])
                self.assertEqual(self.snapshot(excluded=(allowed,)), before)

    def test_start_relative_argument_and_environment_read_requested_workspace(self):
        for selection in ('argument', 'environment'):
            with self.subTest(selection=selection):
                relative = selection + ' workspace'
                expected = self.caller / relative
                self.initialize(expected, confirmed=True)
                self.initialize(self.code / relative)  # Decoy would report missing assertions.
                before = self.snapshot(excluded=(expected,))
                result = (self.wrapper('start.sh', relative) if selection == 'argument'
                          else self.wrapper('start.sh', workspace_env=relative))
                self.assert_start(result, ready=True)
                self.assertEqual(self.snapshot(excluded=(expected,)), before)

    def test_absolute_paths_and_explicit_argument_precedence_are_preserved(self):
        target = self.root / 'absolute workspace'
        ignored = self.caller / 'ignored environment'
        before = self.snapshot(excluded=(target,))
        result = self.wrapper('setup.sh', target, workspace_env=ignored)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_start(self.wrapper('start.sh', target, workspace_env=ignored), ready=False)
        self.assertFalse(ignored.exists())
        self.assertEqual(self.snapshot(excluded=(target,)), before)
        self.initialize(target, confirmed=True)
        self.assert_start(self.wrapper('start.sh', workspace_env=target), ready=True)

    def test_relative_argument_overrides_relative_environment(self):
        target = self.caller / 'chosen workspace'
        ignored = self.caller / 'ignored workspace'
        before = self.snapshot(excluded=(target,))
        result = self.wrapper('setup.sh', 'chosen workspace', workspace_env='ignored workspace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.JSONDecoder().raw_decode(result.stdout)[0]['workspace'], str(target))
        self.assert_start(self.wrapper('start.sh', 'chosen workspace',
                                       workspace_env='ignored workspace'), ready=False)
        self.assertFalse(ignored.exists())
        self.assertEqual(self.snapshot(excluded=(target,)), before)

    def test_no_workspace_keeps_existing_source_directory_default(self):
        result = self.wrapper('setup.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.JSONDecoder().raw_decode(result.stdout)[0]['workspace'], str(self.code))
        self.assertFalse((self.caller / 'data').exists())
        self.assert_start(self.wrapper('start.sh'), ready=False)

    def test_extra_arguments_fail_without_creating_workspace(self):
        before = self.snapshot()
        for name in ('setup.sh', 'start.sh'):
            with self.subTest(wrapper=name):
                result = self.wrapper(name, 'first', 'second')
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage:', result.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
