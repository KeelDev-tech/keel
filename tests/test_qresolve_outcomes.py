"""Synthetic local READY-observation tests; no production sweep attribution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engines'))
import fit_policy
import keel_paths
import qresolve_outcomes as outcomes
import qresolve_policy
import queue_io


class SupplyOutcomesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = datetime(2026, 9, 30, 1, tzinfo=timezone.utc)
        self.floor = fit_policy.main_floor()
        self.old_lock = queue_io.get_lock_path()
        queue_io.set_lock_path(str(self.home / 'hidden_files/queue.lock'))
        self.addCleanup(queue_io.set_lock_path, self.old_lock)
        self.home_patch = mock.patch.object(keel_paths, 'HOME', str(self.home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        for queue in outcomes.QUEUES:
            self.write(f'data/queues/{queue}-queue.json', [])
        self.write('data/application-ledger.json', [])
        self.write('data/answer_bank.json', {'answers': {'email': 'private-answer@example.com'}})
        policy = self.home / 'data/employer-blocklist.md'
        policy.write_text('# Blocked employers\n', encoding='utf-8')
        (self.home / 'hidden_files').mkdir()
        self.row = {'role_id': 'private-role', 'company': 'Example', 'title': 'Example Role',
                    'fit_score': self.floor, 'status': 'READY', 'action_band': 'APPLY',
                    'ats_url': 'https://jobs.example.org/private-role', 'unresolved': [],
                    'queue_notes': [], 'status_reason': 'posting verification complete'}
        self.write('data/queues/standard-queue.json', [self.row])
        self.records = self.receipt(self.row, resolved_at=self.now - timedelta(hours=1))
        self.journal(self.records)

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def journal(self, records, relative='hidden_files/qresolve-resolutions.jsonl'):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')

    def receipt(self, row, *, decision='a', removed=1, resolved_at=None, legacy=False):
        resolved_at = resolved_at or self.now - timedelta(hours=1)
        rid = row['role_id']
        evidence = [{'source': 'data/answer_bank.json', 'pointer': '/answers/email'}]
        common = {'decision_id': decision * 64, 'fingerprint': 'b' * 16,
                  'card_key': 'fixture-card', 'answer': 'private-answer@example.com',
                  'bank_key': 'email', 'evidence': evidence,
                  'context_sha256': 'c' * 64, 'evidence_sha256': outcomes._digest(evidence),
                  'config_sha256': 'd' * 64, 'target_role_ids': [rid]}
        state = {'fit_score': self.floor, 'fit_eligible': True, 'fit_floor': self.floor,
                 'fully_unblocked': True,
                 'remaining_blockers': 0, 'status': 'PARKED-PENDING-VERIFICATION',
                 'status_updated': None, 'identity_sha256': qresolve_policy.identity_digest(row)}
        if not legacy:
            common['target_resolution_state'] = {rid: state}
        counts = {'changed_leads': 1, 'removed_blockers': removed, 'fully_unblocked': 1,
                  'role_ids': [rid]}
        intent = {**common, 'action': 'INTENT', 'ts': (resolved_at - timedelta(seconds=1)).isoformat(),
                  'planned_counts': counts}
        completion = {**common, **counts, 'action': 'auto_applied',
                      'ts': resolved_at.isoformat(), 'target_post_sha256': {rid: 'e' * 64}}
        return [intent, completion]

    def scan(self, *, live=False, now=None):
        with mock.patch('socket.socket', side_effect=AssertionError('network forbidden')):
            return outcomes.observe(self.home, live=live, now=now or self.now)

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.home.rglob('*') if path.is_file()}

    def canonical(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in (self.home / 'data').rglob('*') if path.is_file()}

    def test_dry_without_lock_is_unconfirmed_and_creates_nothing(self):
        before = self.snapshot()
        value = self.scan()
        self.assertEqual(value['status'], 'UNCONFIRMED', value)
        self.assertEqual(value['observation_writes'], 0)
        self.assertFalse(value['coverage']['committed_queue_observation'])
        self.assertEqual(value['resolved_to_ready_24h']['observed_ready_within_24h'], 0)
        self.assertEqual(self.snapshot(), before)

    def test_live_records_only_metadata_and_repeated_observation_deduplicates(self):
        before = self.canonical()
        value = self.scan(live=True)
        self.assertEqual(value['status'], 'OK', value)
        self.assertEqual(value['observation_writes'], 1)
        self.assertEqual(value['resolved_to_ready_24h']['observed_ready_within_24h'], 1)
        self.assertEqual(value['canonical_writes'], 0)
        self.assertEqual(self.canonical(), before)
        journal = self.home / outcomes.OBSERVATIONS
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        original = journal.read_bytes()
        self.assertEqual(self.scan(live=True)['observation_writes'], 0)
        self.assertEqual(journal.read_bytes(), original)
        before = self.snapshot()
        self.assertEqual(self.scan()['status'], 'OK')
        self.assertEqual(self.snapshot(), before)

    def test_durable_observation_survives_later_queue_state_and_policy_change(self):
        self.assertEqual(self.scan(live=True)['observation_writes'], 1)
        self.row['status'] = 'DEAD'
        self.write('data/queues/standard-queue.json', [self.row])
        later = self.now + timedelta(days=2)
        with mock.patch('queue_intake.FIT_BAR', self.floor + 1):
            value = self.scan(now=later)
        self.assertEqual(value['status'], 'OK', value)
        self.assertEqual(value['resolved_to_ready_24h']['observed_ready_within_24h'], 1)
        self.assertFalse(value['leads'][0]['fit_eligible'])
        self.assertEqual(value['resolved_to_ready_24h']['confirmed_not_ready_within_24h'], None)
        self.assertFalse(value['execution_authorized'])
        self.assertIsNone(value['coverage']['executable_ready'])

    def test_late_ready_and_legacy_receipt_are_unknown_not_zero_failure(self):
        late = self.now + timedelta(days=2)
        value = self.scan(live=True, now=late)
        self.assertEqual(value['observation_writes'], 0)
        self.assertEqual(value['resolved_to_ready_24h']['unknown'], 1)
        self.assertEqual(value['leads'][0]['resolved_to_ready_24h'], 'unknown_not_observed_within_24h')
        self.journal(self.receipt(self.row, legacy=True))
        value = self.scan(live=True)
        self.assertEqual(value['status'], 'OK', value)
        self.assertEqual(value['observation_writes'], 0)
        self.assertEqual(value['leads'][0]['resolved_to_ready_24h'], 'unknown_legacy_resolution_snapshot')

    def test_blockers_distinct_leads_and_repeated_receipts_are_separate(self):
        first = self.receipt(self.row, removed=3)
        second = self.receipt(self.row, decision='f', removed=2, resolved_at=self.now - timedelta(minutes=10))
        self.journal(first + second)
        value = self.scan(live=True)
        self.assertEqual(value['status'], 'OK', value)
        counts = value['counts']
        self.assertEqual((counts['completed_decisions'], counts['cleared_blockers'],
                          counts['resolved_distinct_leads'], counts['resolution_role_pairs']), (2, 5, 1, 2))
        self.assertEqual(value['observation_writes'], 2)
        self.assertEqual(value['resolved_to_ready_24h']['observed_ready_within_24h'], 1)

    def test_replayed_closure_is_invalid_to_match_canonical_resolver(self):
        self.journal(self.records + self.records)
        value = self.scan(live=True)
        self.assertIn('invalid_resolution_journal', value['errors'])
        self.assertIsNone(value['counts']['cleared_blockers'])
        self.assertEqual(value['observation_writes'], 0)

    def test_partial_then_fully_unblocked_resolution_enters_cohort_once(self):
        partial = self.receipt(self.row, resolved_at=self.now - timedelta(hours=30))
        for item in partial:
            item['target_resolution_state'][self.row['role_id']]['remaining_blockers'] = 1
            item['target_resolution_state'][self.row['role_id']]['fully_unblocked'] = False
        partial[0]['planned_counts']['fully_unblocked'] = 0
        partial[1]['fully_unblocked'] = 0
        full = self.receipt(self.row, decision='f', resolved_at=self.now - timedelta(hours=1))
        self.journal(partial + full)
        value = self.scan(live=True)
        self.assertEqual(value['status'], 'OK', value)
        self.assertEqual(value['counts']['resolved_distinct_leads'], 1)
        self.assertEqual(value['counts']['cleared_blockers'], 2)
        self.assertEqual(value['resolved_to_ready_24h']['cohort_leads'], 1)
        self.assertEqual(value['resolved_to_ready_24h']['observed_ready_within_24h'], 1)
        self.assertEqual(value['observation_writes'], 1)

    def test_recovery_time_cannot_restart_an_old_resolution_window(self):
        records = self.receipt(self.row, resolved_at=self.now - timedelta(days=3))
        records[1]['action'] = 'recovered_applied'
        records[1]['ts'] = (self.now - timedelta(minutes=1)).isoformat()
        records[1]['intent_sha256'] = outcomes._digest(records[0])
        self.journal(records)
        value = self.scan(live=True)
        self.assertEqual(value['status'], 'OK', value)
        self.assertEqual(value['observation_writes'], 0)
        self.assertEqual(value['counts']['resolved_distinct_leads'], 1)
        self.assertEqual(value['resolved_to_ready_24h']['unknown'], 1)
        self.assertEqual(value['leads'][0]['resolved_to_ready_24h'], 'unknown_recovered_resolution_time')

    def test_changed_identity_below_floor_and_duplicate_rows_do_not_count(self):
        for field, value in [('company', 'Changed'), ('role_title', 'New Role'),
                             ('ats_url', 'https://jobs.example.org/changed'), ('fit_score', self.floor - 1)]:
            with self.subTest(field=field):
                row = {**self.row, field: value}
                self.write('data/queues/standard-queue.json', [row])
                result = self.scan(live=True)
                self.assertEqual(result['observation_writes'], 0, result)
        self.write('data/queues/standard-queue.json', [self.row])
        self.write('data/queues/rejected-queue.json', [self.row])
        value = self.scan(live=True)
        self.assertEqual(value['observation_writes'], 0)
        self.assertIn('duplicate_queue_identity', value['leads'][0]['stall_reasons'])

    def test_current_full_admission_holds_questions_ledger_and_policy(self):
        cases = [{'open_questions': ['question']}, {'manual_hold': True},
                 {'gates': {'no_ai': True}}, {'action_band': 'PARKED'}, {'company': ''}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.write('data/queues/standard-queue.json', [{**self.row, **fields}])
                self.assertEqual(self.scan(live=True)['observation_writes'], 0)
        self.write('data/queues/standard-queue.json', [self.row])
        self.write('data/application-ledger.json', [{'role_id': self.row['role_id'], 'status': 'UNKNOWN_OUTCOME'}])
        self.assertEqual(self.scan(live=True)['observation_writes'], 0)
        self.write('data/application-ledger.json', [])
        (self.home / 'data/employer-blocklist.md').write_text('# Blocked employers\n- Example\n')
        self.assertEqual(self.scan(live=True)['observation_writes'], 0)

    def test_receipt_with_zero_questions_but_nonrevivable_is_not_count_mismatch(self):
        records = self.receipt(self.row)
        records[0]['planned_counts']['fully_unblocked'] = 0
        records[1]['fully_unblocked'] = 0
        for item in records:
            item['target_resolution_state'][self.row['role_id']]['fully_unblocked'] = False
        self.journal(records)
        self.row['no_ai'] = True
        self.write('data/queues/standard-queue.json', [self.row])
        value = self.scan(live=True)
        self.assertEqual(value['status'], 'OK', value)
        self.assertEqual(value['observation_writes'], 0)
        self.assertEqual(value['resolved_to_ready_24h']['cohort_leads'], 0)
        self.assertEqual(value['resolved_to_ready_24h']['nonrevivable_resolution_leads'], 1)

    def test_nonempty_blockers_cannot_claim_fully_unblocked_snapshot(self):
        records = self.receipt(self.row)
        for item in records:
            item['target_resolution_state'][self.row['role_id']]['remaining_blockers'] = 1
        self.journal(records)
        self.assertIn('invalid_resolution_journal', self.scan(live=True)['errors'])

    def test_missing_queues_ledger_and_pending_intent_hold_conversion(self):
        for relative in ['data/queues/strategic-queue.json', 'data/queues/rejected-queue.json',
                         'data/application-ledger.json']:
            with self.subTest(relative=relative):
                path = self.home / relative
                original = path.read_bytes()
                path.unlink()
                value = self.scan(live=True)
                self.assertEqual(value['status'], 'HOLD', value)
                self.assertEqual(value['observation_writes'], 0)
                path.write_bytes(original)
        self.journal(self.records + [self.receipt(self.row, decision='f')[0]])
        self.assertIn('pending_resolution_intent', self.scan(live=True)['errors'])

    def test_pending_queue_transaction_does_not_recover_or_write(self):
        folder = Path(queue_io.get_lock_path() + '.transactions')
        folder.mkdir()
        (folder / 'pending.json').write_text('{}')
        before = self.snapshot()
        value = self.scan()
        self.assertEqual(value['errors'], ['pending_queue_transaction'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.scan(live=True)['errors'], ['pending_queue_transaction'])
        self.assertTrue((folder / 'pending.json').exists())

    def test_symlink_journal_policy_and_torn_tail_fail_closed(self):
        path = self.home / outcomes.OBSERVATIONS
        target = self.home / 'outside.jsonl'
        target.write_text('')
        path.symlink_to(target)
        self.assertEqual(self.scan(live=True)['status'], 'HOLD')
        self.assertEqual(target.read_text(), '')
        path.unlink()
        path.write_text('{')
        self.assertEqual(self.scan(live=True)['status'], 'HOLD')
        path.unlink()
        policy = self.home / 'data/employer-blocklist.md'
        policy.unlink()
        policy.symlink_to(target)
        self.assertEqual(self.scan(live=True)['status'], 'HOLD')

    def test_tampered_observation_and_resolution_bindings_hold(self):
        self.assertEqual(self.scan(live=True)['observation_writes'], 1)
        path = self.home / outcomes.OBSERVATIONS
        observed = json.loads(path.read_text())
        observed['identity_sha256'] = '0' * 64
        self.journal([observed], outcomes.OBSERVATIONS)
        self.assertIn('invalid_observation_journal', self.scan()['errors'])
        path.unlink()
        records = self.receipt(self.row)
        records[1]['target_resolution_state'][self.row['role_id']]['fit_eligible'] = False
        self.journal(records)
        self.assertIn('invalid_resolution_journal', self.scan()['errors'])

    def test_console_has_only_fixed_codes_and_counts(self):
        value = outcomes.console_report(self.scan(live=True))
        output = json.dumps(value)
        for private in ['private-role', 'private-answer@example.com', 'https://jobs.example.org', str(self.home)]:
            self.assertNotIn(private, output)
        self.assertNotIn('leads', value)
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(value['network_calls'], 0)

    def test_fsync_failure_does_not_claim_zero_or_durable_observation_writes(self):
        with mock.patch.object(outcomes.os, 'fsync', side_effect=OSError('fixture failure')):
            value = self.scan(live=True)
        self.assertEqual(value['status'], 'HOLD')
        self.assertIsNone(value['observation_writes'])
        self.assertEqual(value['observation_write_state'], 'unconfirmed')
        self.assertEqual(value['canonical_writes'], 0)


if __name__ == '__main__':
    unittest.main()
