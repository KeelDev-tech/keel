"""Connect actual source-file context, durable admission and native protocol.

The included rehearsal uses synthetic evidence and an injected native tool.
Its complete typed form is not an authenticated employer application.
"""
from pathlib import Path

from .common import atomic_json, clone, digest, new_home


def demo(home):
    from keel_loki.forms import bind_fixture
    from .context import make_demo_inputs, compile_context, validate_cache
    from .browser import InjectedFixture, NativeAdapter
    from .capabilities import fixture_manifest
    from .coordinator import Coordinator

    home = new_home(home)
    inputs = make_demo_inputs(home / 'evidence')
    compiled = compile_context(inputs['contract'], inputs['support'], **inputs['kwargs'])
    if compiled['status'] != 'COMPILED' or not compiled['coverage_complete']:
        raise ValueError('complete_fixture_context_required')
    now = inputs['kwargs']['now']
    fixture = clone(inputs['fixture'])
    fixture['plain_values'] = clone(compiled['host_only']['preparation_values'])
    fixture = bind_fixture(fixture, now=now)
    # Bind each synthetic reviewed field to the bytes actually read by the
    # compiler. This fixture-only helper never issues real human approvals.
    for fid, value in fixture['values'].items():
        value['evidence_sha256'] = clone(compiled['host_only']['evidence_sha256'][fid])
    for approval in fixture['approvals']:
        approval['evidence_sha256'] = clone(fixture['values'][approval['field_id']]['evidence_sha256'])
    driver = InjectedFixture(fixture)
    adapter = NativeAdapter(fixture_manifest(), driver, driver.host_snapshot,
                            driver.permission, driver.record_halt, clock=lambda: now)
    reports = []
    dependencies = {'context': compiled['cache_key']}
    def host(task):
        return {'schema': 'keel.muse.host-snapshot.v1', 'workspace_id': 'fixture-workspace',
                'scope_id': task['scope_id'], 'account_id': task['account_id'],
                'resource_id': task['resource_id'], 'dependencies': clone(task['dependencies']),
                'consent': True, 'no_ai': False, 'approval_current': True, 'holds': [],
                'unknown_attempt': False, 'rate_limited': False,
                'issued_at': now, 'expires_at': now + 60}
    def prepare(_):
        validate_cache(compiled, expected_context_sha256=digest(compiled),
            root=inputs['kwargs']['root'], source_head_provider=inputs['kwargs']['source_head_provider'],
            expected_policy_sha256=inputs['kwargs']['expected_policy_sha256'],
            expected_contract_sha256=inputs['kwargs']['expected_contract_sha256'],
            expected_support_sha256=inputs['kwargs']['expected_support_sha256'], now=now)
        observed = adapter.prepare(fixture['contract'], fixture['values'], fixture['approvals'],
                                   attachments=fixture['attachments'])
        reports.append(observed)
        return {'status': 'RECORDED', 'receipt_sha256': digest(observed)}
    coordinator = Coordinator(home / 'coordinator', 'fixture-workspace',
                              {'prepare': prepare}, host, clock=lambda: now)
    task = {'schema': 'keel.muse.task.v1', 'task_id': 'prepare-fixture',
            'workspace_id': 'fixture-workspace', 'scope_id': 'fixture-scope',
            'handler_id': 'prepare', 'account_id': fixture['contract']['account_id'],
            'resource_id': 'fixture-browser', 'dependencies': dependencies, 'payload': {}, 'priority': 5}
    queued = coordinator.enqueue(task)
    idle = coordinator.worker_once('fixture-worker')
    event = {'schema': 'keel.muse.event.v1', 'event_id': 'context-observed',
             'workspace_id': 'fixture-workspace', 'scope_id': 'fixture-scope', 'kind': 'SOURCE',
             'payload': {'source_id': 'context', 'revision_sha256': compiled['cache_key'],
                         'expected_previous_sha256': None}}
    wake = coordinator.ingest(event)
    duplicate = coordinator.ingest(event)
    finished = coordinator.worker_once('fixture-worker')
    after = coordinator.worker_once('fixture-worker')
    if len(reports) != 1:
        raise ValueError('exactly_one_fixture_handler_required')
    observed = reports[0]
    # Change actual input bytes. An unchanged support manifest cannot now be
    # reused just because the earlier preparation succeeded.
    name = inputs['support']['fields']['full_name']['path']
    changed_path = Path(inputs['kwargs']['root']) / name
    changed_path.write_text('{"value":"Corrected synthetic name","synthetic":true}')
    stale_rejected = False
    try:
        validate_cache(compiled, expected_context_sha256=digest(compiled),
            root=inputs['kwargs']['root'], source_head_provider=inputs['kwargs']['source_head_provider'],
            expected_policy_sha256=inputs['kwargs']['expected_policy_sha256'],
            expected_contract_sha256=inputs['kwargs']['expected_contract_sha256'],
            expected_support_sha256=inputs['kwargs']['expected_support_sha256'], now=now)
    except ValueError:
        stale_rejected = True
    checks = {'all_fixture_fields_have_support': compiled['coverage_complete'],
              'unchanged_blocker_does_not_invoke_handler': idle['handler_calls_attempted'] == 0,
              'event_replay_returns_same_receipt': wake == duplicate,
              'one_admitted_preparation': finished['handler_calls_attempted'] == 1,
              'no_repeat_after_recorded_outcome': after['handler_calls_attempted'] == 0,
              'complete_injected_native_readback': observed['status'] == 'SIMULATED'
                  and observed['field_count'] == 12 and observed['active_field_count'] == 11,
              'actual_source_correction_invalidates_context': stale_rejected}
    result = {'schema': 'keel.muse.integration-demo.v1',
              'status': 'PASS' if all(checks.values()) else 'FAIL', 'synthetic': True,
              'checks': checks, 'context_sha256': digest(compiled),
              'model_context_bytes': compiled['model_context_bytes'],
              'field_manifest': compiled['host_only']['field_manifest'],
              'queue_admission': queued, 'worker_result': finished, 'native_result': observed,
              'durable_coordinator': coordinator.snapshot(),
              'actual_human_decisions': 0, 'real_model_calls': 0, 'real_browser_actions': 0,
              'canonical_writes': 0, 'account_muse_integration': 'NOT_VERIFIED',
              'actual_native_browser': 'NOT_RUN', 'execution_authorized': False,
              'production_deployed': False}
    atomic_json(home / 'integration.json', result)
    return result
