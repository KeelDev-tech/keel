"""Keel's host-callable accessibility normalization protocol for native tools.

No Muse SDK, CDP, page JavaScript, browser launcher or arbitrary tool discovery is
implemented. A trusted host must map these records to its actual controlled tools
and authoritative native permission service. JSON is not authentication.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import secrets
import time

from keel_loki.actions import ActionGateway
from keel_loki.common import atomic_json, clone, digest, require_dict, require_hash, require_id, require_int, LokiError
from keel_loki.forms import (validate_contract, expected_readback, demo_fixture, _value)
from .capabilities import validate_manifest, fixture_manifest

MODES = ('INJECTED', 'HOST_NATIVE_REPORTED')


class NativeError(LokiError):
    pass


def _request(operation, contract, *, snapshot=None, action=None, attachment=None):
    return {'schema': 'keel.muse.native-request.v1', 'request_id': secrets.token_hex(32), 'operation': operation,
            'origin': contract['origin'], 'account_id': contract['account_id'], 'form_id': contract['form_id'],
            'contract_sha256': digest(contract), 'snapshot_id': snapshot['snapshot_id'] if snapshot else None,
            'snapshot_revision': snapshot['snapshot_revision'] if snapshot else None,
            'target_ref': next(row['target_ref'] for row in snapshot['fields'] if row['descriptor']['id'] == action['field_id']) if action else None,
            'action': clone(action), 'attachment': attachment}


def _permission(permission, request, now):
    require_dict(permission, {'schema', 'permission_id', 'request_sha256', 'decision', 'issued_at', 'expires_at', 'revoked'})
    if permission['schema'] != 'keel.muse.native-permission.v1':
        raise NativeError('native_permission_schema')
    require_id(permission['permission_id'])
    require_hash(permission['request_sha256'])
    require_int(permission['issued_at'])
    require_int(permission['expires_at'])
    if (permission['request_sha256'] != digest(request) or permission['decision'] != 'allow' or
            permission['revoked'] is not False or not permission['issued_at'] <= now < permission['expires_at']):
        raise NativeError('native_permission_denied_pending_stale_or_mismatched')


def _snapshot(value, contract, now, *, previous, seen, mode):
    value = clone(value)
    require_dict(value, {'schema', 'snapshot_id', 'snapshot_revision', 'observed_at', 'expires_at', 'origin',
                         'account_id', 'form_id', 'contract_sha256', 'revisions', 'fields', 'unexpected_controls',
                         'human_takeover', 'submission_observed', 'provenance'})
    if value['schema'] != 'keel.muse.accessibility.v1' or value['provenance'] != mode:
        raise NativeError('snapshot_schema_or_provenance_mismatch')
    require_id(value['snapshot_id'])
    require_int(value['snapshot_revision'], 1)
    require_int(value['observed_at'])
    require_int(value['expires_at'])
    if value['snapshot_id'] in seen or value['snapshot_revision'] <= previous:
        raise NativeError('replayed_accessibility_snapshot')
    if not value['observed_at'] <= now < value['expires_at'] or now - value['observed_at'] > 10:
        raise NativeError('stale_accessibility_snapshot')
    if (value['origin'] != contract['origin'] or value['account_id'] != contract['account_id'] or
            value['form_id'] != contract['form_id'] or value['contract_sha256'] != digest(contract) or
            value['revisions'] != contract['revisions']):
        raise NativeError('snapshot_scope_or_revisions_changed')
    if value['human_takeover'] is not False:
        raise NativeError('human_takeover_or_unverified_control')
    if value['submission_observed'] is not False or value['unexpected_controls'] != []:
        raise NativeError('unexpected_controls_or_submission')
    rows = value['fields']
    if type(rows) is not list or len(rows) != len(contract['fields']):
        raise NativeError('incomplete_accessibility_field_inventory')
    prior, refs = {}, set()
    for field, row in zip(contract['fields'], rows):
        require_dict(row, {'descriptor', 'target_ref', 'active', 'editable', 'value', 'value_provenance'})
        if digest(row['descriptor']) != digest(field):
            raise NativeError('accessibility_field_contract_changed')
        require_id(row['target_ref'])
        if row['target_ref'] in refs:
            raise NativeError('ambiguous_accessibility_reference')
        refs.add(row['target_ref'])
        if type(row['active']) is not bool or type(row['editable']) is not bool:
            raise NativeError('invalid_accessibility_flags')
        condition = field['condition']
        active = condition is None or (condition['field_id'] in prior and digest(prior[condition['field_id']]) == digest(condition['equals']))
        if row['active'] != active or (not active and (row['value'] is not None or row['editable'])):
            raise NativeError('conditional_accessibility_state_mismatch')
        if row['value_provenance'] not in ('accessibility', 'native_attachment_readback', 'host_source_only', 'unavailable', 'injected_state'):
            raise NativeError('unknown_value_provenance')
        if mode != 'INJECTED' and row['value_provenance'] == 'injected_state':
            raise NativeError('injected_observation_cannot_upgrade_provenance')
        if active:
            if field['kind'] != 'attachment' and row['value_provenance'] not in (('accessibility', 'injected_state') if mode == 'INJECTED' else ('accessibility',)):
                raise NativeError('unverified_accessibility_value_provenance')
            actual = row['value']
            if field['kind'] == 'attachment':
                if actual is not None:
                    require_dict(actual, {'name', 'mime_type', 'size', 'sha256'})
                    checked = {**actual, 'sha256': actual['sha256'] if actual['sha256'] is not None else '0' * 64}
                    _value({**field, 'required': False}, checked)
            elif actual is None and field['kind'] in ('select', 'radio'):
                pass
            else:
                _value({**field, 'required': False}, actual)
            prior[field['id']] = actual
    return value


def _readback(snapshot, contract, plan, mode, attachment_declaration):
    expected = expected_readback(contract, plan)
    observed = clone(expected)
    unknown = []
    unverified_fields = []
    for row, actual in zip(observed['fields'], snapshot['fields']):
        row['active'] = actual['active']
        row['value'] = clone(actual['value'])
        if row['active'] and row['kind'] != 'attachment' and actual['value_provenance'] not in (('accessibility', 'injected_state') if mode == 'INJECTED' else ('accessibility',)):
            unverified_fields.append(row['field_id'])
        if row['kind'] == 'attachment' and row['active'] and row['value'] is not None:
            provenance_ok = actual['value_provenance'] == 'native_attachment_readback' or (mode == 'INJECTED' and actual['value_provenance'] == 'injected_state')
            if row['value']['sha256'] is None or not provenance_ok or attachment_declaration != 'sha256':
                unknown.append(row['field_id'])
                row['value']['sha256'] = None
                next(item for item in expected['fields'] if item['field_id'] == row['field_id'])['value']['sha256'] = None
    matches = digest(observed) == digest(expected)
    return {'status': 'MISMATCH' if not matches else 'PARTIAL' if unknown or unverified_fields else 'EXACT_REPORTED_READBACK',
            'unverified_attachment_fields': unknown, 'unverified_fields': unverified_fields + unknown, 'observation_sha256': digest(snapshot),
            'observation_provenance': mode, 'native_browser_independently_verified': False,
            'submission_authorized': False}


class NativeAdapter:
    """Trusted-host boundary. Providers must not be exposed as model tools.

    transport(request) returns our normalized accessibility snapshot or receipt.
    Native permission and Keel human approvals are independent required gates.
    record_halt(event) must durably retain unknown/429/takeover outcomes on the
    actual host. This module reports whether that callback returned, not whether
    its storage is durable. A restarted host must reload its canonical holds.
    """
    def __init__(self, manifest, transport, host_snapshot_provider, native_permission_provider,
                 record_halt, *, clock=None, transport_mode='INJECTED'):
        self.manifest = validate_manifest(manifest)
        if transport_mode not in MODES or not all(callable(x) for x in (transport, host_snapshot_provider, native_permission_provider, record_halt)):
            raise NativeError('trusted_host_callbacks_and_known_transport_mode_required')
        self.transport = transport
        self.host = host_snapshot_provider
        self.permission = native_permission_provider
        self.record_halt = record_halt
        self.clock = clock or (lambda: int(time.time()))
        self.mode = transport_mode
        self.halt_reason = None
        self.used = False

    def prepare(self, contract, values, host_approvals, *, attachments=None):
        """One preparation session per adapter. Never retries or submits."""
        if self.used:
            raise NativeError('adapter_session_exhausted_or_halted')
        self.used = True
        contract = validate_contract(contract)
        attachments = clone(attachments or {})
        result = {'schema': 'keel.muse.native-preparation.v1', 'status': 'BLOCKED',
                  'transport_mode': self.mode, 'synthetic': self.mode == 'INJECTED',
                  'transport_calls': 0, 'action_attempts': 0, 'actions_reported_applied': 0,
                  'native_browser_independently_verified': False, 'native_permission_authenticated': False,
                  'human_approval_authenticated': False, 'durable_halt_storage_verified': False,
                  'execution_authorized': False, 'submission_authorized': False,
                  'real_model_calls': 0, 'automatic_retry': False, 'readback': None,
                  'halt_record_callback_completed': False}
        gateway, grant = None, None
        previous, seen = 0, set()
        def refresh():
            snapshot = self.host()
            gateway.update_snapshot(snapshot)
            gateway._check(self.clock(), grant['plan'] if grant else None)
        def authorize(request):
            permission = clone(self.permission(clone(request)))
            _permission(permission, request, self.clock())
            return permission
        def observe():
            nonlocal previous
            refresh()
            request = _request('accessibility_snapshot', contract)
            permission = authorize(request)
            refresh()
            _permission(permission, request, self.clock())
            result['transport_calls'] += 1
            raw = self.transport(clone(request))
            snapshot = _snapshot(raw, contract, self.clock(), previous=previous, seen=seen, mode=self.mode)
            previous = snapshot['snapshot_revision']
            seen.add(snapshot['snapshot_id'])
            return snapshot
        try:
            if self.manifest['mapping_status'] != 'HOST_MAPPING_CONFIGURED' or not self.manifest['snapshot_action_binding']:
                raise NativeError('native_mapping_or_snapshot_binding_unavailable')
            gateway = ActionGateway(contract, self.host(), host_approvals)
            grant = gateway.issue(values, now=self.clock(), ttl=300)
            if any(action['operation'] not in self.manifest['operations'] for action in grant['actions']):
                raise NativeError('unsupported_native_preparation_verb')
            active_attachments = {action['field_id'] for action in grant['actions'] if action['operation'] == 'attach_file'}
            if type(attachments) is not dict or set(attachments) != active_attachments:
                raise NativeError('exact_approved_attachment_payloads_required')
            for action in grant['actions']:
                if action['operation'] == 'attach_file':
                    encoded = attachments[action['field_id']]
                    if type(encoded) is not str or len(encoded) > 7 * 1024 * 1024:
                        raise NativeError('bounded_canonical_attachment_encoding_required')
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as exc:
                        raise NativeError('invalid_attachment_encoding') from exc
                    if (base64.b64encode(raw).decode() != encoded or len(raw) != action['value']['size'] or
                            hashlib.sha256(raw).hexdigest() != action['value']['sha256']):
                        raise NativeError('attachment_payload_does_not_match_human_approved_hash')
            for action in grant['actions']:
                snapshot = observe()
                target = next(row for row in snapshot['fields'] if row['descriptor']['id'] == action['field_id'])
                if not target['active'] or not target['editable']:
                    raise NativeError('target_inactive_readonly_or_unverified')
                request = _request(action['operation'], contract, snapshot=snapshot, action=action,
                                   attachment=attachments.get(action['field_id']))
                permission = authorize(request)
                refresh()
                gateway.consume(grant['capability'], action, now=self.clock())
                # A slow canonical-state refresh can outlive the separate
                # native grant. Recheck it at the actual transport boundary.
                _permission(permission, request, self.clock())
                result['action_attempts'] += 1
                result['transport_calls'] += 1
                response = clone(self.transport(clone(request)))
                require_dict(response, {'schema', 'request_id', 'request_sha256', 'status', 'snapshot_id',
                                        'snapshot_revision', 'target_ref', 'operation'})
                if (response['schema'] != 'keel.muse.native-receipt.v1' or response['request_id'] != request['request_id'] or
                        response['request_sha256'] != digest(request) or response['snapshot_id'] != request['snapshot_id'] or
                        response['snapshot_revision'] != request['snapshot_revision'] or response['target_ref'] != request['target_ref'] or
                        response['operation'] != request['operation']):
                    raise NativeError('unverified_native_action_receipt')
                if response['status'] != 'applied':
                    if response['status'] not in ('denied', 'ask', 'unknown', 'rate_limited', 'human_takeover'):
                        raise NativeError('unknown_native_action_status')
                    raise NativeError('native_' + response['status'])
                result['actions_reported_applied'] += 1
            final = observe()
            refresh()
            # Revalidate every approval and the full host context after readback.
            verification = ActionGateway(contract, self.host(), host_approvals)
            verification.issue(values, now=self.clock())
            result['readback'] = _readback(final, contract, grant['plan'], self.mode, self.manifest['attachment_readback'])
            if result['readback']['status'] == 'MISMATCH':
                raise NativeError('final_native_readback_mismatch')
            result['status'] = ('PARTIAL' if result['readback']['status'] == 'PARTIAL' else
                                'SIMULATED' if self.mode == 'INJECTED' else 'PREPARED_REPORTED')
            result['plan_sha256'] = digest(grant['plan'])
            result['field_count'] = len(grant['plan']['fields'])
            result['active_field_count'] = len(grant['actions'])
        except Exception as exc:
            # A transport can fail after the effect. Never repeat an attempted action.
            self.halt_reason = str(exc) if isinstance(exc, NativeError) else 'host_or_gate_error_' + type(exc).__name__
            if gateway is not None and grant is not None:
                gateway.revoke(grant['capability']['capability_id'])
            result['status'] = 'UNKNOWN' if result['action_attempts'] > result['actions_reported_applied'] else 'BLOCKED'
            result['reason'] = self.halt_reason
            event = {'schema': 'keel.muse.native-halt.v1', 'reason': self.halt_reason,
                     'unknown_effect': result['action_attempts'] > result['actions_reported_applied'],
                     'action_attempts': result['action_attempts'], 'automatic_retry': False,
                     'contract_sha256': digest(contract), 'execution_authorized': False}
            try:
                self.record_halt(clone(event))
                result['halt_record_callback_completed'] = True
            except Exception:
                result['halt_record_callback_completed'] = False
        return result


class InjectedFixture:
    """Executable in-memory accessibility executor, never an actual Muse browser."""
    def __init__(self, fixture=None, *, attachment_hashes=True):
        self.fixture = clone(fixture or demo_fixture())
        if self.fixture['synthetic'] is not True:
            raise NativeError('synthetic_fixture_required')
        self.contract = self.fixture['contract']
        self.attachment_hashes = attachment_hashes
        self.values = {}
        self.revision = 0
        self.latest = None
        self.calls = []
        self.halts = []
        for field in self.contract['fields']:
            self.values[field['id']] = False if field['kind'] in ('checkbox', 'attestation') else None if field['kind'] in ('select', 'radio', 'attachment') else ''

    def host_snapshot(self):
        return clone(self.fixture['host_snapshot'])

    def permission(self, request):
        return {'schema': 'keel.muse.native-permission.v1', 'permission_id': 'synthetic-native-permission',
                'request_sha256': digest(request), 'decision': 'allow', 'issued_at': self.fixture['now'],
                'expires_at': self.fixture['now'] + 300, 'revoked': False}

    def record_halt(self, event):
        self.halts.append(clone(event))
        if 'rate_limited' in event['reason']:
            self.fixture['host_snapshot']['rate_limited'] = True
        if event['unknown_effect']:
            self.fixture['host_snapshot']['unknown_attempt'] = True

    def __call__(self, request):
        self.calls.append(clone(request))
        if request['operation'] == 'accessibility_snapshot':
            self.revision += 1
            rows, active_values = [], {}
            for field in self.contract['fields']:
                condition = field['condition']
                active = condition is None or (condition['field_id'] in active_values and digest(active_values[condition['field_id']]) == digest(condition['equals']))
                value = clone(self.values[field['id']]) if active else None
                if active:
                    active_values[field['id']] = value
                if value is not None and field['kind'] == 'attachment' and not self.attachment_hashes:
                    value['sha256'] = None
                rows.append({'descriptor': field, 'target_ref': 'ref-' + field['id'], 'active': active,
                             'editable': active, 'value': value, 'value_provenance': 'injected_state'})
            self.latest = {'schema': 'keel.muse.accessibility.v1', 'snapshot_id': 'snapshot-' + str(self.revision),
                           'snapshot_revision': self.revision, 'observed_at': self.fixture['now'],
                           'expires_at': self.fixture['now'] + 300, 'origin': self.contract['origin'],
                           'account_id': self.contract['account_id'], 'form_id': self.contract['form_id'],
                           'contract_sha256': digest(self.contract), 'revisions': self.contract['revisions'],
                           'fields': rows, 'unexpected_controls': [], 'human_takeover': False,
                           'submission_observed': False, 'provenance': 'INJECTED'}
            return clone(self.latest)
        if request['operation'] not in fixture_manifest()['operations']:
            raise NativeError('unsupported_injected_verb')
        if self.latest is None or request['snapshot_id'] != self.latest['snapshot_id'] or request['snapshot_revision'] != self.latest['snapshot_revision']:
            raise NativeError('injected_executor_snapshot_changed')
        row = next(row for row in self.latest['fields'] if row['target_ref'] == request['target_ref'])
        if not row['active'] or request['action']['field_id'] != row['descriptor']['id']:
            raise NativeError('injected_target_scope_mismatch')
        value = clone(request['action']['value'])
        if request['operation'] == 'attach_file':
            raw = base64.b64decode(request['attachment'], validate=True)
            if hashlib.sha256(raw).hexdigest() != value['sha256']:
                raise NativeError('injected_attachment_bytes_mismatch')
        self.values[row['descriptor']['id']] = value
        return {'schema': 'keel.muse.native-receipt.v1', 'request_id': request['request_id'],
                'request_sha256': digest(request), 'status': 'applied', 'snapshot_id': request['snapshot_id'],
                'snapshot_revision': request['snapshot_revision'], 'target_ref': request['target_ref'],
                'operation': request['operation']}


def demo(home):
    home = Path(home).absolute()
    from tools.bench_inventory import directory_fd
    import os
    parent = directory_fd(home.parent)
    try:
        os.mkdir(Path('/proc/self/fd') / str(parent) / home.name, 0o700)
    finally:
        os.close(parent)
    fixture = InjectedFixture()
    adapter = NativeAdapter(fixture_manifest(), fixture, fixture.host_snapshot, fixture.permission,
                            fixture.record_halt, clock=lambda: fixture.fixture['now'])
    result = adapter.prepare(fixture.contract, fixture.fixture['values'], fixture.fixture['approvals'],
                             attachments=fixture.fixture['attachments'])
    report = {'schema': 'keel.muse.native-demo.v1', 'synthetic': True, 'transport_mode': 'INJECTED',
              'preparation': result, 'fixture_request_count': len(fixture.calls), 'real_browser_actions': 0,
              'real_model_calls': 0, 'external_network_calls': 0, 'canonical_writes': 0,
              'execution_authorized': False, 'submission_authorized': False}
    atomic_json(home / 'native-demo.json', report)
    return report
