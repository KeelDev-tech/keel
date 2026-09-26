import copy
import pytest
from keel_loki.common import digest
from keel_muse.browser import NativeAdapter, NativeError, InjectedFixture, demo
from keel_muse.capabilities import fixture_manifest


def adapter(fixture, transport=None, manifest=None, permission=None, record_halt=None, mode='INJECTED'):
    return NativeAdapter(manifest or fixture_manifest(), transport or fixture, fixture.host_snapshot,
                         permission or fixture.permission, record_halt or fixture.record_halt,
                         clock=lambda: fixture.fixture['now'], transport_mode=mode)


def prepare(fixture, runner):
    return runner.prepare(fixture.contract, fixture.fixture['values'], fixture.fixture['approvals'],
                          attachments=fixture.fixture['attachments'])


def test_actual_injected_executor_covers_all_fields_without_browser_claim():
    fixture = InjectedFixture()
    report = prepare(fixture, adapter(fixture))
    assert report['status'] == 'SIMULATED'
    assert report['field_count'] == 12 and report['active_field_count'] == 11
    assert report['action_attempts'] == report['actions_reported_applied'] == 11
    assert report['transport_calls'] == 23
    assert report['readback']['status'] == 'EXACT_REPORTED_READBACK'
    assert report['native_browser_independently_verified'] is False
    assert report['native_permission_authenticated'] is False
    assert report['human_approval_authenticated'] is False
    assert report['submission_authorized'] is False
    assert not any(x['operation'] in {'submit','evaluate','click','cdp'} for x in fixture.calls)


@pytest.mark.parametrize('kind', ['absent_hash', 'metadata_declaration'])
def test_attachment_without_native_hash_evidence_is_partial(kind):
    fixture = InjectedFixture(attachment_hashes=kind != 'absent_hash')
    manifest = fixture_manifest(attachment_readback='metadata_only' if kind == 'metadata_declaration' else 'sha256')
    report = prepare(fixture, adapter(fixture, manifest=manifest))
    assert report['status'] == 'PARTIAL'
    assert report['readback']['unverified_attachment_fields'] == ['resume']


@pytest.mark.parametrize('flag,value', [('consent',False),('approval_current',False),('no_ai',True),
                                        ('holds',['dedupe']),('unknown_attempt',True),('rate_limited',True)])
def test_keel_gates_block_before_any_native_transport(flag, value):
    fixture = InjectedFixture()
    fixture.fixture['host_snapshot'][flag] = value
    report = prepare(fixture, adapter(fixture))
    assert report['status'] == 'BLOCKED'
    assert report['transport_calls'] == report['action_attempts'] == 0
    assert fixture.calls == []


@pytest.mark.parametrize('kind', ['deny','ask','revoked','expired','wrong_hash'])
def test_native_permission_independent_of_human_application_approval(kind):
    fixture = InjectedFixture()
    def permission(request):
        result = fixture.permission(request)
        if kind in ('deny','ask'):result['decision'] = kind
        elif kind == 'revoked':result['revoked'] = True
        elif kind == 'expired':result['expires_at'] = fixture.fixture['now']
        else:result['request_sha256'] = '0'*64
        return result
    report = prepare(fixture, adapter(fixture, permission=permission))
    assert report['status'] == 'BLOCKED' and report['transport_calls'] == 0


def test_native_allow_cannot_supply_missing_human_approval():
    fixture = InjectedFixture()
    fixture.fixture['approvals'] = []
    report = prepare(fixture, adapter(fixture))
    assert report['status'] == 'BLOCKED' and report['transport_calls'] == 0


@pytest.mark.parametrize('change', ['origin','account','revision','field_inventory','target_collision','takeover','submission','stale','provenance'])
def test_changed_accessibility_scope_and_takeover_stop_before_action(change):
    fixture = InjectedFixture()
    def transport(request):
        result = fixture(request)
        if request['operation'] == 'accessibility_snapshot':
            if change == 'origin':result['origin'] = 'https://example.org'
            elif change == 'account':result['account_id'] = 'other'
            elif change == 'revision':result['revisions']['policy'] = '0'*64
            elif change == 'field_inventory':result['fields'].pop()
            elif change == 'target_collision':result['fields'][1]['target_ref'] = result['fields'][0]['target_ref']
            elif change == 'takeover':result['human_takeover'] = True
            elif change == 'submission':result['submission_observed'] = True
            elif change == 'stale':result['observed_at'] -= 11
            else:result['provenance'] = 'HOST_NATIVE_REPORTED'
        return result
    report = prepare(fixture, adapter(fixture, transport=transport))
    assert report['status'] == 'BLOCKED' and report['action_attempts'] == 0


def test_replayed_snapshot_halts_after_first_effect():
    fixture = InjectedFixture(); first=[]
    def transport(request):
        result = fixture(request)
        if request['operation'] == 'accessibility_snapshot':
            if not first:first.append(copy.deepcopy(result))
            else:return first[0]
        return result
    report = prepare(fixture, adapter(fixture, transport=transport))
    assert report['status'] == 'BLOCKED'
    assert report['actions_reported_applied'] == 1
    assert report['reason'] == 'replayed_accessibility_snapshot'


@pytest.mark.parametrize('flag,value', [('holds',['new_hold']),('rate_limited',True),('unknown_attempt',True),('no_ai',True)])
def test_host_gates_refreshed_between_snapshot_and_effect(flag, value):
    fixture = InjectedFixture()
    def permission(request):
        result = fixture.permission(request)
        if request['operation'] != 'accessibility_snapshot':
            fixture.fixture['host_snapshot'][flag] = value
        return result
    report = prepare(fixture, adapter(fixture, permission=permission))
    assert report['status'] == 'BLOCKED' and report['action_attempts'] == 0
    assert len(fixture.calls) == 1


@pytest.mark.parametrize('status', ['unknown','rate_limited','ask','human_takeover'])
def test_nonapplied_receipt_never_retries_and_records_halt(status):
    fixture = InjectedFixture()
    def transport(request):
        result = fixture(request)
        if request['operation'] != 'accessibility_snapshot':result['status'] = status
        return result
    runner = adapter(fixture, transport=transport)
    report = prepare(fixture, runner)
    assert report['status'] == 'UNKNOWN' and report['action_attempts'] == 1
    assert report['halt_record_callback_completed'] is True
    assert len(fixture.halts) == 1
    assert fixture.fixture['host_snapshot']['unknown_attempt'] is True
    if status == 'rate_limited':assert fixture.fixture['host_snapshot']['rate_limited'] is True
    with pytest.raises(NativeError):prepare(fixture, runner)


def test_receipt_scope_mismatch_is_uncertain_and_stops():
    fixture = InjectedFixture()
    def transport(request):
        result = fixture(request)
        if request['operation'] != 'accessibility_snapshot':result['snapshot_id'] = 'wrong-snapshot'
        return result
    report = prepare(fixture, adapter(fixture, transport=transport))
    assert report['status'] == 'UNKNOWN' and report['actions_reported_applied'] == 0


def test_transport_exception_after_effect_never_exposes_message_or_retries():
    fixture = InjectedFixture()
    def transport(request):
        result = fixture(request)
        if request['operation'] != 'accessibility_snapshot':raise ValueError('private transport diagnostic')
        return result
    report = prepare(fixture, adapter(fixture, transport=transport))
    assert report['status'] == 'UNKNOWN' and report['action_attempts'] == 1
    assert 'private' not in str(report)


def test_failed_halt_persistence_not_claimed_durable():
    fixture = InjectedFixture()
    fixture.fixture['host_snapshot']['holds'] = ['held']
    def sink(_):raise OSError('disk unavailable')
    report = prepare(fixture, adapter(fixture, record_halt=sink))
    assert report['halt_record_callback_completed'] is False
    assert report['durable_halt_storage_verified'] is False


@pytest.mark.parametrize('change', ['missing_upload','mapping_unconfigured','snapshot_binding_missing'])
def test_unsupported_native_capabilities_block_before_any_tool_call(change):
    fixture = InjectedFixture(); manifest = fixture_manifest()
    if change == 'missing_upload':
        manifest['operations'].remove('attach_file');del manifest['native_tool_names']['attach_file']
    elif change == 'mapping_unconfigured':manifest['mapping_status'] = 'HOST_MAPPING_REQUIRED'
    else:manifest['snapshot_action_binding'] = False
    report = prepare(fixture, adapter(fixture, manifest=manifest))
    assert report['status'] == 'BLOCKED' and report['transport_calls'] == 0


def test_attachment_payload_changed_before_any_native_action_is_blocked():
    fixture = InjectedFixture();fixture.fixture['attachments']['resume'] = 'YWJj'
    report = prepare(fixture, adapter(fixture))
    assert report['status'] == 'BLOCKED' and report['transport_calls'] == 0


def test_forged_success_receipts_without_applied_values_fail_readback():
    fixture = InjectedFixture()
    def transport(request):
        old = copy.deepcopy(fixture.values)
        result = fixture(request)
        if request['operation'] == 'set_text' and request['action']['field_id'] == 'full_name':fixture.values = old
        return result
    report = prepare(fixture, adapter(fixture, transport=transport))
    assert report['status'] == 'BLOCKED'
    assert report['readback']['status'] == 'MISMATCH'


def test_injected_observations_cannot_be_relabelled_native():
    fixture = InjectedFixture()
    report = prepare(fixture, adapter(fixture, mode='HOST_NATIVE_REPORTED'))
    assert report['status'] == 'BLOCKED'
    assert report['native_browser_independently_verified'] is False


def test_demo_persists_only_explicit_injected_results(tmp_path):
    report = demo(tmp_path/'native')
    assert report['preparation']['status'] == 'SIMULATED'
    assert report['real_browser_actions'] == report['real_model_calls'] == report['external_network_calls'] == 0
    assert (tmp_path/'native'/'native-demo.json').is_file()


@pytest.mark.parametrize('provenance', ['host_source_only','unavailable','native_attachment_readback'])
def test_nonattachment_values_need_accessibility_observation(provenance):
    fixture = InjectedFixture()
    def transport(request):
        result = fixture(request)
        if request['operation'] == 'accessibility_snapshot':
            result['fields'][0]['value_provenance'] = provenance
        return result
    report = prepare(fixture, adapter(fixture, transport=transport))
    assert report['status'] == 'BLOCKED'
    assert report['action_attempts'] == 0
    assert report['reason'] == 'unverified_accessibility_value_provenance'


def test_direct_readback_does_not_promote_matching_host_source_text():
    from keel_muse.browser import _readback
    from keel_loki.forms import build_plan
    fixture = InjectedFixture()
    report = prepare(fixture, adapter(fixture))
    assert report['status'] == 'SIMULATED'
    snapshot = copy.deepcopy(fixture.latest)
    snapshot['fields'][0]['value_provenance'] = 'host_source_only'
    plan = build_plan(fixture.contract, fixture.fixture['values'], fixture.fixture['approvals'], now=fixture.fixture['now'])
    compared = _readback(snapshot, fixture.contract, plan, 'INJECTED', 'sha256')
    assert compared['status'] == 'PARTIAL'
    assert compared['unverified_fields'] == ['full_name']


@pytest.mark.parametrize('operation', ['accessibility_snapshot', 'set_text'])
def test_slow_host_refresh_cannot_outlive_native_permission(operation):
    fixture = InjectedFixture()
    clock, advance = [fixture.fixture['now']], [False]
    def permission(request):
        result = fixture.permission(request)
        if request['operation'] == operation:
            result['expires_at'] = clock[0] + 1
            advance[0] = True
        return result
    def host():
        result = fixture.host_snapshot()
        if advance[0]:
            clock[0] += 2
            advance[0] = False
        return result
    runner = NativeAdapter(fixture_manifest(), fixture, host, permission, fixture.record_halt,
                           clock=lambda: clock[0])
    report = prepare(fixture, runner)
    assert report['status'] == 'BLOCKED'
    assert report['action_attempts'] == 0
    assert len(fixture.calls) == (0 if operation == 'accessibility_snapshot' else 1)
    assert report['reason'] == 'native_permission_denied_pending_stale_or_mismatched'
