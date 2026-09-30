#!/usr/bin/env python3
"""Offline synthetic audit of grounded packet wording; no truth/READY authority."""
import argparse
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_grounding.demo import make_fixture
from keel_grounding.evidence import GroundingError
from keel_grounding.packet import verify_packet
from keel_trust.common import canonical, digest

SCENARIOS = ('clean', 'wrong_employer', 'wrong_title', 'inflated_skill',
             'unsupported_metric', 'stale_source')


def fixture(root, profile, role):
    f = make_fixture(root)
    values = [f'Synthetic Employer {profile}', f'Synthetic Operator {profile}',
              f'Synthetic Skill {profile}', f'Synthetic metric {profile} units']
    source = f['document']['sources'][0]
    source['publisher_id'] = f'synthetic-person-{profile}'
    raw = canonical({'values': values})
    (root / 'evidence/profile.json').write_bytes(raw)
    source['content_hash'] = hashlib.sha256(raw).hexdigest()
    scope = digest({'candidate_id': source['publisher_id'], 'employer_id': f'synthetic-hiring-{role}',
                    'posting_id': f'synthetic-role-{role}'})
    original = f['document']['claims'][0]
    claims, bindings, statements = [], [], []
    for index, value in enumerate(values):
        claim = deepcopy(original)
        claim.update(claim_id=f'claim-{index}', subject_id=source['publisher_id'],
                     predicate=('employer', 'title', 'skill', 'metric')[index],
                     value_hash=digest(value), allowed_wording=[value], allowed_scopes=[scope],
                     evidence=[{k: source[k] for k in ('source_id', 'revision', 'content_hash')}])
        claims.append(claim)
        bindings.append({'claim_id': claim['claim_id'], 'revision': 'v1', 'value': value,
                         'selections': [{'source_id': source['source_id'], 'selector_id': f'value-{index}'}]})
        statements.append({'claim_id': claim['claim_id'], 'claim_revision': 'v1', 'wording': value})
    f['document']['claims'] = claims
    f['document']['artifacts'][0].update(scope=scope, statements=statements)
    f['bindings']['sources'][0]['selectors'] = [
        {'selector_id': f'value-{i}', 'kind': 'json_pointer', 'pointer': f'/values/{i}'} for i in range(4)]
    f['bindings']['claims'] = bindings
    pin = digest(f['document'])
    f['bindings']['trust_snapshot_sha256'] = f['packet']['trust_snapshot_sha256'] = pin
    f['packet'].update(subject_id=source['publisher_id'], scope=scope)
    write_packet(f, root, values)
    return f, values


def write_packet(f, root, values):
    raw = ('\n'.join(values) + '\n').encode()
    (root / 'packet/answer.txt').write_bytes(raw)
    f['packet']['artifacts'][0]['sha256'] = hashlib.sha256(raw).hexdigest()


def observe(f, root):
    try:
        result = verify_packet(f['document'], f['bindings'], f['packet'],
            evidence_root=root / 'evidence', packet_root=root / 'packet', now=f['now'],
            expected_packet_sha256=digest(f['packet']))
    except GroundingError:
        return 'BLOCKED', False
    return result['status'], result['execution_authorized']


def audit(out):
    out = Path(out).absolute()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    rows = []
    for profile in range(30):
        for role in range(3):
            index = profile * 3 + role
            scenario = SCENARIOS[index % len(SCENARIOS)]
            root = out / f'case-{index:02d}'
            f, values = fixture(root, profile, role)
            clean, clean_authority = observe(f, root)
            if scenario == 'stale_source':
                f['document']['sources'][0]['expires_at'] = (f['now'] - timedelta(seconds=1)).isoformat()
                pin = digest(f['document'])
                f['bindings']['trust_snapshot_sha256'] = f['packet']['trust_snapshot_sha256'] = pin
            elif scenario != 'clean':
                field = SCENARIOS.index(scenario) - 1
                altered = list(values)
                altered[field] = 'Unsupported synthetic claim'
                # Matching caller file/manifest hashes must not launder wording.
                write_packet(f, root, altered)
            observed, authority = observe(f, root)
            expected = 'VERIFIED' if scenario == 'clean' else 'BLOCKED'
            rows.append({'case_id': index, 'profile_id': profile, 'role_id': role,
                         'scenario': scenario, 'expected': expected, 'observed': observed,
                         'clean_control': clean, 'execution_authorized': authority or clean_authority})
    negatives = [row for row in rows if row['expected'] == 'BLOCKED']
    false_verified = sum(row['observed'] == 'VERIFIED' for row in negatives)
    controls_failed = sum(row['clean_control'] != 'VERIFIED' for row in rows)
    failures = sum(row['observed'] != row['expected'] or row['execution_authorized'] for row in rows)
    report = {'schema': 'keel.tailoring-audit.v1', 'synthetic': True,
              'status': 'PASS' if not (failures or controls_failed) else 'FAIL',
              'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                  for name in ('tools/run_tailoring_audit.py', 'keel_grounding/packet.py',
                               'keel_grounding/claims.py', 'keel_grounding/evidence.py',
                               'keel_grounding/demo.py')},
              'profile_count': 30, 'role_count': 3, 'case_count': len(rows),
              'clean_control_count': len(rows), 'clean_control_failures': controls_failed,
              'negative_case_count': len(negatives), 'false_verified_packets': false_verified,
              'false_verified_rate': false_verified / len(negatives),
              'scenario_counts': {name: sum(row['scenario'] == name for row in rows) for name in SCENARIOS},
              'ready_status_measured': False, 'independent_truth_verified': False,
              'live_muse_verified': False, 'execution_authorized': False, 'cases': rows}
    (out / 'audit.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    attempts = []
    def guard(event, args):
        if event in {'socket.__new__', 'socket.connect', 'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
            attempts.append(event)
            raise RuntimeError('tailoring audit forbids network and child processes')
    sys.addaudithook(guard)
    report = audit(args.out)
    if attempts:
        report['status'] = 'FAIL'
    report['external_effect_attempts'] = len(attempts)
    (Path(args.out) / 'audit.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
