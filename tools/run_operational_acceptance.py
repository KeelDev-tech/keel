#!/usr/bin/env python3
"""Source-bound offline operational acceptance; never a live host qualification."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_operational.common import FLAGS, atomic_json, new_home, outside_source
from keel_operational.demo import run_demo
from tools.operational_inventory import inventory, read_file


def collect_checks(demo, home, *, source_unchanged, network_events):
    """Derive release checks from executed component evidence and retained bytes."""
    checks = []
    def check(name, condition):
        if type(condition) is not bool:
            raise ValueError('boolean_acceptance_condition_required')
        checks.append({'name': name, 'status': 'PASS' if condition else 'FAIL'})
    components = demo['components']
    check('synthetic_rehearsal_completed', demo['status'] == 'PASS' and demo['synthetic'] is True)
    for component, minimum in (('control', 5), ('runtime', 6), ('recovery', 4)):
        measured = components[component]['checks']
        check(component + '_nonvacuous_checks', type(measured) is dict and len(measured) >= minimum)
        check(component + '_completed', components[component]['status'] == 'PASS')
        for name, value in measured.items():
            check(component + '_' + name, value is True)
    q = components['qualification']
    evidence = q['qualification']
    check('complete_qualification_regrade', q['status'] == 'PASS' and q['fixture_check']['admissible'] is True)
    check('qualification_bound_to_release_source', evidence['source_sha256'] == demo['source_sha256'])
    check('injected_qualification_cannot_qualify_real_host', q['real_host_check']['admissible'] is False)
    check('qualification_all_declared_verbs_exercised', bool(evidence['capabilities']) and
          all(value == 'TESTED' for value in evidence['capabilities'].values()))
    check('qualification_nonempty_original_observations', evidence['observations'] == 23 and
          len(evidence['records']) == 23 and all(row['raw_base64'] for row in evidence['records']))
    check('runtime_executed_complete_fixture', components['runtime']['fixture_steps'] == 23)
    exported = components['fixture_export']
    check('fixture_export_only_synthetic', exported['synthetic'] is True and
          exported['served'] is False and exported['browser_opened'] is False)
    check('fixture_export_bytes_retained', bool(exported['files']) and all(
        hashlib.sha256(read_file(home / 'fixture', name)).hexdigest() == pin
        for name, pin in exported['files'].items()))
    page = read_file(home / 'fixture', 'fixture.html').decode('utf-8')
    check('fixture_scope_accessible_without_agent_script', 'Contract SHA-256: ' in page and
          'Observed browser origin: UNKNOWN' in page and 'aria-label="Full name"' in page)
    check('fixture_submission_not_offered', 'type="submit"' not in page and exported['submission_authorized'] is False)
    check('no_live_capability_claims', all(demo.get(k) == v for k, v in FLAGS.items()))
    check('no_real_model_or_canonical_calls', demo['real_model_calls'] == 0 and demo['real_canonical_writes'] == 0)
    check('no_native_browser_calls', demo['real_native_browser_actions'] == 0)
    check('no_network_or_process_attempts', not network_events)
    check('source_unchanged', source_unchanged)
    if len({row['name'] for row in checks}) != len(checks):
        raise ValueError('duplicate_acceptance_name')
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    out = new_home(outside_source(args.out, ROOT))
    before = inventory(); events = []
    def guard(event, args):
        if event in {'socket.__new__', 'socket.connect', 'subprocess.Popen',
                     'os.system', 'os.exec', 'os.posix_spawn'}:
            events.append(event)
            raise RuntimeError('offline_acceptance_prohibits_network_and_process_execution')
    sys.addaudithook(guard)
    demo = run_demo(out / 'rehearsal', source_sha256=before['sha256'])
    after = inventory()
    checks = collect_checks(demo, out / 'rehearsal', source_unchanged=before == after,
                            network_events=events)
    report = {'schema': 'keel.operational.acceptance.v1',
              'status': 'PASS' if all(row['status'] == 'PASS' for row in checks) else 'FAIL',
              'synthetic': True, 'source_unchanged': before == after,
              'source_sha256': after['sha256'], 'release_source_sha256': after['sha256'],
              'checks': checks, 'checks_total': len(checks),
              'checks_passed': sum(row['status'] == 'PASS' for row in checks),
              'network_or_process_attempts': events, 'rehearsal': demo,
              'competitive_superiority_established': False, **FLAGS}
    atomic_json(out / 'acceptance.json', report)
    print(json.dumps({k: report[k] for k in ('status', 'checks_passed', 'checks_total', 'release_source_sha256')}))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
