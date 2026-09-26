#!/usr/bin/env python3
"""Run active subsystems separately, with temporary state and explicit results.

All suites except the explicitly reviewed local_profile suite inherit the
Python audit hook. local_profile verifies a clean extracted installation with
Python -S and no inherited hook; its tests use only synthetic local workflows.
Neither mode is an OS sandbox. Run untrusted changes inside your own disposable
OS/container boundary. The orchestrating runner itself must start outside the
audit hook; an active Python audit hook cannot be removed from a process.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    'core': ('.', ['tests']),
    'local_profile': ('.', ['tests/test_release_profile.py']),
    'security': ('.', ['security/tests']),
    'privacy': ('.', ['privacy/tests']),
    'maintenance': ('maintenance_workbench', ['tests']),
    'integrations': ('.', ['integrations', 'program', 'promotion', 'shadow', 'publish']),
    'monitors': ('.', ['monitors']),
    'canary': ('canary', ['tests']),
    'marketing': ('.', ['marketing-engine']),
}


def suite_environment(name, cwd, temp, out):
    """Keep clean-install verification explicit and all other hooks intact."""
    env = dict(os.environ)
    env.update(KEEL_HOME=temp, PYTHONDONTWRITEBYTECODE='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               HYPOTHESIS_STORAGE_DIRECTORY=temp + '/hypothesis')
    guard_path = ROOT / 'tools/test_guard'
    guarded = name != 'local_profile'
    inherited_search = env.get('PYTHONPATH', '').split(os.pathsep)
    if guarded:
        env.update(KEEL_AUDIT_TEST_ROOT=temp, KEEL_AUDIT_REPORT=str(out),
                   KEEL_AUDIT_CODE=str(ROOT))
        search = [guard_path, cwd, cwd / 'tests', ROOT, ROOT / 'engines']
    else:
        # Only this named suite may launch reviewed -S children and remove
        # source-tree import paths to test the actual extracted distribution.
        for key in ('KEEL_AUDIT_TEST_ROOT', 'KEEL_AUDIT_REPORT', 'KEEL_AUDIT_CODE'):
            env.pop(key, None)
        inherited_search = [entry for entry in inherited_search
                            if entry and Path(entry).resolve() != guard_path.resolve()]
        search = [cwd, cwd / 'tests', ROOT, ROOT / 'engines']
    env['PYTHONPATH'] = os.pathsep.join(map(str, search)) + os.pathsep + os.pathsep.join(inherited_search)
    isolation = {
        'workspace': 'temporary synthetic workspace',
        'python_audit_hook': 'inherited' if guarded else 'not inherited',
        'scope': ('guarded active subsystem checks' if guarded else
                  'reviewed extracted-profile checks; Python -S children; no live network workflow'),
        'os_sandbox': False,
    }
    return env, isolation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', required=True)
    parser.add_argument('--suite', action='append', choices=list(SUITES))
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    if importlib.util.find_spec('pytest') is None:
        parser.error('install free requirements-dev.txt and requirements-marketing.txt first')
    report = Path(args.report_dir).absolute()
    report.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    for name in args.suite or SUITES:
        relative, paths = SUITES[name]
        cwd = ROOT / relative
        out = report / name
        out.mkdir()
        with tempfile.TemporaryDirectory(prefix='keel-test-') as temp:
            env, isolation = suite_environment(name, cwd, temp, out)
            command = [sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider',
                       '--import-mode=importlib', '-q', '-ra', '--tb=short',
                       '--continue-on-collection-errors', '--basetemp=' + temp + '/pytest',
                       '--junitxml=' + str(out/'junit.xml'), *paths]
            if name == 'core':
                command.append('--ignore=tests/test_release_profile.py')
            with (out/'pytest.log').open('w') as log:
                try:
                    status = subprocess.run(command, cwd=cwd, env=env, stdout=log,
                                            stderr=subprocess.STDOUT, timeout=args.timeout).returncode
                except subprocess.TimeoutExpired:
                    status = 124
            counts = dict(passed=0, failed=0, errors=0, skipped=0)
            if (out/'junit.xml').is_file():
                for case in ET.parse(out/'junit.xml').getroot().iter('testcase'):
                    category = ('errors' if case.find('error') is not None else
                                'failed' if case.find('failure') is not None else
                                'skipped' if case.find('skipped') is not None else 'passed')
                    counts[category] += 1
            row = dict(suite=name, exit_code=status, counts=counts, command=command,
                       isolation=isolation)
            results.append(row)
            print(json.dumps(row), flush=True)
    complete = all(r['exit_code'] == 0 and r['counts']['passed'] > 0
                   and not r['counts']['skipped'] for r in results)
    summary = dict(schema_version=1, status='PASS' if complete else 'INCOMPLETE',
                   suites=results, production_state='not supplied',
                   scope='active subsystem suites; historical snapshots not executed',
                   guard='per-suite isolation recorded: audit hook except reviewed local_profile; not an OS sandbox')
    (report/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
