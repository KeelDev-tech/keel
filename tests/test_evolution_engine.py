"""Adversarial lifecycle, actual trials, quotas and recovery tests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from keel_machine.common import digest
from keel_evolve.core import EvolutionEngine
from keel_evolve.workflow import heldout_fixture, adjudications, RULE
from keel_eval.reliability import Runner, freeze_plan
from keel_evolve.interpreter import artifact_runner


class EvolutionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=[1000]
        self.home=Path(self.tmp.name)/'engine'
        self.engine=EvolutionEngine.create(self.home,clock=lambda:self.clock[0])
        self.engine.observe('ev','family','strategy','PASS',evidence_sha256=digest('ev'),context={})

    def artifact(self, name='lesson', *, procedure=False, ttl=3600, rule=None):
        if procedure:
            return self.engine.propose_procedure(name,'family',[{'operation':'evaluate_rule'}],['ev'],
                required_state={'version':1},mutable_inputs=['subject'],validators=[digest('v')],
                rule=RULE if rule is None else rule,ttl_seconds=ttl)
        return self.engine.propose_lesson(name,'family','Evidence is required',['ev'],
            applicability={'version':1},invalidators=['revoked'],rule=RULE if rule is None else rule,ttl_seconds=ttl)

    def setup_trial(self, *, synthetic=False, name='lesson', procedure=False):
        dataset=heldout_fixture(synthetic=synthetic)
        self.engine.register_dataset(dataset)
        art=self.artifact(name,procedure=procedure)
        plan,base,candidate=self.engine.freeze(name,dataset,adjudications(dataset))
        return art,dataset,plan,base,candidate

    def qualify(self, name='lesson', synthetic=False):
        art,dataset,plan,base,candidate=self.setup_trial(name=name,synthetic=synthetic)
        result=self.engine.qualify('evaluation',name,plan,dataset,baseline=base,candidate=candidate,
                                  adjudication_validator=lambda p,d:True)
        return result

    def seed(self, name='procedure', state='SIMULATION'):
        self.artifact(name,procedure=True)
        # Test-only seeding isolates contract state checks from qualification tests.
        with self.engine.db.transaction() as db:db.execute('UPDATE artifacts SET state=? WHERE artifact_id=?',(state,name))
        return self.engine.replay_plan(name,{'version':1},{'subject':{}})

    def test_actual_trials_consume_artifact(self):
        result=self.qualify()
        self.assertTrue(result['qualified_task_distribution']);self.assertFalse(result['production_qualified'])
        with self.engine.db.transaction() as db:
            receipt=json.loads(db.execute('SELECT receipt FROM evolution_trials').fetchone()[0])
        self.assertEqual(len(receipt['report']['trials']),144)
        self.assertEqual({r['repeat'] for r in receipt['report']['trials']},{0,1,2})
        self.assertEqual(receipt['plan']['candidate']['config_sha256'],digest({'artifact':self.engine.retrieve('family',{'version':1})['lessons'][0]['body'],
                          'artifact_body_sha256':receipt['artifact_body_sha256']}))

    def test_synthetic_never_production_promotes(self):
        with self.assertRaisesRegex(ValueError,'not_qualified'):self.qualify(synthetic=True)
        self.assertEqual(self.engine.retrieve('family',{'version':1})['lessons'],[])

    def test_unattested_never_promotes(self):
        a,d,p,b,c=self.setup_trial()
        with self.assertRaisesRegex(ValueError,'not_qualified'):
            self.engine.qualify('eval','lesson',p,d,baseline=b,candidate=c,adjudication_validator=lambda p,d:False)

    def test_simulation_requires_internally_stored_real_trials(self):
        a,d,p,b,c=self.setup_trial(synthetic=True,procedure=True)
        token=self.engine.tournament('lesson',evaluation_id='trial',dataset=d,plan=p)
        self.assertEqual(self.engine.evaluate('trial','lesson',token)['state'],'SIMULATION')
        with self.assertRaises(ValueError):self.engine.evaluate('invented','lesson',token)

    def test_changed_rule_cannot_borrow_other_qualification(self):
        a,d,p,b,c=self.setup_trial()
        different=dict(RULE,on_match='PASS')
        c.config['artifact']['rule']=different
        with self.assertRaisesRegex(ValueError,'bound_to_artifact'):
            self.engine.qualify('eval','lesson',p,d,baseline=b,candidate=c,adjudication_validator=lambda p,d:True)

    def test_unsupported_steps_cannot_be_qualified(self):
        self.engine.propose_procedure('unsafe','family',[{'operation':'shell','command':'echo nope'}],['ev'],
             required_state={},mutable_inputs=[],validators=[digest('v')],rule=RULE)
        with self.assertRaisesRegex(ValueError,'unsupported_procedure'):self.engine.runners('unsafe')

    def test_holdout_must_precede_candidate(self):
        self.artifact()
        d=heldout_fixture();self.engine.register_dataset(d)
        p,b,c=self.engine.freeze('lesson',d,adjudications(d))
        with self.assertRaisesRegex(ValueError,'frozen_before_artifact'):
            self.engine.qualify('eval','lesson',p,d,baseline=b,candidate=c,adjudication_validator=lambda p,d:True)

    def test_partition_leak_and_renamed_cases_rejected(self):
        d=heldout_fixture();d['split']='development';self.engine.register_dataset(d)
        d['split']='held_out';d['dataset_id']='new-id'
        for c in d['cases']:c['case_id']='renamed-'+c['case_id']
        with self.assertRaisesRegex(ValueError,'partition_leak'):self.engine.register_dataset(d)

    def test_duplicate_subjects_not_independent_cases(self):
        d=heldout_fixture();d['cases'][1]['subject']=d['cases'][0]['subject']
        with self.assertRaisesRegex(ValueError,'duplicate_subject'):self.engine.register_dataset(d)

    def test_holdout_cannot_be_reused_for_second_artifact(self):
        a,d,p,b,c=self.setup_trial(synthetic=True)
        self.artifact('second')
        self.engine.tournament('lesson',evaluation_id='one',dataset=d,plan=p)
        p2,_,_=self.engine.freeze('second',d,adjudications(d))
        with self.assertRaisesRegex(ValueError,'already_consumed'):
            self.engine.tournament('second',evaluation_id='two',dataset=d,plan=p2)

    def test_expiry_and_context_filter(self):
        self.artifact(ttl=2)
        with self.engine.db.transaction() as db:db.execute("UPDATE artifacts SET state='PROMOTED'")
        self.assertEqual(len(self.engine.retrieve('family',{'version':1})['lessons']),1)
        self.assertEqual(self.engine.retrieve('family',{'version':True})['lessons'],[])
        self.assertEqual(self.engine.retrieve('family',{'version':1,'invalidators':['revoked']})['lessons'],[])
        self.clock[0]=1003
        self.assertEqual(self.engine.retrieve('family',{'version':1})['lessons'],[])

    def test_body_tamper_is_held(self):
        plan=self.seed()
        with self.engine.db.transaction() as db:db.execute("UPDATE artifacts SET body_json=?",(b'{}',))
        with self.assertRaisesRegex(ValueError,'body_corrupt'):
            self.engine.replay_plan('procedure',{'version':1},{'subject':{}})

    def contract(self, plan):
        return self.engine.bind_action_contract(plan,target={'account':'local'},evidence_bindings={'form':digest('form')},authority_revision_sha256=digest('authority'))

    def check(self, contract):
        return self.engine.validate_action_contract(contract,current_target={'account':'local'},current_evidence_bindings={'form':digest('form')},current_authority_revision_sha256=digest('authority'),current_state={'version':1})

    def test_forged_simulation_flag_does_not_admit(self):
        contract=self.contract(self.seed());contract['simulation_only']=False
        contract['contract_sha256']=digest({k:v for k,v in contract.items() if k!='contract_sha256'})
        self.assertEqual(self.check(contract)['status'],'STALE')

    def test_contradiction_withdraws_existing_plan(self):
        contract=self.contract(self.seed())
        self.engine.observe('bad','family','strategy','FAIL',evidence_sha256=digest('bad'),context={})
        self.engine.feedback('feedback','procedure','bad')
        self.assertEqual(self.check(contract)['status'],'STALE')
        self.engine.feedback('feedback','procedure','bad')

    def test_cross_family_feedback_rejected(self):
        self.seed();self.engine.observe('bad','other','strategy','FAIL',evidence_sha256=digest('bad'),context={})
        with self.assertRaisesRegex(ValueError,'feedback_evidence'):self.engine.feedback('feedback','procedure','bad')

    def test_rollback_selects_only_current_qualified_version(self):
        self.seed('older','PROMOTED');self.clock[0]=1001;self.seed('newer','PROMOTED')
        self.assertEqual(self.engine.rollback('newer','older')['selected_artifact_id'],'older')
        self.engine.retire('older','withdrawn')
        with self.assertRaises(ValueError):self.engine.rollback('newer','older')

    def test_backup_restore_holds_all_promotions(self):
        self.seed(state='PROMOTED')
        backup=Path(self.tmp.name)/'backup';self.engine.backup(backup)
        restored=EvolutionEngine.restore(backup,Path(self.tmp.name)/'restored',clock=lambda:1001)
        self.assertEqual(restored.status()['artifacts'],{'PROCEDURE:HELD':1})
        with self.assertRaises(ValueError):restored.replay_plan('procedure',{'version':1},{'subject':{}})
        with self.assertRaises(FileExistsError):EvolutionEngine.restore(backup,restored.home,clock=lambda:1001)

    def test_restore_rejects_changed_backup(self):
        backup=Path(self.tmp.name)/'backup';self.engine.backup(backup)
        with (backup/'evolution.sqlite3').open('ab') as stream:stream.write(b'corrupt')
        with self.assertRaisesRegex(ValueError,'backup_digest'):EvolutionEngine.restore(backup,Path(self.tmp.name)/'restored')

    def test_evidence_capacity_rolls_back(self):
        with patch('keel_evolve.hardening.ROW_LIMIT',1):
            with self.assertRaisesRegex(ValueError,'row_quota'):
                self.engine.observe('two','family','strategy','PASS',evidence_sha256=digest('two'),context={})
        self.assertEqual(self.engine.status()['evidence'],1)

    def test_byte_capacity_rolls_back(self):
        with patch('keel_evolve.hardening.BYTE_LIMIT',2048):
            with self.assertRaisesRegex(ValueError,'byte_quota'):
                self.engine.observe('two','family','strategy','PASS',evidence_sha256=digest('two'),context={'data':'x'*4096})
        self.assertEqual(self.engine.status()['evidence'],1)

    def test_missing_database_not_reprovisioned(self):
        (self.home/'evolution.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError,'missing_database'):EvolutionEngine(self.home)

    def test_quotas_cover_scenarios_and_community(self):
        invariant={'path':['status'],'operator':'equals','expected':'HELD'}
        self.engine.failure_to_scenario('family',{'code':'a'},fixture={},invariant=invariant)
        with patch('keel_evolve.hardening.ROW_LIMIT',1):
            with self.assertRaisesRegex(ValueError,'row_quota'):
                self.engine.failure_to_scenario('family',{'code':'b'},fixture={},invariant=invariant)
        index=self.engine.export_scenario_index();self.engine.import_scenario_index(index)
        self.assertEqual(self.engine.status()['community_candidates'],1)

    def test_foreign_evidence_cannot_support_artifact(self):
        with self.assertRaisesRegex(ValueError,'evidence_invalid'):
            self.engine.propose_lesson('foreign','other','Statement',['ev'],applicability={},invalidators=[])

    def test_expired_artifact_cannot_renew_by_reproposal(self):
        self.artifact(ttl=1);self.clock[0]=1002;self.artifact(ttl=1)
        with self.assertRaises(ValueError):self.engine.runners('lesson')

    def test_replay_cannot_change_step_or_inputs(self):
        plan=self.seed();plan['steps']=[{'operation':'arbitrary'}]
        with self.assertRaisesRegex(ValueError,'plan_changed'):self.contract(plan)

    def test_failure_fixture_cannot_become_holdout(self):
        d=heldout_fixture()
        self.engine.failure_to_scenario('family',{'code':'failure'},fixture={'subject':d['cases'][0]['subject']},
            invariant={'path':['status'],'operator':'equals','expected':'HELD'})
        with self.assertRaisesRegex(ValueError,'partition_leak'):self.engine.register_dataset(d)

    def test_worker_reports_installed_os_limits(self):
        self.qualify()
        with self.engine.db.transaction() as db:
            receipt=json.loads(db.execute('SELECT receipt FROM evolution_trials').fetchone()[0])
        self.assertEqual(receipt['report']['worker_limits'],{
            'RLIMIT_CPU':[3,3], 'RLIMIT_AS':[536870912,536870912],
            'RLIMIT_FSIZE':[2097152,2097152], 'RLIMIT_NOFILE':[64,64], 'RLIMIT_CORE':[0,0]})

    def test_worker_timeout_cannot_promote(self):
        import subprocess
        a,d,p,b,c=self.setup_trial()
        with patch('keel_evolve.hardening.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',8)):
            with self.assertRaisesRegex(ValueError,'wall_deadline'):
                self.engine.tournament('lesson',evaluation_id='timeout',dataset=d,plan=p)
        self.assertEqual(self.engine.status()['artifacts'],{'LESSON:CANDIDATE':1})

    def test_worker_nonzero_cannot_promote(self):
        import subprocess
        a,d,p,b,c=self.setup_trial()
        with patch('keel_evolve.hardening.subprocess.run',return_value=subprocess.CompletedProcess([],2)):
            with self.assertRaisesRegex(ValueError,'worker_failed'):
                self.engine.tournament('lesson',evaluation_id='crash',dataset=d,plan=p)

    def test_changed_form_state_rejects_review(self):
        contract=self.contract(self.seed())
        with self.assertRaisesRegex(ValueError,'contract_stale'):
            self.engine.execute_review(contract,current_target={'account':'local'},
                current_evidence_bindings={'form':digest('form')},
                current_authority_revision_sha256=digest('authority'),current_state={'version':2})

    def test_pure_review_executes_bound_rule_and_stops_after_retirement(self):
        contract=self.contract(self.seed())
        args=dict(current_target={'account':'local'},current_evidence_bindings={'form':digest('form')},
                  current_authority_revision_sha256=digest('authority'),current_state={'version':1})
        result=self.engine.execute_review(contract,**args)
        self.assertEqual(result['result'],'ABSTAIN');self.assertTrue(result['simulation_only'])
        self.assertFalse(result['execution_authorized'])
        self.engine.retire('procedure','withdrawn')
        with self.assertRaises(ValueError):self.engine.execute_review(contract,**args)

    def test_absent_value_cannot_satisfy_negative_invariant(self):
        scenario=self.engine.failure_to_scenario('family',{'code':'missing'},fixture={},
            invariant={'path':['status'],'operator':'not_equals','expected':'BAD'})
        self.assertFalse(self.engine.run_scenario(scenario['scenario_id'],{})['passed'])

    def test_actual_pipeline_and_fault_adapters(self):
        from keel_evolve.workflow import run
        from keel_evolve.faultlab import run as faultlab
        self.assertEqual(run(Path(self.tmp.name)/'workflow')['status'],'WORKFLOW_PASSED')
        self.assertEqual(faultlab()['status'],'FAULTLAB_PASSED')


if __name__=='__main__':unittest.main()
