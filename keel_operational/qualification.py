"""Pinned host conformance from retained observations, never platform attestation.

All configuration arguments belong to the trusted host. Tool output is data.
The host must capture original bytes before normalization. Hashes detect changes,
not a lying capture adapter, fabricated execution, or host-owner replacement.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import time

from keel_loki.actions import ActionGateway
from keel_loki.common import (atomic_json, canonical, clone, digest, require_dict,
                              require_hash, require_id, require_int)
from keel_loki.forms import demo_fixture, validate_contract
from keel_muse.browser import InjectedFixture, _readback, _request, _snapshot
from keel_muse.capabilities import OPERATIONS, fixture_manifest, validate_manifest
from keel_muse.common import new_home
from keel_muse.session import NATIVE_GATE, NativeSession, _validate_attachments

MAX_RECORDS = 129
MAX_RAW_BYTES = 32768
MAX_RECORD_BYTES = 57344
MAX_REPORT_BYTES = 8 * 1024 * 1024


def _required(operations):
    if (type(operations) is not list or not operations or len(operations) > len(OPERATIONS)
            or any(type(op) is not str or op not in OPERATIONS for op in operations)
            or len(set(operations)) != len(operations) or 'accessibility_snapshot' not in operations):
        raise ValueError('bounded_unique_required_operations_with_snapshot_required')
    return sorted(operations)


def _host(config):
    config = clone(config)
    require_dict(config, {'schema', 'host_id', 'adapter_version', 'runtime_fingerprint_sha256',
                         'fixture_origin', 'fixture_account_id', 'fixture_authorized',
                         'transport_mode', 'native_gate'})
    if config['schema'] != 'keel.operational.qualification-host.v1':
        raise ValueError('qualification_host_schema')
    require_id(config['host_id']); require_id(config['adapter_version'])
    require_hash(config['runtime_fingerprint_sha256']); require_id(config['fixture_account_id'])
    if config['fixture_authorized'] is not True:
        raise ValueError('trusted_host_fixture_authorization_required')
    if config['transport_mode'] not in ('INJECTED', 'HOST_NATIVE_REPORTED'):
        raise ValueError('qualification_mode_invalid')
    expected_gate = NATIVE_GATE if config['transport_mode'] == 'HOST_NATIVE_REPORTED' else None
    if config['native_gate'] != expected_gate:
        raise ValueError('native_tool_permission_gate_mapping_required')
    return config


def _schemas(manifest, schemas):
    schemas = clone(schemas)
    if (type(schemas) is not dict or set(schemas) != set(manifest['native_tool_names'].values())
            or any(type(v) is not dict or not v for v in schemas.values())
            or len(canonical(schemas)) > 65536):
        raise ValueError('exact_bounded_host_tool_schemas_required')
    return schemas


def freeze_plan(fixture, *, manifest, tool_schemas, host_config, source_sha256,
                now, ttl=3600, required_operations=None):
    """Freeze one host-authorized synthetic fixture; no application authority.

    ``fixture_authorized`` is a trusted host configuration decision, never a
    field to accept from a model. Actual native permission is still enforced by
    the tool. All field-level fixture approvals are synthetic, including when
    the fixture is rendered in the real host browser.
    """
    manifest = validate_manifest(manifest)
    tool_schemas = _schemas(manifest, tool_schemas)
    config = _host(host_config)
    require_hash(source_sha256); require_int(now); require_int(ttl, 1, 86400)
    fixture = clone(fixture)
    require_dict(fixture, {'contract', 'plain_values', 'attachments', 'synthetic', 'now',
                          'values', 'approvals', 'host_snapshot'})
    contract = validate_contract(fixture['contract'])
    if (fixture['synthetic'] is not True or fixture['host_snapshot'].get('synthetic') is not True
            or fixture['now'] != now or type(fixture['now']) is not int
            or contract['origin'] != config['fixture_origin']
            or contract['account_id'] != config['fixture_account_id']):
        raise ValueError('exact_authorized_synthetic_fixture_scope_required')
    grant = ActionGateway(contract, fixture['host_snapshot'], fixture['approvals']).issue(fixture['values'], now=now, ttl=300)
    if not 1 <= len(grant['actions']) <= (MAX_RECORDS - 1) // 2:
        raise ValueError('bounded_nonempty_qualification_fixture_required')
    _validate_attachments(fixture['attachments'], grant['actions'])
    if len(canonical(fixture)) > 131072:
        raise ValueError('qualification_fixture_too_large')
    required = _required(list(manifest['operations']) if required_operations is None else required_operations)
    return {'schema': 'keel.operational.qualification-plan.v1', 'fixture': fixture,
            'fixture_sha256': digest(fixture), 'manifest_sha256': digest(manifest),
            'tool_schemas_sha256': digest(tool_schemas), 'host_config_sha256': digest(config),
            'source_sha256': source_sha256, 'host_id': config['host_id'],
            'transport_mode': config['transport_mode'], 'required_operations': required,
            'created_at': now, 'expires_at': now + ttl, 'max_observations': 2 * len(grant['actions']) + 1,
            'submission_authorized': False, 'execution_authorized': False}


def _pinned(plan, expected_plan_sha256, manifest, tool_schemas, host_config, source_sha256, now):
    require_hash(expected_plan_sha256); require_int(now)
    plan = clone(plan)
    if type(plan) is not dict or digest(plan) != expected_plan_sha256:
        raise ValueError('qualification_plan_pin_mismatch')
    rebuilt = freeze_plan(plan['fixture'], manifest=manifest, tool_schemas=tool_schemas,
                          host_config=host_config, source_sha256=source_sha256,
                          now=plan['created_at'], ttl=plan['expires_at'] - plan['created_at'],
                          required_operations=plan['required_operations'])
    if plan != rebuilt:
        raise ValueError('qualification_source_schema_manifest_or_host_drift')
    if not plan['created_at'] <= now < plan['expires_at']:
        raise ValueError('qualification_expired_or_future')
    return plan


def record_observation(request, raw_bytes, normalized, *, captured_at, native_tool_name):
    """Retain exact original tool bytes separately from normalized observations.

    The adapter supplies raw bytes, not a reserialization of its normalized
    output. Tests deliberately use a distinct raw format. Their relationship
    cannot be authenticated here; the normalizer is part of the pinned trusted
    host adapter. No caller-supplied success status enters a record.
    """
    require_int(captured_at)
    if type(raw_bytes) is not bytes or not raw_bytes or len(raw_bytes) > MAX_RAW_BYTES:
        raise ValueError('bounded_nonempty_original_tool_bytes_required')
    if type(native_tool_name) is not str or not native_tool_name or len(native_tool_name) > 256:
        raise ValueError('bounded_native_tool_name_required')
    request, normalized = clone(request), clone(normalized)
    result = {'schema': 'keel.operational.tool-observation.v1', 'request': request,
              'request_sha256': digest(request), 'native_tool_name': native_tool_name,
              'captured_at': captured_at, 'raw_encoding': 'base64',
              'raw_base64': base64.b64encode(raw_bytes).decode(),
              'raw_sha256': hashlib.sha256(raw_bytes).hexdigest(),
              'normalized': normalized, 'normalized_sha256': digest(normalized)}
    if len(canonical(result)) > MAX_RECORD_BYTES:
        raise ValueError('qualification_record_too_large')
    return result


def _record(record):
    require_dict(record, {'schema', 'request', 'request_sha256', 'native_tool_name', 'captured_at',
                          'raw_encoding', 'raw_base64', 'raw_sha256', 'normalized', 'normalized_sha256'})
    try:
        raw = base64.b64decode(record['raw_base64'], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('qualification_original_bytes_invalid') from exc
    rebuilt = record_observation(record['request'], raw, record['normalized'],
                                 captured_at=record['captured_at'], native_tool_name=record['native_tool_name'])
    if record != rebuilt:
        raise ValueError('qualification_observation_hash_or_encoding_mismatch')
    return record


def grade(plan, records, *, expected_plan_sha256, manifest, tool_schemas,
          host_config, source_sha256, now):
    """Regrade every request, observation, action and final field readback.

    This is evidence consistency and conformance, not proof that a real browser
    executed. Original bytes are retained, but only a trusted host capture and
    normalization adapter can establish their relationship to native tools.
    """
    plan = _pinned(plan, expected_plan_sha256, manifest, tool_schemas, host_config, source_sha256, now)
    manifest = validate_manifest(manifest)
    records = clone(records)
    if type(records) is not list or len(records) > plan['max_observations']:
        raise ValueError('qualification_record_count_invalid')
    fixture, mode = plan['fixture'], plan['transport_mode']
    contract = fixture['contract']
    grant = ActionGateway(contract, fixture['host_snapshot'], fixture['approvals']).issue(fixture['values'], now=plan['created_at'], ttl=300)
    cursor, snapshot, readback = 0, None, None
    previous, seen, request_ids = 0, set(), set()
    observed, applied = set(), set()
    reason, last_time = None, plan['created_at']
    mapping_ok = manifest['mapping_status'] == 'HOST_MAPPING_CONFIGURED' and manifest['snapshot_action_binding'] is True
    if not mapping_ok:
        reason = 'HOST_MAPPING_REQUIRED'
    for item in records:
        if reason is not None:
            raise ValueError('qualification_observations_after_stop')
        row = _record(item)
        at = row['captured_at']
        if not last_time <= at <= now or at >= plan['expires_at']:
            raise ValueError('qualification_observation_time_invalid')
        last_time = at
        # Every step independently rechecks the fixture's actual host/approval
        # expiry; report validity never extends permission to continue a run.
        gateway = ActionGateway(contract, fixture['host_snapshot'], fixture['approvals'])
        fresh_grant = gateway.issue(fixture['values'], now=at, ttl=300)
        request, response = row['request'], row['normalized']
        if readback is not None:
            raise ValueError('qualification_observation_after_final_readback')
        if snapshot is None:
            expected = _request('accessibility_snapshot', contract)
        else:
            action = grant['actions'][cursor]
            expected = _request(action['operation'], contract, snapshot=snapshot, action=action,
                                attachment=fixture['attachments'].get(action['field_id']))
        require_hash(request.get('request_id'))
        expected['request_id'] = request['request_id']
        if request != expected or request['request_id'] in request_ids:
            raise ValueError('qualification_request_sequence_or_binding_invalid')
        request_ids.add(request['request_id'])
        operation = request['operation']
        if operation not in manifest['operations']:
            raise ValueError('qualification_undeclared_operation')
        if row['native_tool_name'] != manifest['native_tool_names'][operation]:
            raise ValueError('qualification_tool_mapping_changed')
        if operation == 'accessibility_snapshot':
            snapshot = _snapshot(response, contract, at, previous=previous, seen=seen, mode=mode)
            previous = snapshot['snapshot_revision']; seen.add(snapshot['snapshot_id'])
            observed.add(operation)
            if cursor == len(grant['actions']):
                readback = _readback(snapshot, contract, grant['plan'], mode, manifest['attachment_readback'])
        else:
            _snapshot(snapshot, contract, at, previous=previous - 1, seen=set(), mode=mode)
            target = next(r for r in snapshot['fields'] if r['descriptor']['id'] == request['action']['field_id'])
            if not target['active'] or not target['editable']:
                raise ValueError('qualification_inactive_or_readonly_target')
            for prior in fresh_grant['actions'][:cursor + 1]:
                gateway.consume(fresh_grant['capability'], prior, now=at)
            binding = {k: request[k] for k in ('request_id', 'operation', 'snapshot_id', 'snapshot_revision', 'target_ref')}
            NativeSession._receipt_matches(response, digest(request), binding)
            if response['status'] != 'applied':
                reason = 'native_' + str(response['status'])
                continue
            observed.add(operation); applied.add(operation)
            cursor += 1; snapshot = None
    complete = (readback is not None and readback['status'] == 'EXACT_REPORTED_READBACK'
                and cursor == len(grant['actions']) and len(records) == plan['max_observations'] and reason is None)
    capabilities = {}
    for op in sorted(OPERATIONS):
        capabilities[op] = ('UNAVAILABLE' if op not in manifest['operations'] or not mapping_ok
                            else 'TESTED' if complete and op in observed
                            else 'OBSERVED_UNVERIFIED' if op in observed else 'UNEXERCISED')
    missing = [op for op in plan['required_operations'] if capabilities[op] != 'TESTED']
    if complete and not missing:
        status = 'SIMULATED' if mode == 'INJECTED' else 'QUALIFIED_REPORTED'
    elif reason is not None or readback is not None and readback['status'] == 'MISMATCH':
        status = 'BLOCKED'
    else:
        status = 'UNAVAILABLE' if not records else 'PARTIAL'
    result = {'schema': 'keel.operational.qualification-report.v1', 'plan_sha256': expected_plan_sha256,
              'source_sha256': source_sha256, 'manifest_sha256': digest(manifest),
              'tool_schemas_sha256': digest(tool_schemas), 'host_config_sha256': digest(host_config),
              'fixture_sha256': plan['fixture_sha256'], 'status': status, 'reason': reason,
              'transport_mode': mode, 'synthetic_fixture': True, 'evaluated_at': now,
              'expires_at': plan['expires_at'], 'capabilities': capabilities,
              'missing_required_operations': missing, 'records': records,
              'records_sha256': digest(records), 'observations': len(records),
              'actions_reported_applied': cursor, 'final_readback': readback,
              'raw_normalization_relationship': 'TRUSTED_CAPTURE_ADAPTER_NOT_INDEPENDENTLY_VERIFIED',
              'native_browser_authenticated': False, 'native_permission_authenticated': False,
              'human_approval_authenticated': False, 'execution_authorized': False,
              'submission_authorized': False, 'production_deployed': False}
    if len(canonical(result)) > MAX_REPORT_BYTES:
        raise ValueError('qualification_report_too_large')
    return result


def verify(report, *, expected_report_sha256, plan, expected_plan_sha256, manifest,
           tool_schemas, host_config, source_sha256, now, required_operations=None,
           allow_injected=False):
    """Regrade against current host pins. Drift and expiry fail closed.

    The caller must load expected hashes from trusted host configuration. A
    report accompanied by its own hash is consistency, not authorization.
    Required operations belong to the consuming workflow, not its model.
    """
    require_hash(expected_report_sha256)
    if type(allow_injected) is not bool:
        raise ValueError('allow_injected_must_be_boolean')
    report = clone(report)
    if digest(report) != expected_report_sha256:
        raise ValueError('qualification_report_pin_mismatch')
    _pinned(plan, expected_plan_sha256, manifest, tool_schemas, host_config, source_sha256, now)
    require_int(report['evaluated_at'], plan['created_at'], now)
    rebuilt = grade(plan, report['records'], expected_plan_sha256=expected_plan_sha256,
                    manifest=manifest, tool_schemas=tool_schemas, host_config=host_config,
                    source_sha256=source_sha256, now=report['evaluated_at'])
    if report != rebuilt:
        raise ValueError('qualification_grade_or_authority_tampered')
    required = _required(plan['required_operations'] if required_operations is None else required_operations)
    missing = [op for op in required if report['capabilities'][op] != 'TESTED']
    admitted = (not missing and (report['status'] == 'QUALIFIED_REPORTED'
                                 or allow_injected and report['status'] == 'SIMULATED'))
    return {'schema': 'keel.operational.qualification-check.v1', 'admissible': admitted,
            'status': 'CONFORMANT_REPORTED' if admitted else 'NOT_QUALIFIED',
            'qualification_report_sha256': expected_report_sha256, 'checked_at': now,
            'missing_required_operations': missing, 'transport_mode': report['transport_mode'],
            'native_browser_authenticated': False, 'native_permission_authenticated': False,
            'execution_authorized': False, 'submission_authorized': False}


def run(home, plan, *, expected_plan_sha256, manifest, tool_schemas, host_config,
        source_sha256, capture=None, now=None, clock=None):
    """Drive the existing durable NativeSession once using a trusted capture.

    ``capture(request)`` returns exactly raw(bytes), normalized(JSON), and
    native_tool_name(str). It must invoke only the pinned authorized fixture.
    Capture and normalization failures leave the session issued/uncertain;
    nothing retries. For personal Muse use its serializable NativeSession
    handoff and feed retained records to ``grade`` instead of inventing a Python
    browser API. This function never launches a browser itself.
    """
    clock = clock or (lambda: int(time.time()))
    if not callable(clock) or capture is not None and not callable(capture):
        raise ValueError('trusted_clock_and_capture_callbacks_required')
    live = clock(); require_int(live)
    if now is not None:
        require_int(now)
    now = live if now is None else max(now, live)
    plan = _pinned(plan, expected_plan_sha256, manifest, tool_schemas, host_config, source_sha256, now)
    if capture is None:
        return grade(plan, [], expected_plan_sha256=expected_plan_sha256, manifest=manifest,
                     tool_schemas=tool_schemas, host_config=host_config, source_sha256=source_sha256, now=now)
    home = new_home(home)
    fixture = plan['fixture']
    session = NativeSession.create(home / 'session', workspace_id=host_config['host_id'],
                source_sha256=source_sha256, manifest=manifest, contract=fixture['contract'],
                values=fixture['values'], host_approvals=fixture['approvals'],
                host_snapshot=fixture['host_snapshot'], attachments=fixture['attachments'], now=now,
                transport_mode=host_config['transport_mode'], native_gate=host_config['native_gate'],
                expires_at=min(plan['expires_at'], fixture['host_snapshot']['expires_at'], now + 300), clock=clock)
    records = []
    def fresh():
        nonlocal now
        live = clock(); require_int(live)
        now = max(now, live)
        _pinned(plan, expected_plan_sha256, manifest, tool_schemas, host_config, source_sha256, now)
    for index in range(plan['max_observations']):
        fresh()
        proposed = session.next_request(source_sha256=source_sha256, host_snapshot=fixture['host_snapshot'], now=now)
        if proposed['status'] != 'PROPOSED':
            break
        permission = None
        if host_config['transport_mode'] == 'INJECTED':
            permission = {'schema': 'keel.muse.native-permission.v1', 'permission_id': 'synthetic-qualification',
                          'request_sha256': proposed['request_sha256'], 'decision': 'allow',
                          'issued_at': now, 'expires_at': now + 30, 'revoked': False}
        fresh()
        issued = session.next_request(source_sha256=source_sha256, host_snapshot=fixture['host_snapshot'],
                                      now=now, native_permission=permission, dispatch=True)
        if issued['status'] != 'ISSUED' or issued['dispatch_issued_once'] is not True:
            break
        fresh()
        if now >= issued['lease_until']:
            raise ValueError('qualification_dispatch_deadline_elapsed')
        captured = capture(clone(issued['request']))
        require_dict(captured, {'raw', 'normalized', 'native_tool_name'})
        live = clock(); require_int(live)
        now = max(now, live)
        record = record_observation(issued['request'], captured['raw'], captured['normalized'],
                                    captured_at=now, native_tool_name=captured['native_tool_name'])
        # Persist original/normalized evidence before interpreting it. Each
        # exclusive file is separately recoverable after a controller crash.
        atomic_json(home / ('observation-%03d.json' % index), record)
        records.append(record)
        result = session.observe(captured['normalized'], request_sha256=issued['request_sha256'],
                                 source_sha256=source_sha256, host_snapshot=fixture['host_snapshot'], now=now)
        if result['status'] in ('BLOCKED', 'UNKNOWN', 'SIMULATED', 'PREPARED_REPORTED', 'PARTIAL'):
            break
    report = grade(plan, records, expected_plan_sha256=expected_plan_sha256, manifest=manifest,
                   tool_schemas=tool_schemas, host_config=host_config, source_sha256=source_sha256, now=now)
    atomic_json(home / 'qualification.json', report)
    return report


def fixture_inputs():
    fixture, manifest = demo_fixture(), fixture_manifest()
    config = {'schema': 'keel.operational.qualification-host.v1', 'host_id': 'fixture-host',
              'adapter_version': 'fixture-v1', 'runtime_fingerprint_sha256': digest({'runtime': 'injected'}),
              'fixture_origin': fixture['contract']['origin'], 'fixture_account_id': fixture['contract']['account_id'],
              'fixture_authorized': True, 'transport_mode': 'INJECTED', 'native_gate': None}
    schemas = {name: {'type': 'object', 'description': 'Synthetic fixture protocol only.'}
               for name in manifest['native_tool_names'].values()}
    return {'fixture': fixture, 'manifest': manifest, 'tool_schemas': schemas,
            'host_config': config, 'source_sha256': 'a' * 64, 'now': fixture['now']}


def demo(home, *, source_sha256=None):
    inputs = fixture_inputs()
    if source_sha256 is not None:
        inputs['source_sha256'] = source_sha256
    fixture = InjectedFixture(inputs['fixture'])
    plan = freeze_plan(**inputs)
    kwargs = {k: inputs[k] for k in ('manifest', 'tool_schemas', 'host_config', 'source_sha256', 'now')}
    def capture(request):
        observed = fixture(request)
        return {'raw': b'INJECTED fixture original output\n' + canonical(observed), 'normalized': observed,
                'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
    report = run(home, plan, expected_plan_sha256=digest(plan), capture=capture,
                 clock=lambda: inputs['now'], **kwargs)
    check = verify(report, expected_report_sha256=digest(report), plan=plan,
                   expected_plan_sha256=digest(plan), allow_injected=True, **kwargs)
    rejects_real = verify(report, expected_report_sha256=digest(report), plan=plan,
                          expected_plan_sha256=digest(plan), **kwargs)
    return {'schema': 'keel.operational.qualification-demo.v1', 'status': 'PASS' if check['admissible'] and not rejects_real['admissible'] else 'FAIL',
            'qualification': report, 'fixture_check': check, 'real_host_check': rejects_real,
            'synthetic': True, 'real_native_browser_calls': 0, 'model_calls': 0,
            'execution_authorized': False, 'submission_authorized': False}
