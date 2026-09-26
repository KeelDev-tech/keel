"""Cross-module regressions for scoped memory and CLI failure propagation."""
import copy
from dataclasses import asdict
import json

import pytest

from keel_loki import __main__ as cli
from keel_loki.forms import bind_fixture, demo_fixture, digest
from keel_loki.integration import compile_grounded_plan
from keel_loki.modelcheck import INITIAL, machine_sha256
from keel_loki.temporal import TemporalMemory


@pytest.fixture
def bound(tmp_path):
    fixture = demo_fixture()
    value = fixture['values']['motivation']['value']
    source_hash = fixture['values']['motivation']['evidence_sha256'][0]
    claim = {'revision_id': 'motivation-v1', 'subject_id': 'synthetic-candidate', 'key': 'motivation',
             'value': value, 'source': {'source_id': 'fixture-source', 'sha256': source_hash,
             'classification': 'personal', 'allowed_scopes': [fixture['contract']['form_id']],
             'permitted_uses': ['application_fact']}, 'scope': fixture['contract']['form_id'],
             'permitted_uses': ['application_fact'], 'valid_from': 1000, 'valid_until': None, 'supersedes': []}
    with TemporalMemory(tmp_path/'memory', clock=lambda: 1000) as memory:
        memory.record_claim(claim, 'claim-v1')
        memory.record_observation({'observation_id': 'observation-v1', 'revision_id': 'motivation-v1',
                                  'reviewer_id': 'synthetic-reviewer', 'verdict': 'verified',
                                  'source_sha256': source_hash, 'observed_at': 1000}, 'observation-v1')
        bindings = {'motivation': {'subject_id': 'synthetic-candidate', 'key': 'motivation',
                                  'scope': fixture['contract']['form_id'], 'revision_id': 'motivation-v1'}}
        yield memory, fixture, claim, bindings


def compile_bound(bound):
    memory, fixture, _, bindings = bound
    return compile_grounded_plan(memory, fixture, bindings,
                                 expected_memory_head=memory.verify()['head_sha256'], now=1000)


def test_cross_bound_plan_does_not_authenticate_human_or_source(bound):
    report = compile_bound(bound)
    assert report['status'] == 'PREPARATION_PLAN_BOUND'
    assert report['human_approval_authenticated'] is False
    assert report['source_truth_authenticated'] is False
    assert report['all_material_claims_enumerated'] is False
    assert report['execution_authorized'] is False
    assert report['submission_authorized'] is False


@pytest.mark.parametrize('change', ['missing_approval', 'model_actor', 'mismatched_source', 'stale_revision', 'wrong_scope', 'attestation'])
def test_cross_memory_cannot_supply_missing_approval_or_other_scope(bound, change):
    memory, fixture, _, bindings = bound
    if change == 'missing_approval':
        fixture['approvals'] = []
    elif change == 'model_actor':
        fixture['approvals'][0]['actor_kind'] = 'model'
    elif change == 'mismatched_source':
        fixture['values']['motivation']['evidence_sha256'] = ['0'*64]
    elif change == 'stale_revision':
        bindings['motivation']['revision_id'] = 'unrecorded-v2'
    elif change == 'wrong_scope':
        bindings['motivation']['scope'] = 'another-role'
    else:
        bindings['accuracy'] = bindings.pop('motivation')
    with pytest.raises(ValueError):
        compile_bound(bound)


def test_cross_stale_expected_head_rejected(bound):
    memory, fixture, claim, bindings = bound
    old_head = memory.verify()['head_sha256']
    correction = copy.deepcopy(claim)
    correction.update(revision_id='motivation-v2', value='Changed statement', supersedes=['motivation-v1'])
    memory.record_claim(correction, 'claim-v2')
    with pytest.raises(ValueError):
        compile_grounded_plan(memory, fixture, bindings, expected_memory_head=old_head, now=1000)


def test_cross_changed_memory_during_compilation_rejected(bound, monkeypatch):
    memory, fixture, claim, bindings = bound
    original = memory.resolve
    def resolve_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        correction = copy.deepcopy(claim)
        correction.update(revision_id='motivation-v2', value='Concurrent correction', supersedes=['motivation-v1'])
        memory.record_claim(correction, 'claim-v2')
        return result
    monkeypatch.setattr(memory, 'resolve', resolve_then_change)
    with pytest.raises(ValueError):
        compile_bound(bound)


def test_cross_verified_numeric_fact_cannot_fill_text_field(bound):
    memory, fixture, claim, bindings = bound
    correction = copy.deepcopy(claim)
    correction.update(revision_id='motivation-v2', value=42, supersedes=['motivation-v1'])
    correction['source']['sha256'] = digest('numeric fixture source')
    memory.record_claim(correction, 'claim-v2')
    memory.record_observation({'observation_id':'observation-v2','revision_id':'motivation-v2',
        'reviewer_id':'synthetic-reviewer','verdict':'verified','source_sha256':correction['source']['sha256'],
        'observed_at':1000}, 'observation-v2')
    fixture['plain_values']['motivation'] = 42
    updated = bind_fixture(fixture, now=1000)
    fixture.clear(); fixture.update(updated)
    fixture['values']['motivation']['evidence_sha256'] = [correction['source']['sha256']]
    for approval in fixture['approvals']:
        if approval['field_id'] == 'motivation':
            approval['evidence_sha256'] = [correction['source']['sha256']]
    bindings['motivation']['revision_id'] = 'motivation-v2'
    with pytest.raises(ValueError):
        compile_bound(bound)


@pytest.fixture
def cli_inventory(monkeypatch):
    import tools.loki_inventory
    monkeypatch.setattr(tools.loki_inventory, 'inventory', lambda: {'sha256':'a'*64,'files':{}})


def test_cross_cli_nonconforming_trace_returns_failure(tmp_path, cli_inventory):
    trace = {'schema':'keel.loki.trace.v1','machine_sha256':machine_sha256(),
             'initial':asdict(INITIAL),'laboratory_mutant':None,
             'steps':[{'action':'finish','state':asdict(INITIAL)}]}
    source = tmp_path/'trace.json'; out = tmp_path/'result.json'
    source.write_text(json.dumps({'operation':'replay_trace','arguments':{'trace':trace}}))
    assert cli.main(['trace-check','--input',str(source),'--out',str(out)]) == 3
    assert json.loads(out.read_text())['result']['status'] == 'NONCONFORMING'


@pytest.mark.parametrize('status', ['COUNTEREXAMPLE','ERROR'])
def test_cross_cli_formal_counterexample_and_errors_are_not_success(tmp_path, cli_inventory, monkeypatch, status):
    monkeypatch.setattr(cli, 'dispatch', lambda *_: {'status':status,'execution_authorized':False})
    assert cli.main(['modelcheck','--out',str(tmp_path/'result.json')]) == 3


def test_cross_cli_truncated_formal_check_returns_failure(tmp_path, cli_inventory):
    out = tmp_path/'result.json'
    assert cli.main(['modelcheck','--max-states','1','--out',str(out)]) == 3
    result = json.loads(out.read_text())['result']
    assert result['status'] == 'INCOMPLETE' and result['exploration_complete'] is False


def test_cross_cli_missing_browser_remains_unavailable(tmp_path, cli_inventory, monkeypatch):
    import keel_loki.browser as browser
    monkeypatch.setattr(browser.importlib.util, 'find_spec', lambda _: None)
    out = tmp_path/'result.json'
    assert cli.main(['browser-trial','--render-browser','--home',str(tmp_path/'trial'),'--out',str(out)]) == 3
    result = json.loads(out.read_text())['result']
    assert result['rendered_browser']['status'] == 'UNAVAILABLE'
    assert result['whole_fixture_prepared'] is False
    assert result['fixture_server_started'] is False


@pytest.mark.parametrize('command,operation', [('route','run_route'),('lab','run_lab')])
@pytest.mark.parametrize('injected_key', ['allow_model_calls','transport'])
def test_cross_json_cannot_opt_into_inference_or_supply_transport(tmp_path, cli_inventory, monkeypatch, command, operation, injected_key):
    from keel_loki import lab, routing
    def forbidden(**_):
        pytest.fail('untrusted inference request must not reach runtime')
    monkeypatch.setattr(lab, 'run_lab', forbidden)
    monkeypatch.setattr(routing, 'run_route', forbidden)
    source = tmp_path/'request.json'
    source.write_text(json.dumps({'operation':operation,'arguments':{injected_key:True}}))
    assert cli.main([command,'--input',str(source),'--out',str(tmp_path/'out.json')]) == 2
