"""Pinned ancestry admission and transitive invalidation under actual engine APIs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from keel_machine.common import digest
from keel_evolve.core import EvolutionEngine
from keel_evolve.workflow import RULE


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=[1000];self.engine=EvolutionEngine.create(Path(self.tmp.name)/'engine',clock=lambda:self.clock[0])
        self.engine.observe('proof','family','strategy','PASS',evidence_sha256=digest('proof'),context={})
        self.engine.propose_procedure('parent','family',[{'operation':'evaluate_rule'}],['proof'],
            required_state={'revision':1},mutable_inputs=['subject'],validators=[digest('validator')],rule=RULE)
        self.seed('parent')

    def seed(self, name, state='PROMOTED'):
        # Test-only state setup isolates ancestry from statistically tested promotion.
        with self.engine.db.transaction() as db:db.execute('UPDATE artifacts SET state=? WHERE artifact_id=?',(state,name))

    def chain(self):
        self.engine.derive('child','parent',['proof']);self.seed('child')
        self.engine.derive('grandchild','child',['proof']);self.seed('grandchild')
        return self.engine.replay_plan('grandchild',{'revision':1},{'subject':{}})

    def test_child_is_quarantined_and_pins_source(self):
        child=self.engine.derive('child','parent',['proof'])
        self.assertEqual(child['state'],'CANDIDATE')
        parent=self.engine.lineage('child')['parents'][0]
        self.assertEqual(parent['artifact_id'],'parent')
        with self.assertRaises(ValueError):self.engine.replay_plan('child',{'revision':1},{'subject':{}})

    def test_retirement_transitively_blocks_grandchild(self):
        self.chain();self.engine.retire('parent','bad')
        with self.assertRaises(ValueError):self.engine.replay_plan('grandchild',{'revision':1},{'subject':{}})
        self.assertEqual(self.engine.lineage('grandchild')['state'],'HELD')

    def test_contradiction_invalidates_already_bound_contract(self):
        plan=self.chain()
        contract=self.engine.bind_action_contract(plan,target={},evidence_bindings={'proof':digest('proof')},authority_revision_sha256=digest('a'))
        self.engine.observe('bad','family','strategy','FAIL',evidence_sha256=digest('bad'),context={})
        self.engine.feedback('withdraw','parent','bad')
        status=self.engine.validate_action_contract(contract,current_target={},current_evidence_bindings={'proof':digest('proof')},
            current_authority_revision_sha256=digest('a'),current_state={'revision':1})
        self.assertEqual(status['status'],'STALE')

    def test_parent_expiry_blocks_longer_lived_child(self):
        self.engine.derive('child','parent',['proof'],ttl_seconds=8000);self.seed('child')
        self.clock[0]=4601
        with self.assertRaises(ValueError):self.engine.replay_plan('child',{'revision':1},{'subject':{}})

    def test_pin_change_blocks_child(self):
        self.chain()
        with self.engine.db.transaction() as db:
            row=db.execute("SELECT body_json FROM artifacts WHERE artifact_id='parent'").fetchone()
            body=json.loads(row[0]);body['rule']['on_match']='FAIL'
            from keel_machine.common import canonical
            db.execute("UPDATE artifacts SET body_json=?,body_sha256=? WHERE artifact_id='parent'",(canonical(body),digest(body)))
        with self.assertRaises(ValueError):self.engine.runners('grandchild')

    def test_simulation_parent_cannot_grant_promoted_descendant(self):
        self.seed('parent','SIMULATION');self.engine.derive('child','parent',['proof'])
        with self.assertRaises(ValueError):self.engine.qualify('e','child',{}, {}, baseline=None,candidate=None,adjudication_validator=None)

    def test_duplicate_or_cross_family_parents_rejected(self):
        with self.assertRaises(ValueError):self.engine.derive('child','parent',['proof'],additional_parents=['parent'])
        self.engine.observe('other','other','strategy','PASS',evidence_sha256=digest('other'),context={})
        self.engine.propose_lesson('foreign','other','Other',['other'],applicability={},invalidators=[],rule=RULE)
        self.seed('foreign')
        with self.assertRaises(ValueError):self.engine.derive('child','parent',['proof'],additional_parents=['foreign'])

    def test_bad_child_pin_does_not_withdraw_valid_sibling(self):
        self.engine.derive('child','parent',['proof']);self.seed('child')
        self.engine.derive('sibling','parent',['proof']);self.seed('sibling')
        from keel_machine.common import canonical
        with self.engine.db.transaction() as db:
            row=db.execute("SELECT body_json FROM artifacts WHERE artifact_id='child'").fetchone()
            body=json.loads(row[0]);body['dependencies'][0]['body_sha256']=digest('different')
            db.execute("UPDATE artifacts SET body_json=?,body_sha256=? WHERE artifact_id='child'",(canonical(body),digest(body)))
        self.assertEqual(self.engine.lineage('child')['state'],'HELD')
        self.assertEqual(self.engine.lineage('parent')['state'],'PROMOTED')
        self.assertFalse(self.engine.replay_plan('sibling',{'revision':1},{'subject':{}})['simulation_only'])

    def test_all_ancestor_applicability_and_invalidators_are_inherited(self):
        self.engine.propose_lesson('wide','family','Wide',['proof'],applicability={},invalidators=[],rule=RULE)
        self.engine.propose_lesson('narrow','family','Narrow',['proof'],applicability={'board':'greenhouse'},invalidators=['closed'],rule=RULE)
        self.seed('wide');self.seed('narrow')
        self.engine.derive('lesson','wide',['proof'],additional_parents=['narrow']);self.seed('lesson')
        wrong=self.engine.retrieve('family',{'board':'lever','closed':True})
        self.assertNotIn('lesson',{r['artifact_id'] for r in wrong['lessons']})
        self.assertIn('lesson',{r['artifact_id'] for r in self.engine.retrieve('family',{'board':'greenhouse'})['lessons']})

    def test_incompatible_parent_preconditions_rejected(self):
        self.engine.propose_procedure('other','family',[{'operation':'evaluate_rule'}],['proof'],
            required_state={'revision':2},mutable_inputs=['subject'],validators=[digest('v')],rule=RULE)
        self.seed('other')
        with self.assertRaisesRegex(ValueError,'precondition_conflict'):
            self.engine.derive('child','parent',['proof'],additional_parents=['other'])

    def test_corrupt_promoted_root_does_not_break_healthy_retrieval(self):
        for name in ('healthy','corrupt'):
            self.engine.propose_lesson(name,'family','Statement',['proof'],applicability={},invalidators=[],rule=RULE)
            self.seed(name)
        with self.engine.db.transaction() as db:db.execute("UPDATE artifacts SET body_json=? WHERE artifact_id='corrupt'",(b'[]',))
        lessons=self.engine.retrieve('family',{})['lessons']
        self.assertEqual([r['artifact_id'] for r in lessons],['healthy'])
        self.assertEqual(self.engine.lineage('healthy')['state'],'PROMOTED')

    def test_self_or_reused_identity_cannot_create_cycle(self):
        with self.assertRaises(ValueError):self.engine.derive('parent','parent',['proof'])
        self.chain()
        with self.assertRaises(ValueError):self.engine.derive('parent','grandchild',['proof'])

    def test_rejected_parent_cannot_be_derived(self):
        self.engine.retire('parent','retired')
        with self.assertRaises(ValueError):self.engine.derive('child','parent',['proof'])

    def test_derivation_rechecks_parent_at_commit(self):
        original=self.engine._propose
        def revoke(*args,**kwargs):
            self.engine.retire('parent','withdrawn')
            return original(*args,**kwargs)
        with patch.object(self.engine,'_propose',side_effect=revoke):
            with self.assertRaises(ValueError):self.engine.derive('child','parent',['proof'])
        self.assertNotIn('PROCEDURE:CANDIDATE',self.engine.status()['artifacts'])

    def test_sixty_four_ancestors_accepted_sixty_five_rejected(self):
        # MAX_ANCESTORS=64 bounds the ancestor set, not the traversal: the
        # candidate itself must not consume one of the 64 slots.
        previous='parent'
        for index in range(1,65):
            name='chain%d'%index
            self.engine.derive(name,previous,['proof']);self.seed(name)
            previous=name
        with self.engine.db.transaction() as db:
            row=db.execute("SELECT body_json FROM artifacts WHERE artifact_id='chain64'").fetchone()
            ancestors=self.engine._validate_dependencies(db,json.loads(row['body_json']),'chain64')
        self.assertEqual(len(ancestors),64)
        # One more link exceeds the boundary: deriving the final descendant
        # raises lineage_ancestor_limit.
        with self.assertRaisesRegex(ValueError,'lineage_ancestor_limit'):
            self.engine.derive('chain65','chain64',['proof'])


if __name__=='__main__':unittest.main()
