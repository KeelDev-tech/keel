"""Offline regressions for registry-bound delegation and approval consumption."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time
import unittest
from unittest.mock import patch

from security.actions.approval_gate import ApprovalStore
from security.identity.agent_identity import IdentityRegistry, register_system_identity
from security.identity.capabilities import CapabilityManifest
from security.identity.delegation import Delegation, DelegationRegistry
from security.policy.engine import Decision, evaluate


class AuthorityBindingRegressions(unittest.TestCase):
    def setUp(self):
        IdentityRegistry.clear()
        DelegationRegistry.clear()
        self.grantor = register_system_identity('regression-grantor')
        self.worker = register_system_identity('regression-worker')
        self.other = register_system_identity('regression-other')

    def tearDown(self):
        IdentityRegistry.clear()
        DelegationRegistry.clear()

    def delegation(self):
        return DelegationRegistry.delegate(
            self.grantor.id, CapabilityManifest(('read_leads', 'write_packet')),
            self.worker.id, ('read_leads',))

    def evaluate(self, delegation, actor=None, action='read_leads'):
        return evaluate(
            {'identity': actor or self.worker,
             'manifest': CapabilityManifest(('read_leads', 'write_packet')),
             'delegation': delegation},
            {'name': action}, {'sensitivity': 'INTERNAL'}, {})

    def test_unregistered_delegation_rejected(self):
        forged = Delegation('invented', self.grantor.id, self.worker.id,
                            frozenset(('read_leads',)),
                            expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        self.assertFalse(DelegationRegistry.is_valid(forged)[0])

    def test_stale_copy_cannot_bypass_revoke(self):
        original = self.delegation()
        stale = copy.copy(original)
        DelegationRegistry.revoke(original.id, 'regression')
        self.assertFalse(DelegationRegistry.is_valid(stale)[0])

    def test_grant_handle_cannot_expand_capabilities(self):
        grant = self.delegation()
        grant.capabilities = frozenset(('read_leads', 'write_packet'))
        self.assertFalse(DelegationRegistry.is_valid(grant)[0])

    def test_delegation_cannot_be_used_by_another_actor(self):
        self.assertEqual(self.evaluate(self.delegation(), actor=self.other).decision, Decision.DENY)

    def test_manifest_cannot_escape_delegated_subset(self):
        self.assertEqual(self.evaluate(self.delegation(), action='write_packet').decision, Decision.DENY)

    def test_revoked_delegator_invalidates_delegation(self):
        grant = self.delegation()
        IdentityRegistry.revoke(self.grantor.id, 'regression')
        self.assertEqual(self.evaluate(grant).decision, Decision.DENY)

    def test_valid_delegation_preserves_allowed_read(self):
        self.assertEqual(self.evaluate(self.delegation()).decision, Decision.ALLOW)

    def test_approval_handle_cannot_change_resource(self):
        store = ApprovalStore()
        grant = store.grant('agent', 'deploy', {'id': 'approved'}, 'operator')
        from security.actions.approval_gate import digest_resource
        grant.resource_digest = digest_resource({'id': 'different'})
        self.assertIsNone(store.find_match('agent', 'deploy', {'id': 'different'}))

    def test_consumed_handle_cannot_reset_use(self):
        store = ApprovalStore()
        grant = store.grant('agent', 'deploy', {'id': 'approved'}, 'operator')
        used = store.consume(grant.approval_id)
        used.used = False
        used.used_at = ''
        self.assertIsNone(store.consume(grant.approval_id))

    def test_approval_concurrent_consumption_has_one_winner(self):
        store = ApprovalStore()
        grant = store.grant('agent', 'deploy', {'id': 'approved'}, 'operator')
        now = datetime.now(timezone.utc)
        def slow_clock():
            # Widen the pre-existing check/set race without network or files.
            time.sleep(0.003)
            return now
        with patch('security.actions.approval_gate._utcnow', side_effect=slow_clock):
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda _: store.consume(grant.approval_id), range(32)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_naive_approval_expiry_fails_closed(self):
        store = ApprovalStore()
        grant = store.grant('agent', 'deploy', {'id': 'approved'}, 'operator')
        grant.expires_at = '2099-01-01T00:00:00'
        self.assertIsNone(store.find_match('agent', 'deploy', {'id': 'approved'}))

    def test_naive_delegation_expiry_fails_closed(self):
        grant = self.delegation()
        grant.expires_at = '2099-01-01T00:00:00'
        self.assertFalse(DelegationRegistry.is_valid(grant)[0])


if __name__ == '__main__':
    unittest.main()
