"""Measured operational rehearsal with private synthetic state only."""
from pathlib import Path

from .common import FLAGS, atomic_json, digest, new_home, outside_source


def run_demo(home, *, source_sha256):
    from . import control, fixture, qualification, recovery, runtime
    home = new_home(outside_source(home, Path(__file__).resolve().parents[1]))
    inputs = qualification.fixture_inputs()
    inputs['source_sha256'] = source_sha256
    plan = qualification.freeze_plan(**inputs)
    components = {
        'qualification': qualification.demo(home / 'qualification', source_sha256=source_sha256),
        'control': control.demo(home / 'control'),
        'runtime': runtime.demo(home / 'runtime', source_sha256=source_sha256),
        'recovery': recovery.demo(home / 'recovery'),
        'fixture_export': fixture.export_fixture(home / 'fixture', plan, expected_plan_sha256=digest(plan)),
    }
    report = {'schema': 'keel.operational.demo.v1', 'synthetic': True,
              'status': 'PASS' if all(components[k]['status'] == 'PASS' for k in
                                    ('qualification', 'control', 'runtime', 'recovery')) else 'FAIL',
              'components': components, 'source_sha256': source_sha256,
              'real_model_calls': 0, 'real_native_browser_actions': 0,
              'real_canonical_writes': 0, 'external_network_calls': 0,
              'competitive_superiority_established': False, **FLAGS}
    atomic_json(home / 'demo.json', report)
    return report
