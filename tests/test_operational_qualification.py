import base64
import hashlib
from pathlib import Path

import pytest

from keel_loki.common import canonical, clone, digest
from keel_loki.forms import bind_fixture
from keel_muse.browser import InjectedFixture
from keel_muse.session import NativeSession, NATIVE_GATE
from keel_operational.qualification import (
    demo, fixture_inputs, freeze_plan, grade, record_observation, run, verify,
)


def kwargs(inputs):
    return {k: inputs[k] for k in ('manifest', 'tool_schemas', 'host_config', 'source_sha256', 'now')}


def execute(home, inputs=None, mutate=None):
    inputs = fixture_inputs() if inputs is None else inputs
    plan = freeze_plan(**inputs)
    fixture = InjectedFixture(inputs['fixture'])
    def capture(request):
        normalized = fixture(request)
        if mutate:
            normalized = mutate(normalized)
        return {'raw': b'original native bytes\x00\n' + canonical(normalized),
                'normalized': normalized,
                'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
    report = run(home, plan, expected_plan_sha256=digest(plan), capture=capture,
                 clock=lambda: inputs['now'], **kwargs(inputs))
    return inputs, plan, report, fixture


@pytest.fixture
def completed(tmp_path):
    return execute(tmp_path / 'qualification')


def check(inputs, plan, report, **changes):
    args = dict(expected_report_sha256=digest(report), plan=plan,
                expected_plan_sha256=digest(plan), **kwargs(inputs))
    args.update(changes)
    return verify(report, **args)


def regrade(inputs, plan, records):
    return grade(plan, records, expected_plan_sha256=digest(plan), **kwargs(inputs))


def rerecord(row, *, request=None, normalized=None, raw=None, at=None, name=None):
    return record_observation(row['request'] if request is None else request,
                              base64.b64decode(row['raw_base64']) if raw is None else raw,
                              row['normalized'] if normalized is None else normalized,
                              captured_at=row['captured_at'] if at is None else at,
                              native_tool_name=row['native_tool_name'] if name is None else name)


def test_executes_actual_state_and_preserves_original_bytes(completed):
    inputs, plan, report, fixture = completed
    assert report['status'] == 'SIMULATED'
    assert report['observations'] == plan['max_observations'] == len(fixture.calls)
    assert report['actions_reported_applied'] == 11
    assert set(report['capabilities'].values()) == {'TESTED'}
    assert fixture.values['full_name'] == inputs['fixture']['plain_values']['full_name']
    assert fixture.values['resume']['sha256'] == hashlib.sha256(base64.b64decode(inputs['fixture']['attachments']['resume'])).hexdigest()
    for row in report['records']:
        assert base64.b64decode(row['raw_base64']).startswith(b'original native bytes\x00\n')
        assert row['raw_sha256'] != row['normalized_sha256']
    assert check(inputs, plan, report, allow_injected=True)['admissible'] is True
    assert check(inputs, plan, report)['admissible'] is False


@pytest.mark.parametrize('change', ['source', 'manifest', 'schema', 'runtime', 'adapter', 'expiry', 'future'])
def test_current_pin_drift_or_time_invalidates(completed, change):
    inputs, plan, report, _ = completed
    params = kwargs(inputs)
    if change == 'source': params['source_sha256'] = 'b' * 64
    if change == 'manifest': params['manifest']['native_tool_names']['set_text'] = 'changed.tool'
    if change == 'schema': params['tool_schemas']['fixture.set_text']['description'] = 'changed'
    if change == 'runtime': params['host_config']['runtime_fingerprint_sha256'] = 'b' * 64
    if change == 'adapter': params['host_config']['adapter_version'] = 'v2'
    if change == 'expiry': params['now'] = plan['expires_at']
    if change == 'future': params['now'] = plan['created_at'] - 1
    with pytest.raises(ValueError):
        check(inputs, plan, report, **params)


@pytest.mark.parametrize('field,value', [('status', 'QUALIFIED_REPORTED'),
                                      ('native_browser_authenticated', True),
                                      ('native_permission_authenticated', True),
                                      ('execution_authorized', True),
                                      ('actions_reported_applied', 50),
                                      ('observations', 24)])
def test_fabricated_pass_or_authority_regraded_not_accepted(completed, field, value):
    inputs, plan, report, _ = completed
    report[field] = value
    with pytest.raises(ValueError, match='tampered'):
        check(inputs, plan, report, allow_injected=True)


@pytest.mark.parametrize('field', ['raw_base64', 'raw_sha256', 'normalized_sha256', 'request_sha256'])
def test_record_byte_and_normalization_integrity(completed, field):
    inputs, plan, report, _ = completed
    row = report['records'][0]
    row[field] = base64.b64encode(b'changed').decode() if field == 'raw_base64' else 'b' * 64
    with pytest.raises(ValueError):
        regrade(inputs, plan, report['records'])


def test_required_final_readback_and_no_vacuous_success(completed):
    inputs, plan, report, _ = completed
    partial = regrade(inputs, plan, report['records'][:-1])
    assert partial['status'] == 'PARTIAL'
    assert partial['final_readback'] is None
    assert 'TESTED' not in partial['capabilities'].values()
    assert check(inputs, plan, partial, allow_injected=True)['admissible'] is False
    empty = regrade(inputs, plan, [])
    assert empty['status'] == 'UNAVAILABLE'
    assert set(empty['capabilities'].values()) == {'UNEXERCISED'}


def test_no_transport_remains_unavailable_no_home_created(tmp_path):
    inputs = fixture_inputs(); plan = freeze_plan(**inputs)
    result = run(tmp_path / 'unused', plan, expected_plan_sha256=digest(plan), clock=lambda: inputs['now'], **kwargs(inputs))
    assert result['status'] == 'UNAVAILABLE'
    assert not (tmp_path / 'unused').exists()


@pytest.mark.parametrize('change', ['omit_middle', 'duplicate', 'wrong_target', 'wrong_tool', 'wrong_receipt', 'stale_snapshot', 'out_of_order_time', 'inactive', 'submission', 'mismatch'])
def test_observation_contract_and_sequence_fail_closed(completed, change):
    inputs, plan, report, _ = completed
    rows = report['records']
    if change == 'omit_middle': del rows[1]
    if change == 'duplicate': rows[2] = clone(rows[0])
    if change == 'wrong_target':
        request = clone(rows[1]['request']); request['target_ref'] = 'wrong'
        rows[1] = rerecord(rows[1], request=request)
    if change == 'wrong_tool': rows[1] = rerecord(rows[1], name='wrong-tool')
    if change == 'wrong_receipt':
        response = clone(rows[1]['normalized']); response['request_sha256'] = 'b' * 64
        rows[1] = rerecord(rows[1], normalized=response)
    if change == 'stale_snapshot':
        response = clone(rows[0]['normalized']); response['observed_at'] -= 11
        rows[0] = rerecord(rows[0], normalized=response)
    if change == 'out_of_order_time': rows[1] = rerecord(rows[1], at=999)
    if change == 'inactive':
        response = clone(rows[0]['normalized']); response['fields'][0]['editable'] = False
        rows[0] = rerecord(rows[0], normalized=response)
    if change == 'submission':
        response = clone(rows[0]['normalized']); response['submission_observed'] = True
        rows[0] = rerecord(rows[0], normalized=response)
    if change == 'mismatch':
        response = clone(rows[-1]['normalized']); response['fields'][0]['value'] = 'Wrong candidate'
        rows[-1] = rerecord(rows[-1], normalized=response)
        result = regrade(inputs, plan, rows)
        assert result['status'] == 'BLOCKED'
        assert result['final_readback']['status'] == 'MISMATCH'
        return
    with pytest.raises(ValueError):
        regrade(inputs, plan, rows)


def test_declared_but_unexercised_capability_blocks(tmp_path):
    inputs = fixture_inputs()
    fixture = inputs['fixture']
    fixture['contract']['fields'] = [f for f in fixture['contract']['fields'] if f['id'] != 'accuracy']
    del fixture['plain_values']['accuracy']
    inputs['fixture'] = bind_fixture(fixture, now=inputs['now'])
    inputs, plan, report, _ = execute(tmp_path / 'partial', inputs)
    assert report['status'] == 'PARTIAL'
    assert report['capabilities']['set_attestation'] == 'UNEXERCISED'
    assert report['missing_required_operations'] == ['set_attestation']
    assert check(inputs, plan, report, allow_injected=True)['admissible'] is False


def test_hashless_attachment_is_partial(tmp_path):
    inputs = fixture_inputs()
    inputs['manifest']['attachment_readback'] = 'metadata_only'
    inputs, plan, report, _ = execute(tmp_path / 'partial', inputs)
    assert report['status'] == 'PARTIAL'
    assert report['final_readback']['unverified_attachment_fields'] == ['resume']


def test_host_report_does_not_authenticate_browser_or_sentinel(tmp_path):
    # This test intentionally demonstrates that a trusted capture can report
    # real-host provenance without proving actual execution. All auth flags
    # remain false. Never call this fixture a live-browser test.
    inputs = fixture_inputs()
    inputs['host_config'].update(transport_mode='HOST_NATIVE_REPORTED', native_gate=NATIVE_GATE)
    def normalize(response):
        if response['schema'] == 'keel.muse.accessibility.v1':
            response['provenance'] = 'HOST_NATIVE_REPORTED'
            for field in response['fields']:
                field['value_provenance'] = 'native_attachment_readback' if field['descriptor']['kind'] == 'attachment' else 'accessibility'
        return response
    inputs, plan, report, _ = execute(tmp_path / 'host-reported', inputs, normalize)
    assert report['status'] == 'QUALIFIED_REPORTED'
    assert check(inputs, plan, report)['admissible'] is True
    assert report['native_browser_authenticated'] is False
    assert report['native_permission_authenticated'] is False
    assert report['execution_authorized'] is False


def test_injected_snapshot_cannot_silently_upgrade_to_real_host(completed):
    inputs, plan, report, _ = completed
    inputs['host_config'].update(transport_mode='HOST_NATIVE_REPORTED', native_gate=NATIVE_GATE)
    real_plan = freeze_plan(**inputs)
    with pytest.raises(ValueError, match='provenance'):
        regrade(inputs, real_plan, report['records'])


def test_native_denial_stops_and_cannot_be_renamed_success(completed):
    inputs, plan, report, _ = completed
    rows = report['records'][:2]
    denied = clone(rows[-1]['normalized']); denied['status'] = 'denied'
    rows[-1] = rerecord(rows[-1], normalized=denied)
    result = regrade(inputs, plan, rows)
    assert result['status'] == 'BLOCKED'
    assert result['reason'] == 'native_denied'
    assert result['actions_reported_applied'] == 0


def test_capture_crash_leaves_issued_intent_without_retry(tmp_path):
    inputs = fixture_inputs(); plan = freeze_plan(**inputs)
    calls = []
    def capture(request):
        calls.append(request)
        raise RuntimeError('capture_crashed')
    with pytest.raises(RuntimeError, match='capture_crashed'):
        run(tmp_path / 'crash', plan, expected_plan_sha256=digest(plan), capture=capture,
            clock=lambda: inputs['now'], **kwargs(inputs))
    state = NativeSession(tmp_path / 'crash' / 'session', inputs['host_config']['host_id'], clock=lambda: inputs['now']).status()
    assert state['status'] == 'WAITING_OBSERVATION'
    assert state['issued_requests'] == len(calls) == 1


def test_slow_capture_cannot_extend_fixture_approvals(tmp_path):
    inputs = fixture_inputs(); plan = freeze_plan(**inputs)
    clock = [inputs['now']]
    fixture = InjectedFixture(inputs['fixture'])
    def capture(request):
        response = fixture(request)
        clock[0] += 301
        return {'raw': canonical(response), 'normalized': response,
                'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
    with pytest.raises(ValueError):
        run(tmp_path / 'slow', plan, expected_plan_sha256=digest(plan), capture=capture,
            clock=lambda: clock[0], **kwargs(inputs))
    state = NativeSession(tmp_path / 'slow' / 'session', inputs['host_config']['host_id'], clock=lambda: clock[0]).status()
    assert state['status'] == 'BLOCKED'
    assert state['issued_requests'] == 1
    assert (tmp_path / 'slow' / 'observation-000.json').is_file()


def test_expired_plan_cannot_dispatch_next_action(tmp_path):
    inputs = fixture_inputs(); plan = freeze_plan(ttl=1, **inputs)
    clock = [inputs['now']]
    fixture = InjectedFixture(inputs['fixture'])
    calls = []
    def capture(request):
        calls.append(request['operation'])
        response = fixture(request)
        clock[0] += 1
        return {'raw': canonical(response), 'normalized': response,
                'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
    with pytest.raises(ValueError, match='expired'):
        run(tmp_path / 'expired', plan, expected_plan_sha256=digest(plan), capture=capture,
            clock=lambda: clock[0], **kwargs(inputs))
    assert calls == ['accessibility_snapshot']
    state = NativeSession(tmp_path / 'expired' / 'session', inputs['host_config']['host_id'], clock=lambda: clock[0]).status()
    assert state['status'] == 'BLOCKED'
    assert state['action_attempts'] == 0


def test_time_advance_after_dispatch_commit_prevents_capture(tmp_path, monkeypatch):
    inputs = fixture_inputs(); plan = freeze_plan(**inputs)
    clock = [inputs['now']]
    calls = []
    original = NativeSession.next_request
    def delayed(self, **args):
        result = original(self, **args)
        if args.get('dispatch'):
            clock[0] += 31
        return result
    monkeypatch.setattr(NativeSession, 'next_request', delayed)
    with pytest.raises(ValueError, match='dispatch_deadline'):
        run(tmp_path / 'late-dispatch', plan, expected_plan_sha256=digest(plan), capture=lambda request: calls.append(request),
            clock=lambda: clock[0], **kwargs(inputs))
    assert calls == []


@pytest.mark.parametrize('mutator', [
    lambda x: x['host_config'].update(fixture_authorized=False),
    lambda x: x['host_config'].update(fixture_origin='https://unrelated.example'),
    lambda x: x['fixture'].update(synthetic=False),
    lambda x: x['host_config'].update(transport_mode='HOST_NATIVE_REPORTED'),
    lambda x: x['tool_schemas'].update(unmapped={'type': 'object'}),
    lambda x: x.update(required_operations=[]),
])
def test_fixture_scope_and_trusted_configuration_exact(mutator):
    inputs = fixture_inputs(); mutator(inputs)
    with pytest.raises(ValueError): freeze_plan(**inputs)


def test_demo_reports_only_fixture_result(tmp_path):
    report = demo(tmp_path / 'demo')
    assert report['status'] == 'PASS'
    assert report['synthetic'] is True
    assert report['real_native_browser_calls'] == report['model_calls'] == 0
    assert report['real_host_check']['admissible'] is False
