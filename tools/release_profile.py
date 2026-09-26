"""Offline capability inventory for the local agent ecosystem source profile.

Presence, configuration, and verified live operation are separate claims. This
module never probes a network, opens a workspace database, or loads a plugin.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import platform
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def _configured_sources(workspace):
    from engines.safe_io import read_json
    config = read_json(Path(workspace) / 'data/sources.json',
                       missing={'schema_version': 1, 'sources': []})
    if (not isinstance(config, dict) or not isinstance(config.get('sources'), list)
            or len(config['sources']) > 100):
        raise ValueError('invalid source registry')
    seen, enabled = set(), 0
    for item in config['sources']:
        ref = item.get('ref') if isinstance(item, dict) else None
        if (not isinstance(ref, str)
                or re.fullmatch(r'(greenhouse|lever|lever_eu|ashby):[A-Za-z0-9_-]{1,160}', ref) is None
                or ref in seen or type(item.get('enabled', False)) is not bool):
            raise ValueError('invalid or duplicate source configuration')
        seen.add(ref)
        enabled += item.get('enabled') is True
    return enabled


def capability_report(workspace, code_root=ROOT):
    """Report observed local prerequisites and explicitly disconnected services."""
    root = Path(code_root)
    system = platform.system()
    python_ok = sys.version_info >= (3, 11)
    posix = os.name == 'posix'
    descriptors = all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW'))
    descriptors = descriptors and os.open in os.supports_dir_fd
    locking = importlib.util.find_spec('fcntl') is not None
    sqlite = {'available': False, 'in_memory_transaction_verified': False, 'fts5_verified': False}
    try:
        import sqlite3
        with sqlite3.connect(':memory:') as connection:
            connection.execute('CREATE TABLE capability_probe (value INTEGER)')
            connection.execute('INSERT INTO capability_probe VALUES (1)')
            connection.rollback()
            sqlite = {'available': True, 'version': sqlite3.sqlite_version,
                      'in_memory_transaction_verified':
                      connection.execute('SELECT COUNT(*) FROM capability_probe').fetchone()[0] == 0,
                      'fts5_verified': False}
            try:
                connection.execute('CREATE VIRTUAL TABLE text_probe USING fts5(value)')
                connection.execute("INSERT INTO text_probe VALUES ('synthetic')")
                sqlite['fts5_verified'] = connection.execute(
                    "SELECT COUNT(*) FROM text_probe WHERE text_probe MATCH 'synthetic'").fetchone()[0] == 1
            except sqlite3.Error:
                pass
    except (ImportError, OSError, RuntimeError) as exc:
        sqlite['error_type'] = type(exc).__name__
    except sqlite3.Error as exc:
        sqlite['error_type'] = type(exc).__name__
    runtime_ok = python_ok and posix and descriptors and locking and sqlite['available']
    # Linux includes WSL. macOS has the primitives but is not release-qualified.
    runtime_status = 'SUPPORTED' if runtime_ok and system == 'Linux' else (
        'UNVALIDATED_PLATFORM' if runtime_ok else 'UNSUPPORTED')

    source_report = {'status': 'UNCONFIGURED', 'enabled_sources': 0,
                     'network_checked': False, 'credentials_required': False}
    try:
        count = _configured_sources(workspace)
        source_report['enabled_sources'] = count
        source_report['status'] = 'CONFIGURED_NOT_PROBED' if count else 'UNCONFIGURED'
    except (OSError, ValueError, TypeError, KeyError) as exc:
        source_report.update(status='INVALID_CONFIGURATION', error_type=type(exc).__name__)

    def present(*paths):
        return all((root / path).is_file() for path in paths)

    modules = {
        'local_preparation': present('engines/pipeline_service.py', 'engines/packet_contract.py'),
        'profile_scoring': present('engines/score_roles.py'),
        'decision_sessions': present('keel_loki/decision_sessions.py'),
        'source_feedback': present('engines/source_feedback.py'),
        'authenticated_observations': present('keel_observability/store.py', 'keel_observability/adapters.py'),
        'canonical_identity_index': present('engines/dedupe_index.py'),
        'reliability_laboratory': present('keel_eval/reliability.py'),
        'rendered_browser_qualification': present('keel_loki/browser_lab.py', 'keel_loki/playwright_adapter.py',
                                                 'keel_loki/fixtures/browser_lab.html'),
        'persistent_evidence_index': present('keel_memory/index.py', 'keel_loki/temporal.py'),
        'worker_resource_enforcement': present('security/execution/resource_limits.py',
                                               'security/execution/seccomp_profile.py'),
        'implementation_trace_checks': present('keel_eval/trace_conformance.py', 'security/execution/durable.py'),
        'statistical_improvement_controller': present('keel_learning/controller.py', 'keel_learning/reliability.py'),
        'integrated_synthetic_demo': present('keel_next/__main__.py'),
        'machine_computation_graph': present('keel_machine/graph.py', 'keel_machine/builtins.py',
                                             'keel_machine/adapters.py'),
        'machine_computation_cache': present('keel_machine/cache.py', 'keel_machine/common.py'),
        'machine_contract_sentry': present('keel_machine/contracts.py', 'keel_machine/drift.py'),
        'local_machine_runtime': present('keel_machine/runtime.py', 'keel_muse/coordinator.py',
                                         'keel_agent/state.py'),
        'offline_pipeline_benchmark': present('keel_next/benchmark.py'),
        'guarded_evolution_engine': present('keel_evolve/core.py', 'keel_evolve/demo.py',
                                            'keel_eval/reliability.py'),
    }
    return {
        'schema_version': 1,
        'profile': 'local-agent-ecosystem',
        'runtime': {'status': runtime_status, 'operating_system': system,
                    'supported_platform': 'Linux / WSL with Python 3.11+',
                    'qualified_platform': 'Linux with Python 3.12 in this audit',
                    'python': platform.python_version(), 'python_supported': python_ok,
                    'posix_file_locking': locking and posix,
                    'descriptor_relative_no_follow_io': descriptors, 'sqlite': sqlite,
                    'runtime_dependencies': 'Python standard library for local core; optional local Playwright/Chromium for rendered tests'},
        'modules_present': modules,
        'public_board_discovery': source_report,
        'evidence_authentication': {'status': 'HOST_VALIDATOR_REQUIRED', 'producer_identity_verified': False},
        'persistent_search': {'status': 'AVAILABLE_NOT_CONFIGURED' if sqlite.get('fts5_verified') else 'FTS5_UNAVAILABLE',
                              'databases_opened': False},
        'rendered_browser': {'status': 'NOT_QUALIFIED' if importlib.util.find_spec('playwright') else 'OPTIONAL_DEPENDENCY_MISSING',
                             'playwright_installed': importlib.util.find_spec('playwright') is not None,
                             'chromium_launched': False, 'rendered_execution_verified': False},
        'worker_limits': {'status': 'HOST_CONFIGURATION_REQUIRED', 'enforced': False,
                          'requirements': ['Linux delegated cgroup v2', 'bubblewrap', 'reviewed x86_64 seccomp profile',
                                           'single-threaded supervisor']},
        'statistical_evidence': {'status': 'HOST_AUTHENTICATED_MEASUREMENTS_REQUIRED', 'improvement_proven': False},
        'machine_optimization': {'status': 'REVIEWED_HOST_OPERATIONS_REQUIRED',
                                 'runtime_dependencies': 'Python standard library',
                                 'production_integrated': False, 'improvement_measured': False,
                                 'live_authority_cacheable': False, 'external_actions': False},
        'local_machine_runtime': {'status': 'AVAILABLE_NOT_OPENED' if modules['local_machine_runtime'] else 'MODULE_MISSING',
                                  'fixed_reviewed_operations_only': True, 'databases_opened': False,
                                  'external_action_admission': False, 'live_integration_verified': False},
        'pipeline_benchmark': {'status': 'AVAILABLE_NOT_RUN' if modules['offline_pipeline_benchmark'] else 'MODULE_MISSING',
                               'offline_synthetic_inputs': True, 'production_throughput_measured': False},
        'provider_receipt_verification': {'status': 'NOT_CONNECTED', 'verified': False},
        'mail_connector': {'status': 'NOT_CONNECTED', 'configured': False},
        'application_submission': {'status': 'NOT_EXPOSED_BY_LOCAL_CLI', 'authorized': False},
        'language_model': {'status': 'NOT_REQUIRED', 'paid_service_required': False},
        'publication_authorized': False,
        'scope': 'Offline inventory only. Module presence is not integration or release validation.',
    }
