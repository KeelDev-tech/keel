"""Bounded read-only tool requests with pinned code, state and typed results.

This bridge intentionally admits planning and inspection only. Effectful work
goes through the durable Coordinator and native adapter APIs, selected by the
trusted embedding host. No model-supplied code, handler or transport is loaded.
The outer RECORDED status means a result was recorded, not that its task passed.
"""
from dataclasses import dataclass
import time

from .common import (MuseError, canonical, clone, digest, require_dict,
                     require_hash, require_id, require_int)

REQUEST_FIELDS = {'schema', 'request_id', 'workspace_id', 'operation',
                  'expected_source_sha256', 'expected_state_sha256',
                  'expires_at', 'arguments'}
MAX_REQUEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ReadOperation:
    handler: object
    state_required: bool = False

    def __post_init__(self):
        if not callable(self.handler) or type(self.state_required) is not bool:
            raise MuseError('invalid_trusted_operation')


class CommandBridge:
    """A host-created registry of pure planners and observations.

    Handlers are trusted Python callables. The read-only declaration is a
    contract, not a Python or OS sandbox. Fresh state is never a cached prior
    outcome. Admission and completion deadlines are checked; this class cannot
    forcibly preempt a handler. The host must bound its individual tools.
    """

    def __init__(self, workspace_id, operations, *, source_provider,
                 state_provider=None, clock=None):
        require_id(workspace_id)
        if (type(operations) is not dict or not operations or len(operations) > 64
                or not callable(source_provider)
                or state_provider is not None and not callable(state_provider)):
            raise MuseError('invalid_trusted_bridge_configuration')
        for name, operation in operations.items():
            require_id(name)
            if not isinstance(operation, ReadOperation):
                raise MuseError('read_operation_required')
        self.workspace_id = workspace_id
        self.operations = dict(operations)
        self.source_provider = source_provider
        self.state_provider = state_provider
        self.clock = clock or (lambda: int(time.time()))

    def _source(self):
        value = self.source_provider()
        require_hash(value)
        return value

    def _state(self, needed):
        if not needed:
            return None
        if self.state_provider is None:
            raise MuseError('trusted_state_provider_required')
        value = clone(self.state_provider())
        if type(value) is not dict or value.get('workspace_id') != self.workspace_id:
            raise MuseError('host_state_workspace_mismatch')
        return digest(value)

    def invoke(self, request):
        request = clone(request)
        if len(canonical(request)) > MAX_REQUEST_BYTES:
            raise MuseError('bridge_request_too_large')
        require_dict(request, REQUEST_FIELDS)
        if request['schema'] != 'keel.muse.bridge-request.v1':
            raise MuseError('unsupported_bridge_request')
        for key in ('request_id', 'workspace_id', 'operation'):
            require_id(request[key])
        require_hash(request['expected_source_sha256'])
        require_int(request['expires_at'])
        if request['workspace_id'] != self.workspace_id or request['operation'] not in self.operations:
            raise MuseError('bridge_scope_or_operation_invalid')
        if type(request['arguments']) is not dict:
            raise MuseError('bridge_arguments_object_required')
        operation = self.operations[request['operation']]
        if operation.state_required:
            require_hash(request['expected_state_sha256'])
        elif request['expected_state_sha256'] is not None:
            raise MuseError('unexpected_state_pin')
        now = self.clock()
        require_int(now)
        if not now < request['expires_at'] <= now + 3600:
            raise MuseError('bridge_request_expired_or_unbounded')
        before = self._source()
        if before != request['expected_source_sha256']:
            raise MuseError('running_source_changed')
        state_before = self._state(operation.state_required)
        if state_before != request['expected_state_sha256']:
            raise MuseError('host_state_changed')
        admitted = self.clock()
        require_int(admitted)
        if admitted < now or admitted >= request['expires_at']:
            raise MuseError('bridge_admission_expired_or_clock_regressed')
        started = time.monotonic()
        # Clone both ways: a handler cannot modify the request pinned below,
        # and executable/nonfinite result objects cannot enter a JSON report.
        result = clone(operation.handler(clone(request['arguments'])))
        after = self._source()
        state_after = self._state(operation.state_required)
        finished = self.clock()
        require_int(finished)
        if finished < admitted:
            raise MuseError('host_clock_regressed')
        reasons = []
        if after != before:
            reasons.append('source_changed_during_operation')
        if state_after != state_before:
            reasons.append('host_state_changed_during_operation')
        if finished >= request['expires_at']:
            reasons.append('completion_deadline_exceeded')
        return {'schema': 'keel.muse.bridge-result.v1',
                'status': 'QUARANTINED' if reasons else 'RECORDED',
                'request_id': request['request_id'], 'request_sha256': digest(request),
                'operation': request['operation'], 'workspace_id': self.workspace_id,
                'source_sha256': before, 'source_unchanged': before == after,
                'state_sha256': state_before, 'state_unchanged': state_before == state_after,
                'quarantine_reasons': reasons, 'result': result,
                'result_sha256': digest(result),
                'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                'deadline_enforcement': 'ADMISSION_AND_COMPLETION_NOT_PREEMPTION',
                'operation_mode': 'READ_ONLY_CONTRACT', 'read_only_os_enforced': False,
                'task_success_inferred': False, 'execution_authorized': False,
                'production_deployed': False, 'account_muse_integration': 'NOT_VERIFIED'}


def demo():
    """Pinned in-process exchange, including stale state quarantine."""
    source = digest('synthetic-source')
    state = {'workspace_id': 'fixture-workspace', 'revision': 1}
    def inspect(_):
        return {'status': 'BLOCKED', 'reason': 'authentic_approval_absent'}
    bridge = CommandBridge('fixture-workspace', {'inspect': ReadOperation(inspect, True)},
                           source_provider=lambda: source, state_provider=lambda: state,
                           clock=lambda: 100)
    request = {'schema': 'keel.muse.bridge-request.v1', 'request_id': 'fixture-read',
               'workspace_id': 'fixture-workspace', 'operation': 'inspect',
               'expected_source_sha256': source, 'expected_state_sha256': digest(state),
               'expires_at': 200, 'arguments': {}}
    report = bridge.invoke(request)
    state['revision'] = 2
    rejected = False
    try:
        bridge.invoke(request)
    except MuseError:
        rejected = True
    return {'schema': 'keel.muse.bridge-demo.v1', 'synthetic': True,
            'recorded_blocker': report, 'stale_state_rejected': rejected,
            'real_model_calls': 0, 'execution_authorized': False}
