"""Integration and evidence-negatives for the source-bound offline rehearsal."""
from copy import deepcopy

import pytest

from keel_operational.demo import run_demo
from tools.run_operational_acceptance import collect_checks


@pytest.fixture(scope='module')
def executed(tmp_path_factory):
    home = tmp_path_factory.mktemp('operational-acceptance') / 'rehearsal'
    return run_demo(home, source_sha256='a' * 64), home


def evaluate(demo, home, **extra):
    return collect_checks(demo, home, source_unchanged=extra.get('source_unchanged', True),
                          network_events=extra.get('network_events', []))


def test_actual_components_and_retained_bytes_pass(executed):
    demo, home = executed
    checks = evaluate(demo, home)
    assert len(checks) >= 35
    assert len({row['name'] for row in checks}) == len(checks)
    assert all(row['status'] == 'PASS' for row in checks)
    assert demo['execution_authorized'] is False
    assert demo['real_native_browser_actions'] == 0


@pytest.mark.parametrize('fault', ['vacuous_control', 'false_recovery', 'unqualified_host',
                                  'missing_verb', 'missing_raw', 'wrong_source', 'partial_runtime',
                                  'live_claim', 'source_write', 'network_attempt'])
def test_overall_pass_does_not_hide_missing_or_contradictory_evidence(executed, fault):
    original, home = executed
    demo = deepcopy(original); extra = {}
    components = demo['components']
    if fault == 'vacuous_control':
        components['control']['checks'] = {}
    elif fault == 'false_recovery':
        first = next(iter(components['recovery']['checks']))
        components['recovery']['checks'][first] = False
    elif fault == 'unqualified_host':
        components['qualification']['real_host_check']['admissible'] = True
    elif fault == 'missing_verb':
        evidence = components['qualification']['qualification']
        evidence['capabilities'][next(iter(evidence['capabilities']))] = 'UNEXERCISED'
    elif fault == 'missing_raw':
        components['qualification']['qualification']['records'][0]['raw_base64'] = ''
    elif fault == 'wrong_source':
        components['qualification']['qualification']['source_sha256'] = 'b' * 64
    elif fault == 'partial_runtime':
        components['runtime']['fixture_steps'] = 1
    elif fault == 'live_claim':
        demo['account_muse_integration'] = 'VERIFIED'
    elif fault == 'source_write':
        extra['source_unchanged'] = False
    else:
        extra['network_events'] = ['socket.connect']
    assert any(row['status'] == 'FAIL' for row in evaluate(demo, home, **extra))
