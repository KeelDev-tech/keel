"""Bounded local learning from Keel's actual offline pipeline adapter."""
import hashlib
from pathlib import Path
import tempfile

from keel_machine.common import digest, require
from keel_eval.reliability import freeze_plan
from .core import EvolutionEngine


RULE = {'path':['evidence'], 'equals':[], 'on_match':'ABSTAIN', 'otherwise':'FAIL'}


def heldout_fixture(dataset_id='synthetic-heldout', *, synthetic=True, offset=0):
    return {'schema':'keel.eval.dataset.v1','dataset_id':dataset_id,'synthetic':synthetic,
            'split':'held_out','label_source':'synthetic_fixture' if synthetic else 'operator_supplied',
            'cases':[{'case_id':'case-'+str(i+offset),'tags':['missing-evidence'],
                'expected_verdict':'ABSTAIN','label_rationale':'Evidence is missing.',
                'subject':{'required_claim_ids':['claim'],
                    'claims':[{'claim_id':'claim','text':'Synthetic claim '+str(i+offset)}],
                    'evidence':[]}} for i in range(24)]}


def adjudications(dataset):
    return {c['case_id']:{'cluster_id':c['case_id'],'adjudicator_id':'fixture-adjudicator',
                         'record_sha256':digest(c)} for c in dataset['cases']}


def run(home):
    # A new workspace only; the actual discovery/verification/outbox implementation
    # runs with controlled fixtures and the benchmark's network denial guard.
    engine=EvolutionEngine.create(home)
    from keel_next.benchmark import run_benchmark
    pipeline=run_benchmark(engine.home/'pipeline',boards=2,jobs_per_board=12,max_new=8)
    require(pipeline['status']=='BENCHMARK_PASSED','learning_pipeline_failed')
    dataset=heldout_fixture()
    engine.register_dataset(dataset)
    evidence=engine.observe('pipeline-completed','pipeline-review','bounded-pipeline','PASS',
        evidence_sha256=digest(pipeline),context={'source_sha256':pipeline['source_sha256'],
            'workload_sha256':pipeline['workload_sha256'],'checks':pipeline['checks']})
    artifact=engine.propose_procedure('review-missing-evidence','pipeline-review',
        [{'operation':'evaluate_rule'}],['pipeline-completed'],required_state={'policy_revision':1},
        mutable_inputs=['subject'],validators=[digest(RULE)],rule=RULE)
    plan, _, _=engine.freeze(artifact['artifact_id'],dataset,adjudications(dataset))
    token=engine.tournament(artifact['artifact_id'],evaluation_id='observed-trials',dataset=dataset,plan=plan)
    evaluated=engine.evaluate('observed-trials',artifact['artifact_id'],token)
    replay=engine.replay_plan(artifact['artifact_id'],{'policy_revision':1},{'subject':dataset['cases'][0]['subject']})
    contract=engine.bind_action_contract(replay,target={'kind':'local-analysis'},
        evidence_bindings={'pipeline':digest(pipeline)},authority_revision_sha256=digest('simulation'))
    check=engine.validate_action_contract(contract,current_target={'kind':'local-analysis'},
        current_evidence_bindings={'pipeline':digest(pipeline)},current_authority_revision_sha256=digest('simulation'),current_state={'policy_revision':1})
    executed=engine.execute_review(contract,current_target={'kind':'local-analysis'},
        current_evidence_bindings={'pipeline':digest(pipeline)},current_authority_revision_sha256=digest('simulation'),
        current_state={'policy_revision':1})
    engine.observe('changed-evidence','pipeline-review','bounded-pipeline','FAIL',
        evidence_sha256=digest('missing-current-evidence'),context={'synthetic':True})
    engine.feedback('withdraw-procedure',artifact['artifact_id'],'changed-evidence')
    withdrawn=engine.validate_action_contract(contract,current_target={'kind':'local-analysis'},
        current_evidence_bindings={'pipeline':digest(pipeline)},current_authority_revision_sha256=digest('simulation'),current_state={'policy_revision':1})
    checks={'actual_pipeline_passed':all(pipeline['checks'].values()),
            'actual_repeated_trials_screened':evaluated['state']=='SIMULATION',
            'simulation_preserved':check['status']=='SIMULATION_ONLY',
            'bound_rule_executed':executed['result']=='ABSTAIN' and executed['simulation_only'],
            'contradiction_revokes_replay':withdrawn['status']=='STALE'}
    return {'schema':'keel.evolution.workflow.v2','status':'WORKFLOW_PASSED' if all(checks.values()) else 'WORKFLOW_FAILED',
            'checks':checks,'pipeline_counts':pipeline['counts'],'synthetic':True,
            'learned_behavior':'Explicit missing-evidence review rule; no model training.',
            'execution_authorized':False,'paid_services_required':False}
