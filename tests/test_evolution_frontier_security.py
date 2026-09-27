"""Regressions for saturated revocation, recovery isolation and data partitions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from keel_evolve.core import EvolutionEngine
from keel_evolve.workflow import RULE, heldout_fixture, adjudications
from keel_machine.common import digest


class FrontierSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = [1000]
        self.engine = EvolutionEngine.create(self.root/'engine',clock=lambda:self.now[0])
        self.observe('good','PASS')
        self.observe('bad','FAIL')

    def observe(self, identifier, outcome, engine=None, evidence_hash=None):
        (engine or self.engine).observe(identifier,'family','review',outcome,
            evidence_sha256=evidence_hash or digest(identifier),context={})

    def procedure(self, identifier='procedure', state='PROMOTED'):
        self.engine.propose_procedure(identifier,'family',[{'operation':'evaluate_rule'}],['good'],
            required_state={},mutable_inputs=['subject'],validators=[digest('validator')],rule=RULE)
        # Isolate lifecycle regressions without treating test data as qualification.
        with self.engine.db.transaction() as db:
            db.execute('UPDATE artifacts SET state=? WHERE artifact_id=?',(state,identifier))

    def usage(self):
        with self.engine.db.transaction() as db:
            return self.engine._usage(db)

    def state(self, identifier='procedure'):
        with self.engine.db.transaction() as db:
            return db.execute('SELECT state FROM artifacts WHERE artifact_id=?',(identifier,)).fetchone()[0]

    def test_contradiction_withdraws_when_details_exhaust_byte_quota(self):
        self.procedure()
        with patch('keel_evolve.hardening.BYTE_LIMIT',self.usage()+1):
            result = self.engine.feedback('feedback','procedure','bad')
            self.assertFalse(result['detail_recorded'])
            self.assertEqual(self.state(),'HELD')
            with self.assertRaises(ValueError):self.engine.replay_plan('procedure',{}, {'subject':{}})
        with self.engine.db.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evolution_feedback').fetchone()[0],0)
        # Once detail fits, retry fills the audit record while retaining the hold.
        self.assertTrue(self.engine.feedback('feedback','procedure','bad')['detail_recorded'])

    def test_contradiction_withdraws_when_feedback_table_is_full(self):
        self.procedure()
        with self.engine.db.transaction() as db:
            for index in range(2):
                db.execute('INSERT INTO evolution_feedback VALUES(?,?,?,?)',
                           (str(index),'procedure','bad','contradiction'))
        with patch('keel_evolve.hardening.ROW_LIMIT',2):
            self.assertFalse(self.engine.feedback('new','procedure','bad')['detail_recorded'])
            self.assertEqual(self.state(),'HELD')

    def test_retirement_still_withdraws_after_quota_is_lowered(self):
        self.procedure()
        with patch('keel_evolve.hardening.BYTE_LIMIT',1):
            result = self.engine.retire('procedure','Unsafe artifact')
            self.assertFalse(result['detail_recorded'])
            self.assertEqual(self.state(),'HELD')

    def test_rollback_withdraws_after_quota_is_lowered(self):
        self.procedure('previous');self.now[0] += 1;self.procedure('failed')
        with patch('keel_evolve.hardening.BYTE_LIMIT',1):
            result = self.engine.rollback('failed','previous')
            self.assertFalse(result['detail_recorded'])
            self.assertEqual(result['selected_artifact_id'],'previous')
            self.assertEqual(self.state('failed'),'HELD')
            self.assertEqual(self.state('previous'),'PROMOTED')

    def test_invalid_feedback_never_withdraws_at_capacity(self):
        self.procedure()
        with patch('keel_evolve.hardening.BYTE_LIMIT',1):
            with self.assertRaisesRegex(ValueError,'feedback_evidence_invalid'):
                self.engine.feedback('forged','procedure','good')
        self.assertEqual(self.state(),'PROMOTED')

    def test_backup_rejects_unsafe_ancestor_before_creation(self):
        parent = self.root/'shared';parent.mkdir();parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError,'storage_writable_ancestor'):
            self.engine.backup(parent/'backup')
        self.assertFalse((parent/'backup').exists())

    def test_restore_metadata_has_a_read_limit(self):
        self.engine.backup(self.root/'backup')
        (self.root/'backup'/'backup.json').write_bytes(b'x'*4097)
        with self.assertRaisesRegex(ValueError,'backup_metadata_too_large'):
            EvolutionEngine.restore(self.root/'backup',self.root/'restored')
        self.assertFalse((self.root/'restored').exists())

    def test_raw_and_nested_failure_subjects_cannot_become_holdout(self):
        for wrapper in ('raw','nested'):
            with self.subTest(wrapper=wrapper):
                dataset = heldout_fixture(dataset_id='fixture-'+wrapper,offset=100 if wrapper=='raw' else 200)
                subject = dataset['cases'][0]['subject']
                fixture = subject if wrapper=='raw' else {'response':[{'case':subject}]}
                self.engine.failure_to_scenario('family',{'code':'failure'},fixture=fixture,
                    invariant={'path':['state'],'operator':'equals','expected':'HELD'})
                with self.assertRaisesRegex(ValueError,'dataset_partition_leak_or_reuse'):
                    self.engine.register_dataset(dataset)

    def test_existing_holdout_cannot_enter_raw_failure_fixture(self):
        dataset = heldout_fixture();self.engine.register_dataset(dataset)
        with self.assertRaisesRegex(ValueError,'scenario_holdout_leak'):
            self.engine.failure_to_scenario('family',{},fixture=dataset['cases'][0]['subject'],
                invariant={'path':['state'],'operator':'equals','expected':'HELD'})

    def restored(self):
        old = heldout_fixture(dataset_id='old',offset=0)
        self.engine.register_dataset(old)
        self.procedure(state='SIMULATION')
        plan,_,_ = self.engine.freeze('procedure',old,adjudications(old))
        token = self.engine.tournament('procedure',evaluation_id='old-trials',dataset=old,plan=plan)
        self.engine.backup(self.root/'backup')
        recovered = EvolutionEngine.restore(self.root/'backup',self.root/'restored',clock=lambda:1001)
        self.observe('fresh','PASS',recovered)
        fresh = heldout_fixture(dataset_id='fresh',offset=100)
        key = recovered.register_dataset(fresh)
        return recovered,old,fresh,key,token

    def test_restored_artifact_forks_into_new_candidate_with_fresh_trials(self):
        recovered,old,fresh,key,old_token = self.restored()
        candidate = recovered.fork_for_requalification('procedure','recovered',
            evidence_ids=['fresh'],dataset_sha256=key)
        self.assertEqual(candidate['state'],'CANDIDATE')
        self.assertEqual(candidate['body']['requalification']['dataset_sha256'],key)
        self.assertNotEqual(candidate['body_sha256'],old_token['artifact_body_sha256'])
        with self.assertRaises(ValueError):recovered.evaluate('old-trials','recovered',old_token)
        self.assertEqual(recovered.lineage('procedure')['state'],'HELD')
        plan,_,_ = recovered.freeze('recovered',fresh,adjudications(fresh))
        token = recovered.tournament('recovered',evaluation_id='new-trials',dataset=fresh,plan=plan)
        self.assertEqual(recovered.evaluate('new-trials','recovered',token)['state'],'SIMULATION')

    def test_recovery_requires_fresh_id_holdout_and_evidence(self):
        recovered,old,fresh,key,_ = self.restored()
        for identifier,evidence,holdout,expected in (
            ('procedure',['fresh'],key,'new_id'),
            ('recovered',['good'],key,'fresh_evidence'),
            ('recovered',['fresh'],recovered.register_dataset(old),'fresh_holdout')):
            with self.subTest(expected=expected),self.assertRaisesRegex(ValueError,expected):
                recovered.fork_for_requalification('procedure',identifier,evidence_ids=evidence,dataset_sha256=holdout)
        self.observe('relabeled','PASS',recovered,evidence_hash=digest('good'))
        with self.assertRaisesRegex(ValueError,'fresh_evidence'):
            recovered.fork_for_requalification('procedure','recovered',evidence_ids=['relabeled'],dataset_sha256=key)

    def test_recovery_trials_cannot_substitute_another_registered_holdout(self):
        recovered,_,fresh,key,_ = self.restored()
        other = heldout_fixture(dataset_id='other',offset=200)
        recovered.register_dataset(other)
        recovered.fork_for_requalification('procedure','recovered',evidence_ids=['fresh'],dataset_sha256=key)
        plan,_,_ = recovered.freeze('recovered',other,adjudications(other))
        with self.assertRaisesRegex(ValueError,'holdout_binding_mismatch'):
            recovered.tournament('recovered',evaluation_id='wrong-trials',dataset=other,plan=plan)

    def test_recovery_preserves_dependencies_and_rejects_a_withdrawn_parent(self):
        self.procedure('parent')
        self.engine.derive('child','parent',['good'])
        self.engine.retire('parent','withdraw parent')
        dataset = heldout_fixture();key = self.engine.register_dataset(dataset)
        self.observe('fresh','PASS')
        with self.assertRaisesRegex(ValueError,'inactive_or_expired'):
            self.engine.fork_for_requalification('child','recovered',evidence_ids=['fresh'],dataset_sha256=key)
        with self.engine.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM artifacts WHERE artifact_id='recovered'").fetchone())

    def test_successful_recovery_fork_keeps_parent_revocation_binding(self):
        self.procedure('parent')
        child = self.engine.derive('child','parent',['good'])
        self.engine.feedback('child-failure','child','bad')
        key = self.engine.register_dataset(heldout_fixture())
        self.observe('fresh','PASS')
        recovered = self.engine.fork_for_requalification('child','recovered',
            evidence_ids=['fresh'],dataset_sha256=key)
        self.assertEqual(recovered['body']['dependencies'],child['body']['dependencies'])
        self.engine.retire('parent','withdraw parent')
        with self.assertRaisesRegex(ValueError,'inactive_or_expired'):
            self.engine.runners('recovered')


if __name__ == '__main__':
    unittest.main()
