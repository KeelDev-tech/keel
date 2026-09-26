import copy
import pytest
from keel_loki.common import LokiError
from keel_muse.capabilities import probe_host, fixture_manifest, validate_manifest


def test_local_python_observation_is_not_native_browser_or_process_proof():
    report = probe_host()
    assert report['python_runtime']['status'] == 'PROBED'
    assert report['python_executable']['status'] == 'FILESYSTEM_OBSERVED'
    assert report['python_executable']['process_launch_tested'] is False
    assert report['native_adapter']['status'] == 'UNAVAILABLE'
    assert report['native_browser_execution'] == 'NOT_RUN'
    assert report['process_calls'] == report['network_calls'] == report['native_tool_calls'] == 0
    assert not report['execution_authorized']


def test_manifest_is_a_declaration_not_a_probe_or_authentication():
    report = probe_host(fixture_manifest())
    assert report['native_adapter']['status'] == 'HOST_DECLARED'
    assert not report['native_adapter']['tool_calls_probed']
    assert not report['native_adapter']['sentinel_authenticated']
    assert not report['native_adapter']['official_muse_api_schema_available']


@pytest.mark.parametrize('operation', ['submit', 'click', 'evaluate_javascript', 'cdp', 'shell', 'navigate'])
def test_unrestricted_verbs_cannot_be_declared(operation):
    manifest = fixture_manifest()
    manifest['operations'].append(operation)
    manifest['native_tool_names'][operation] = operation
    with pytest.raises(LokiError):
        validate_manifest(manifest)


@pytest.mark.parametrize('change', ['missing_snapshot', 'extra_mapping', 'boolean_version', 'duplicate', 'injected_signature'])
def test_manifest_schema_does_not_accept_implied_capabilities(change):
    manifest = fixture_manifest()
    if change == 'missing_snapshot':
        manifest['operations'].remove('accessibility_snapshot')
        del manifest['native_tool_names']['accessibility_snapshot']
    elif change == 'extra_mapping':
        manifest['native_tool_names']['submit'] = 'anything'
    elif change == 'boolean_version':
        manifest['protocol_version'] = True
    elif change == 'duplicate':
        manifest['operations'].append('set_text')
    else:
        manifest['signature'] = 'self-authenticated'
    with pytest.raises(LokiError):
        validate_manifest(manifest)
