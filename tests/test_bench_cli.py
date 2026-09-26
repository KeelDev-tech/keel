from copy import deepcopy
import json
from pathlib import Path
import stat
import pytest
from keel_bench import __main__ as cli
from keel_bench.experiment import plan_digest
from test_bench_experiment import dataset,systems,SOURCE


def files(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,'inventory',lambda:{'sha256':SOURCE})
    d,s,p=tmp_path/'dataset.json',tmp_path/'systems.json',tmp_path/'plan.json'
    d.write_text(json.dumps(dataset()));s.write_text(json.dumps(systems()))
    assert cli.main(['plan','--dataset',str(d),'--systems',str(s),'--experiment-id','test',
                     '--out',str(p)])==0
    return d,s,p


def run_args(d,p,out):
    return ['run','--dataset',str(d),'--plan',str(p),'--expected-plan-sha256',plan_digest(json.loads(p.read_text())),
            '--out',str(out)]


def test_plan_and_default_run_are_private_and_explicitly_unmeasured(tmp_path,monkeypatch):
    d,s,p=files(tmp_path,monkeypatch);out=tmp_path/'run.json'
    assert cli.main(run_args(d,p,out))==3
    r=json.loads(out.read_text())
    assert r['mode']=='BASELINE_ONLY' and r['model_calls_attempted']==0
    assert stat.S_IMODE(out.stat().st_mode)==0o600
    compared=tmp_path/'comparison.json'
    assert cli.main(['compare','--dataset',str(d),'--plan',str(p),'--run',str(out),
        '--expected-plan-sha256',plan_digest(r['plan']),'--out',str(compared)])==0
    assert not json.loads(compared.read_text())['state_of_the_art_established']


def test_existing_output_refused_before_any_run(tmp_path,monkeypatch):
    d,s,p=files(tmp_path,monkeypatch);out=tmp_path/'run.json';out.write_text('KEEP')
    def forbidden(*a,**kw):raise AssertionError('must not run')
    monkeypatch.setattr(cli,'run_experiment',forbidden)
    assert cli.main(run_args(d,p,out))==2 and out.read_text()=='KEEP'


def test_changed_source_quarantines_results_instead_of_claiming_bound_run(tmp_path,monkeypatch):
    d,s,p=files(tmp_path,monkeypatch);out=tmp_path/'run.json'
    answers=iter([{'sha256':SOURCE},{'sha256':'b'*64}])
    monkeypatch.setattr(cli,'inventory',lambda:next(answers))
    assert cli.main(run_args(d,p,out))==4
    r=json.loads(out.read_text())
    assert r['status']=='INVALID' and r['reason']=='source_changed' and not r['execution_authorized']


def test_missing_or_replaced_plan_pin_cannot_start(tmp_path,monkeypatch):
    d,s,p=files(tmp_path,monkeypatch);out=tmp_path/'run.json'
    args=run_args(d,p,out);args[args.index('--expected-plan-sha256')+1]='c'*64
    assert cli.main(args)==2 and not out.exists()


def test_duplicate_json_keys_fail_before_plan_creation(tmp_path,monkeypatch):
    d,s,p=files(tmp_path,monkeypatch);new=tmp_path/'new-plan.json'
    d.write_text('{"schema":"x","schema":"y"}')
    assert cli.main(['plan','--dataset',str(d),'--systems',str(s),'--experiment-id','test','--out',str(new)])==2
    assert not new.exists()


def test_offline_host_trial_never_claims_rendering_or_full_application(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,'inventory',lambda:{'sha256':SOURCE})
    home=tmp_path/'trial';out=tmp_path/'host.json'
    assert cli.main(['host-trial','--home',str(home),'--out',str(out)])==3
    r=json.loads(out.read_text())
    assert r['status']=='PARTIAL' and r['model']['status']=='NOT_RUN'
    assert r['browser']['status']=='NOT_RUN' and not r['whole_form_prepared']
    assert not r['full_application_trial'] and r['release_source_sha256']==SOURCE


def test_host_model_calls_require_config_before_home_created(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,'inventory',lambda:{'sha256':SOURCE})
    home=tmp_path/'trial';out=tmp_path/'host.json'
    assert cli.main(['host-trial','--home',str(home),'--allow-model-calls','--out',str(out)])==2
    assert not home.exists() and not out.exists()


def test_partition_cli_never_promotes_identical_content_to_heldout(tmp_path):
    dev=dataset();dev['split']='development';held=deepcopy(dev);held['split']='held_out'
    a,b,out=tmp_path/'dev.json',tmp_path/'held.json',tmp_path/'audit.json'
    a.write_text(json.dumps(dev));b.write_text(json.dumps(held))
    assert cli.main(['audit-partition','--development',str(a),'--heldout',str(b),'--out',str(out)])==3
    assert not json.loads(out.read_text())['heldout_independence_verified']


def test_output_symlink_never_overwrites_target(tmp_path):
    original=tmp_path/'original';original.write_text('KEEP')
    link=tmp_path/'report.json';link.symlink_to(original)
    assert cli.main(['inspect-host','--out',str(link)])==2 and original.read_text()=='KEEP'
