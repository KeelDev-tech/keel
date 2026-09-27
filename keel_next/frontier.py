"""Integrated local source fairness, skill lineage and balanced evaluation demo."""
import os
from pathlib import Path
import runpy

from keel_machine.common import digest, require, _ancestors
from keel_evolve.core import EvolutionEngine
from keel_evolve.workflow import RULE, heldout_fixture, adjudications


def lineage_demo(home):
    engine=EvolutionEngine.create(home)
    parent_data=heldout_fixture('parent-suite',offset=0)
    child_data=heldout_fixture('child-suite',offset=24)
    for dataset in (parent_data,child_data):engine.register_dataset(dataset)
    engine.observe('proof','review','fixture','PASS',evidence_sha256=digest('synthetic-proof'),context={'synthetic':True})
    engine.propose_procedure('parent','review',[{'operation':'evaluate_rule'}],['proof'],
        required_state={'revision':1},mutable_inputs=['subject'],validators=[digest(RULE)],rule=RULE)
    def screen(name,dataset):
        plan,_,_=engine.freeze(name,dataset,adjudications(dataset))
        token=engine.tournament(name,evaluation_id='eval-'+name,dataset=dataset,plan=plan)
        return engine.evaluate('eval-'+name,name,token)
    parent=screen('parent',parent_data)
    engine.derive('child','parent',['proof']);child=screen('child',child_data)
    plan=engine.replay_plan('child',{'revision':1},{'subject':child_data['cases'][0]['subject']})
    contract=engine.bind_action_contract(plan,target={'kind':'local-review'},evidence_bindings={'proof':digest('synthetic-proof')},
                                       authority_revision_sha256=digest('simulation'))
    args=dict(current_target={'kind':'local-review'},current_evidence_bindings={'proof':digest('synthetic-proof')},
              current_authority_revision_sha256=digest('simulation'),current_state={'revision':1})
    execution=engine.execute_review(contract,**args)
    engine.observe('contradiction','review','fixture','FAIL',evidence_sha256=digest('synthetic-bad-proof'),context={'synthetic':True})
    engine.feedback('withdraw','parent','contradiction')
    checked=engine.validate_action_contract(contract,**args)
    checks={'actual_parent_screen':parent['state']=='SIMULATION',
            'actual_child_screen':child['state']=='SIMULATION',
            'bound_pure_review':execution['result']=='ABSTAIN' and execution['simulation_only'],
            'descendant_withdrawn':engine.lineage('child')['state']=='HELD',
            'prior_contract_stale':checked['status']=='STALE'}
    return {'status':'LINEAGE_PASSED' if all(checks.values()) else 'LINEAGE_FAILED',
            'checks':checks,'synthetic':True,'execution_authorized':False}


def run(home):
    home=Path(os.path.abspath(home));_ancestors(home)
    require(home.parent.resolve(strict=True)==home.parent,'frontier_parent_invalid')
    home.mkdir(mode=0o700,exist_ok=False)
    runpy.run_path(str(Path(__file__).resolve().parents[1]/'keel.py'),run_name='frontier_loader')
    from keel_next.source_scheduler_demo import run_demo
    from keel_eval.frontier import run_benchmark
    sources=run_demo(home/'sources')
    lineage=lineage_demo(home/'lineage')
    evaluation=run_benchmark()
    reports={'sources':sources,'lineage':lineage,'evaluation':evaluation}
    checks={name:report['status'].endswith('PASSED') for name,report in reports.items()}
    return {'schema':'keel.frontier.demo.v1','status':'FRONTIER_PASSED' if all(checks.values()) else 'FRONTIER_FAILED',
            'checks':checks,'reports':reports,'synthetic':True,'execution_authorized':False,
            'paid_services_required':False,'production_qualified':False}
