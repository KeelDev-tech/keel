import json
from pathlib import Path
import pytest
from keel_loki import __main__ as cli
from keel_loki.common import load_json
from keel_loki.forms import demo_fixture


@pytest.fixture(autouse=True)
def frozen_inventory(monkeypatch):
    import tools.loki_inventory
    monkeypatch.setattr(tools.loki_inventory,'inventory',lambda:{'sha256':'a'*64,'files':{}})


def test_existing_output_prevents_dispatch(tmp_path,monkeypatch):
    p=tmp_path/'out.json';p.write_text('original')
    monkeypatch.setattr(cli,'dispatch',lambda *_:pytest.fail('must not execute'))
    assert cli.main(['inspect-host','--out',str(p)])==2
    assert p.read_text()=='original'


def test_real_form_plan_cli_preserves_authority_boundary(tmp_path):
    f=demo_fixture();p=tmp_path/'input.json';out=tmp_path/'output.json'
    p.write_text(json.dumps({k:f[k] for k in ('contract','values','approvals','now')}))
    assert cli.main(['forms-plan','--input',str(p),'--out',str(out)])==0
    r=load_json(out)
    assert r['schema']=='keel.loki.command.v1' and r['source_unchanged']
    assert len(r['result']['fields'])==12 and not r['result']['execution_authorized']


def test_changed_source_quarantines_result(tmp_path,monkeypatch):
    import tools.loki_inventory
    values=iter([{'sha256':'a'*64},{'sha256':'b'*64}])
    monkeypatch.setattr(tools.loki_inventory,'inventory',lambda:next(values))
    monkeypatch.setattr(cli,'dispatch',lambda *_:{'status':'PASS'})
    p=tmp_path/'result.json'
    assert cli.main(['inspect-host','--out',str(p)])==4
    assert load_json(p)['schema']=='keel.loki.invalid-command.v1'


def test_model_calls_default_disabled_and_input_cannot_override(tmp_path,monkeypatch):
    p=tmp_path/'input.json';p.write_text(json.dumps({'operation':'run_lab','arguments':{'allow_model_calls':True}}))
    assert cli.main(['lab','--input',str(p),'--out',str(tmp_path/'out.json')])==2
    observed=[]
    def dispatch(args,_):observed.append(args.allow_model_calls);return {'status':'DISABLED'}
    monkeypatch.setattr(cli,'dispatch',dispatch)
    assert cli.main(['lab','--input',str(p),'--out',str(tmp_path/'ok.json')])==0
    assert observed==[False]


def test_runtime_output_cannot_be_in_source(monkeypatch):
    monkeypatch.setattr(cli,'dispatch',lambda *_:pytest.fail('must not execute'))
    assert cli.main(['inspect-host','--out',str(cli.ROOT/'forbidden_runtime.json')])==2


def test_recovery_controller_transitions_not_exposed_as_separate_commands(tmp_path):
    p=tmp_path/'input.json';p.write_text(json.dumps({'operation':'claim','arguments':{}}))
    assert cli.main(['recovery','--input',str(p),'--home',str(tmp_path/'state'),'--workspace-id','x','--out',str(tmp_path/'out.json')])==2
    assert not (tmp_path/'state').exists()
