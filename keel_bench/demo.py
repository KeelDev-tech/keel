"""Deliberately fallible scripted comparison, not an inference benchmark."""
from dataclasses import asdict
import json
from keel_agent.models import HTTPResult,ReviewerConfig
from .experiment import make_plan,plan_digest,run_experiment
from .comparison import analyze_run


def scripted_pass(config,payload,timeout):
    """Intentionally returns PASS regardless of evidence to test false positives."""
    subject=json.loads(payload['messages'][1]['content'])['subject']
    assessment={'verdict':'PASS','covered_claim_ids':subject['required_claim_ids'],'findings':[]}
    return HTTPResult(200,{'content-type':'application/json'},json.dumps({
        'model':config.model,'done':True,'created_at':'synthetic-scripted-response',
        'message':{'role':'assistant','content':json.dumps(assessment)}}).encode())


def run_demo(dataset,*,source_sha256):
    config=ReviewerConfig('scripted-fixture','ollama','http://127.0.0.1:11434/api/chat',
                          'scripted-fixture',timeout_seconds=1)
    systems=[{'system_id':'abstain-baseline','kind':'abstain_baseline','model_config':None,'declared_weights_sha256':None},
        {'system_id':'scripted-always-pass','kind':'local_model','model_config':asdict(config),'declared_weights_sha256':None}]
    plan=make_plan(dataset,systems,experiment_id='SYNTHETIC-SCORING-ONLY',source_sha256=source_sha256,trials=3,seed=41,
                   max_model_calls=100,max_wall_seconds=60)
    pin=plan_digest(plan)
    run=run_experiment(dataset,plan,expected_plan_sha256=pin,source_sha256=source_sha256,
                       allow_model_calls=True,transport=scripted_pass)
    comparison=analyze_run(dataset,plan,run,expected_plan_sha256=pin)
    return {'plan':plan,'run':run,'comparison':comparison,'synthetic':True,
        'real_model_calls':0,'execution_authorized':False,'purpose':'Exercise repeated scoring and deliberately wrong responses.'}
