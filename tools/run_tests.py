#!/usr/bin/env python3
"""Run active subsystems separately, with temporary state and explicit results.

Suites other than local_profile require the separately supplied audit guard
file, or the explicit --allow-unguarded option for reviewed local checks. A
configured file is not proof that an audit hook was installed or enforced.
local_profile tests the extracted source with Python -S and synthetic state.
No mode is an OS sandbox. Run untrusted changes inside your own disposable
OS/container boundary. The orchestrator must start outside any audit hook;
an active hook cannot be removed from the current process.
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


def audit_guard_available():
    """File presence only; this does not prove that a hook is operational."""
    return (ROOT / 'tools/test_guard/sitecustomize.py').is_file()


def suite_environment(name, cwd, temp, out, *, allow_unguarded=False):
    """Describe the configured environment without claiming unseen isolation."""
    env = dict(os.environ)
    env.update(KEEL_HOME=temp, PYTHONDONTWRITEBYTECODE='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               HYPOTHESIS_STORAGE_DIRECTORY=temp + '/hypothesis')
    guard_path = ROOT / 'tools/test_guard'
    guard_requested = name != 'local_profile'
    guard_present = audit_guard_available()
    guarded = guard_requested and guard_present
    if guard_requested and not guard_present and not allow_unguarded:
        raise RuntimeError('audit guard unavailable: tools/test_guard/sitecustomize.py; '
                           'use --allow-unguarded only for reviewed local checks')
    inherited_search = env.get('PYTHONPATH', '').split(os.pathsep)
    if guarded:
        env.update(KEEL_AUDIT_TEST_ROOT=temp, KEEL_AUDIT_REPORT=str(out),
                   KEEL_AUDIT_CODE=str(ROOT))
        search = [guard_path, cwd, cwd / 'tests', ROOT, ROOT / 'engines']
    else:
        # No nonexistent hook or stale guard configuration should appear in
        # an explicitly unguarded child. local_profile also uses -S children
        # without source-tree import paths to test the actual distribution.
        for key in ('KEEL_AUDIT_TEST_ROOT', 'KEEL_AUDIT_REPORT', 'KEEL_AUDIT_CODE'):
            env.pop(key, None)
        inherited_search = [entry for entry in inherited_search
                            if entry and Path(entry).resolve() != guard_path.resolve()]
        search = [cwd, cwd / 'tests', ROOT, ROOT / 'engines']
    env['PYTHONPATH'] = os.pathsep.join(map(str, search)) + os.pathsep + os.pathsep.join(inherited_search)
    isolation = {
        'workspace': 'temporary synthetic workspace',
        'python_audit_hook': ('configured_not_verified' if guarded else
                              'unavailable' if guard_requested else 'not_configured'),
        'audit_guard_file_present': guard_present,
        'audit_hook_enforcement_verified': False,
        'unguarded_explicitly_allowed': guard_requested and not guard_present and allow_unguarded,
        'scope': ('audit guard configured; hook installation and enforcement not verified' if guarded else
                  'explicitly unguarded reviewed local checks' if guard_requested else
                  'reviewed extracted-profile checks; Python -S children; no live network workflow'),
        'os_sandbox': False,
    }
    return env, isolation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', required=True)
    parser.add_argument('--suite', action='append', choices=list(SUITES))
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--allow-unguarded', action='store_true',
                        help='run reviewed local suites without the unavailable audit guard; '
                             'reports do not claim hook enforcement or OS isolation')
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    if importlib.util.find_spec('pytest') is None:
        parser.error('install free requirements-dev.txt and requirements-marketing.txt first')
    selected_suites = args.suite or list(SUITES)
    if (any(name != 'local_profile' for name in selected_suites)
            and not audit_guard_available() and not args.allow_unguarded):
        parser.error('audit guard unavailable: tools/test_guard/sitecustomize.py; '
                     'use --allow-unguarded only for reviewed local checks')
    report = Path(args.report_dir).absolute()
    report.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    for name in selected_suites:
        relative, paths = SUITES[name]
        cwd = ROOT / relative
        out = report / name
        out.mkdir()
        with tempfile.TemporaryDirectory(prefix='keel-test-') as temp:
            env, isolation = suite_environment(name, cwd, temp, out,
                                               allow_unguarded=args.allow_unguarded)
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
                   guard='per-suite guard configuration recorded; audit-hook enforcement not verified; not an OS sandbox')
    (report/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
