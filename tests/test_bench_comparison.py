from copy import deepcopy
import json
from pathlib import Path
import pytest
from keel_bench.demo import run_demo,scripted_pass
from keel_bench.comparison import analyze_run
from keel_bench.experiment import BenchmarkError,plan_digest,run_experiment,make_plan
from keel_bench.datasets import audit_partition

SOURCE='a'*64


def data():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/grounding_eval/dataset.json').read_text())


def analyze(demo):return analyze_run(data(),demo['plan'],demo['run'],expected_plan_sha256=plan_digest(demo['plan']),bootstrap_samples=200)


def test_repeated_comparison_reconstructs_wrong_answers_and_baseline_abstentions():
    demo=run_demo(data(),source_sha256=SOURCE);r=analyze(demo)
    systems={s['system_id']:s['metrics'] for s in r['systems']}
    candidate=systems['scripted-always-pass'];base=systems['abstain-baseline']
    assert r['record_count']==54 and r['mode']=='INJECTED'
    assert candidate['false_pass_on_nonpass_labels']=={'numerator':21,'denominator':21,'value':1}
    assert candidate['exact_label_agreement']['numerator']==6
    assert base['abstentions']['numerator']==27
    assert base['withheld_supported_cases']['numerator']==6
    assert candidate['tasks_all_trials_match']['denominator']==9
    assert candidate['tasks_all_trials_match']['numerator']==2
    assert r['paired_comparisons'][0]['tasks']==9
    assert not r['state_of_the_art_established'] and not r['execution_authorized']
    assert r['human_intervention']=='NOT_MEASURED'


def test_cluster_bootstrap_is_reproducible_and_respects_case_count():
    demo=run_demo(data(),source_sha256=SOURCE)
    a,b=analyze(demo),analyze(demo)
    assert a==b
    pair=a['paired_comparisons'][0]
    assert pair['task_wins']+pair['task_ties']+pair['task_losses']==9
    assert pair['exact_agreement_delta_left_minus_right']==pytest.approx(1/3)
    low,high=pair['task_cluster_bootstrap_95_interval']
    assert -1<=low<=high<=1 and not pair['statistical_superiority_established']


@pytest.mark.parametrize('mutation',['missing','duplicate','label','subject','dataset','source','pin','authority','boolean_trial','negative_latency','total_calls','extra_key'])
def test_tampered_measurements_are_rejected(mutation):
    demo=run_demo(data(),source_sha256=SOURCE);r=demo['run']
    if mutation=='missing':r['records'].pop()
    if mutation=='duplicate':r['records'][1]=deepcopy(r['records'][0])
    if mutation=='label':r['records'][0]['expected_verdict']='not-gold'
    if mutation=='subject':r['records'][0]['subject_sha256']='b'*64
    if mutation=='dataset':r['dataset_sha256']='b'*64
    if mutation=='source':r['source_sha256']='b'*64
    if mutation=='pin':r['plan_sha256']='b'*64
    if mutation=='authority':r['execution_authorized']=True
    if mutation=='boolean_trial':r['records'][0]['trial']=False
    if mutation=='negative_latency':r['records'][0]['latency_ms']=-1
    if mutation=='total_calls':r['model_calls_attempted']+=1
    if mutation=='extra_key':r['accuracy']=1.0
    with pytest.raises(BenchmarkError):analyze(demo)


def test_skipped_calls_remain_errors_in_denominators():
    demo=run_demo(data(),source_sha256=SOURCE);p=deepcopy(demo['plan']);p['limits']['max_model_calls']=2
    r=run_experiment(data(),p,expected_plan_sha256=plan_digest(p),source_sha256=SOURCE,
                      allow_model_calls=True,transport=scripted_pass)
    result=analyze_run(data(),p,r,expected_plan_sha256=plan_digest(p),bootstrap_samples=100)
    metrics=next(s['metrics'] for s in result['systems'] if s['system_id']=='scripted-always-pass')
    assert metrics['errors']['numerator']==25 and metrics['errors']['denominator']==27
    assert metrics['validated_response_coverage']['numerator']==2
    assert metrics['latency']['missing_count']==25


def test_disabled_model_is_unmeasured_not_perfectly_safe_model():
    demo=run_demo(data(),source_sha256=SOURCE);p=demo['plan']
    r=run_experiment(data(),p,expected_plan_sha256=plan_digest(p),source_sha256=SOURCE)
    result=analyze_run(data(),p,r,expected_plan_sha256=plan_digest(p),bootstrap_samples=100)
    m=next(s['metrics'] for s in result['systems'] if s['system_id']=='scripted-always-pass')
    assert m['validated_response_coverage']['value']==0 and m['errors']['value']==1
    assert m['withheld_supported_cases']['value']==1


def test_single_task_has_no_bootstrap_interval():
    d=data();d['cases']=d['cases'][:1]
    r=run_demo(d,source_sha256=SOURCE)['comparison']
    assert r['paired_comparisons'][0]['task_cluster_bootstrap_95_interval'] is None


def test_zero_denominator_stays_null():
    d=data();d['cases']=[c for c in d['cases'] if c['expected_verdict']=='ABSTAIN']
    r=run_demo(d,source_sha256=SOURCE)['comparison']
    for system in r['systems']:assert system['metrics']['false_block_on_pass_labels']['value'] is None


def test_split_audit_ignores_changed_case_and_source_ids():
    dev=data();held=deepcopy(dev);held['dataset_id']='renamed';held['split']='held_out'
    for i,c in enumerate(held['cases']):
        c['case_id']='renamed-'+str(i)
        for e in c['subject']['evidence']:e['evidence_id']='renamed-'+e['evidence_id']
    r=audit_partition(dev,held)
    assert r['status']=='EXACT_OVERLAP_DETECTED' and len(r['subject_overlap'])==9
    assert not r['heldout_independence_verified'] and not r['semantic_overlap_checked']


def test_partition_requires_correct_roles():
    with pytest.raises(BenchmarkError):audit_partition(data(),data())


def test_no_exact_overlap_does_not_certify_independence():
    dev=data();held=deepcopy(dev);held['split']='held_out'
    for c in held['cases']:
        for x in c['subject']['claims']+c['subject']['evidence']:x['text']='Rephrased '+x['text']
    r=audit_partition(dev,held)
    assert r['status']=='NO_EXACT_OVERLAP_DETECTED'
    assert not r['heldout_independence_verified'] and not r['training_contamination_checked']


@pytest.mark.parametrize('value',[True,0,99,10001])
def test_bootstrap_work_bound(value):
    demo=run_demo(data(),source_sha256=SOURCE)
    with pytest.raises(BenchmarkError):analyze_run(data(),demo['plan'],demo['run'],expected_plan_sha256=plan_digest(demo['plan']),bootstrap_samples=value)
