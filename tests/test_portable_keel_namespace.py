"""Canonical public imports must work from an arbitrarily named extraction."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PortableNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.copy = Path(self.temp.name) / 'arbitrary-extraction-name'
        self.copy.mkdir()
        (self.copy / 'engines').mkdir()
        (self.copy / 'privacy').mkdir()
        shutil.copyfile(ROOT / 'keel.py', self.copy / 'keel.py')
        for name in ('safe_io.py', 'safe_http.py', 'packet_contract.py'):
            shutil.copyfile(ROOT / 'engines' / name, self.copy / 'engines' / name)
        for file in (ROOT / 'privacy').glob('*.py'):
            shutil.copyfile(file, self.copy / 'privacy' / file.name)

    def run_python(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-B', *args], cwd=self.copy,
                              env=env, text=True, capture_output=True, timeout=20)

    def test_public_namespace_is_real_extraction_and_denies_unapproved_release(self):
        code = '''import json
from pathlib import Path
import keel
from keel.privacy import publication_guard
assert Path(keel.__file__).resolve() == Path('keel.py').resolve()
assert Path(publication_guard.__file__).resolve() == Path('privacy/publication_guard.py').resolve()
assert callable(keel.initialize)
result = publication_guard.check_egress('synthetic-release', 'a'*64, state_dir='empty-state')
print(json.dumps({'allowed':result['allowed'], 'namespace':keel.__package__}))
'''
        result = self.run_python('-c', code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'allowed': False, 'namespace': 'keel'})

    def test_script_and_canonical_module_entrypoints_remain_available(self):
        for args in [('keel.py', '--help'), ('-m', 'keel.privacy.gate_release', '--help')]:
            with self.subTest(args=args):
                result = self.run_python(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout)


if __name__ == '__main__': unittest.main()
