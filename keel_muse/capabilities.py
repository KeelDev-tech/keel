"""Observed local capabilities and explicitly declared native adapter mappings.

This is Keel's normalization protocol, not a public Muse SDK. A JSON manifest
cannot authenticate Sentinel, a tool identity, a browser execution or a person.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import platform
import sys

from keel_loki.common import clone, digest, require_dict, require_id, LokiError

OPERATIONS = {'accessibility_snapshot', 'set_text', 'select_option', 'choose_radio',
              'set_checkbox', 'set_attestation', 'attach_file'}


def validate_manifest(value):
    value = clone(value)
    require_dict(value, {'schema', 'adapter_id', 'protocol_version', 'mapping_status', 'operations',
                         'native_tool_names', 'snapshot_action_binding', 'attachment_readback'})
    if value['schema'] != 'keel.muse.adapter-manifest.v1' or type(value['protocol_version']) is not int or value['protocol_version'] != 1:
        raise LokiError('unsupported Keel normalized adapter protocol')
    require_id(value['adapter_id'])
    if value['mapping_status'] not in ('HOST_MAPPING_REQUIRED', 'HOST_MAPPING_CONFIGURED'):
        raise LokiError('invalid host mapping status')
    operations = value['operations']
    if type(operations) is not list or not operations or len(operations) > len(OPERATIONS):
        raise LokiError('bounded declared operations required')
    if any(type(op) is not str or op not in OPERATIONS for op in operations) or len(set(operations)) != len(operations):
        raise LokiError('unsupported or duplicate native operation')
    if 'accessibility_snapshot' not in operations:
        raise LokiError('accessibility snapshots are required')
    names = value['native_tool_names']
    if type(names) is not dict or set(names) != set(operations):
        raise LokiError('exact operation-to-host-tool mapping required')
    for name in names.values():
        if type(name) is not str or not name or len(name) > 256 or any(ord(c) < 32 for c in name):
            raise LokiError('bounded opaque host tool names required')
    if type(value['snapshot_action_binding']) is not bool:
        raise LokiError('snapshot binding declaration must be boolean')
    if value['attachment_readback'] not in ('sha256', 'metadata_only', 'unavailable'):
        raise LokiError('unsupported attachment observation declaration')
    return value


def probe_host(manifest=None):
    """Read-only in-process/filesystem probe; no subprocess, model or native calls.

    Finding Python or a package does not prove process execution is allowed.
    A configured native manifest remains HOST_DECLARED until its real host maps
    and exercises the actual tools. No environment values or credentials output.
    """
    manifest = validate_manifest(manifest) if manifest is not None else None
    executable = Path(sys.executable)
    executable_found = executable.is_file()
    try:
        playwright_present = importlib.util.find_spec('playwright') is not None
    except (ValueError, ModuleNotFoundError):
        playwright_present = False
    return {'schema': 'keel.muse.capabilities.v1',
            'python_runtime': {'status': 'PROBED', 'method': 'current_process_runtime',
                               'implementation': platform.python_implementation(),
                               'version': list(sys.version_info[:3]), 'subprocess_execution_tested': False},
            'python_executable': {'status': 'FILESYSTEM_OBSERVED' if executable_found else 'UNAVAILABLE',
                                  'path': str(executable), 'executable_bit': executable_found and os.access(executable, os.X_OK),
                                  'process_launch_tested': False},
            'playwright_package': {'status': 'FILESYSTEM_OBSERVED' if playwright_present else 'UNAVAILABLE',
                                   'required_for_native_adapter': False, 'browser_rendering_tested': False},
            'native_adapter': {'status': 'HOST_DECLARED' if manifest else 'UNAVAILABLE',
                               'manifest_sha256': digest(manifest) if manifest else None,
                               'mapping_status': manifest['mapping_status'] if manifest else 'HOST_MAPPING_REQUIRED',
                               'tool_calls_probed': False, 'sentinel_authenticated': False,
                               'official_muse_api_schema_available': False},
            'external_model_inference': 'NOT_RUN', 'native_browser_execution': 'NOT_RUN',
            'process_calls': 0, 'native_tool_calls': 0, 'network_calls': 0,
            'execution_authorized': False, 'submission_authorized': False,
            'os_containment_verified': False}


def fixture_manifest(*, attachment_readback='sha256'):
    """Our injected test protocol. Names do not represent real Muse tool names."""
    operations = sorted(OPERATIONS)
    return {'schema': 'keel.muse.adapter-manifest.v1', 'adapter_id': 'injected-fixture', 'protocol_version': 1,
            'mapping_status': 'HOST_MAPPING_CONFIGURED', 'operations': operations,
            'native_tool_names': {op: 'fixture.' + op for op in operations},
            'snapshot_action_binding': True, 'attachment_readback': attachment_readback}
