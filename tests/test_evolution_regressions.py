"""These six tests fail on the delivered Evolution 0.3.0 source."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from keel_evolve.core import EvolutionEngine
from keel_eval.reliability import Runner, freeze_plan


def sha(value):return hashlib.sha256(value.encode()).hexdigest()


def pass_runner(subject, config, seed):return 'PASS'


def fail_runner(subject, config, seed):return 'FAIL'


class EvolutionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.engine=EvolutionEngine.create(Path(self.temp.name)/'engine',clock=lambda:1000)
        self.engine.observe('ev','family','strategy','PASS',evidence_sha256=sha('ev'),context={})
        self.lesson=self.engine.propose_lesson('lesson','family','Only for version one',['ev'],
            applicability={'version':1},invalidators=['revoked'])
        self.procedure=self.engine.propose_procedure('procedure','family',[{'operation':'evaluate_rule'}],['ev'],
            required_state={'version':1},mutable_inputs=[],validators=[sha('validator')])

    def state(self, artifact, state):
        with self.engine.db.transaction() as db:
            db.execute('UPDATE artifacts SET state=? WHERE artifact_id=?',(state,artifact))

    def test_context_mismatch_cannot_retrieve(self):
        self.state('lesson','PROMOTED')
        self.assertEqual(self.engine.retrieve('family',{'version':2})['lessons'],[])

    def test_invalidator_cannot_retrieve(self):
        self.state('lesson','PROMOTED')
        self.assertEqual(self.engine.retrieve('family',{'version':1,'revoked':True})['lessons'],[])

    def contract(self):
        self.state('procedure','SIMULATION')
        plan=self.engine.replay_plan('procedure',{'version':1},{})
        return self.engine.bind_action_contract(plan,target={'account':'local'},
             evidence_bindings={'form':sha('form')},authority_revision_sha256=sha('authority'))

    def test_simulation_restriction_survives_contract(self):
        self.assertIs(self.contract().get('simulation_only'),True)

    def test_retirement_revokes_existing_contract(self):
        contract=self.contract();self.engine.retire('procedure','withdrawn')
        result=self.engine.validate_action_contract(contract,current_target={'account':'local'},
            current_evidence_bindings={'form':sha('form')},current_authority_revision_sha256=sha('authority'))
        self.assertEqual(result['status'],'STALE')

    def test_declared_repeats_are_not_observed_trials(self):
        cases=[{'case_id':str(i),'champion':'FAIL','candidate':'PASS'} for i in range(20)]
        with self.assertRaises(ValueError):
            self.engine.tournament('lesson',cases,harness_sha256=sha('harness'),repeats=3)

    def test_unrelated_runner_cannot_promote(self):
        cases=[{'case_id':'case-'+str(i),'tags':['fixture'],'expected_verdict':'FAIL',
            'label_rationale':'Test fixture', 'subject':{'required_claim_ids':['c'],
              'claims':[{'claim_id':'c','text':'Claim '+str(i)}],
              'evidence':[{'evidence_id':'e','text':'Contradiction'}]}} for i in range(24)]
        dataset={'schema':'keel.eval.dataset.v1','dataset_id':'fixture','synthetic':False,
            'split':'held_out','label_source':'operator_supplied','cases':cases}
        labels={c['case_id']:{'cluster_id':c['case_id'],'adjudicator_id':'fixture',
                            'record_sha256':sha(c['case_id'])} for c in cases}
        baseline,candidate=Runner(pass_runner,{}),Runner(fail_runner,{})
        plan=freeze_plan(dataset,baseline=baseline,candidate=candidate,adjudications=labels)
        with self.assertRaises(ValueError):
            self.engine.qualify('eval','lesson',plan,dataset,baseline=baseline,candidate=candidate,
                                adjudication_validator=lambda p,d:True)


if __name__=='__main__':unittest.main()
