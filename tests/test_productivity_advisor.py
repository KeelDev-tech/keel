"""Canonical, offline advice cannot spend resources or weaken admission."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import keel_paths
import log_event
import pipeline_service as pipeline
import productivity_advisor as advisor
import productivity_service as service
import productivity_trials as trials
import queue_io
from keel_efficiency.ledger import ResourceLedger
from safe_io import atomic_json, digest


class ProductivityAdvisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-advice-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.now = time.time() + 1
        self.calls = []
        self.queue = self.home / 'data/queues/standard-queue.json'
        for name in pipeline.QUEUES:
            atomic_json(self.home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        atomic_json(self.home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        for context in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
                        patch.object(log_event, 'EVENTS', str(self.home / 'data/telemetry/events.jsonl')),
                        patch.object(keel_paths, 'HOME', str(self.home)),
                        patch.dict(os.environ, {'KEEL_HOME': str(self.home),
                            'JOB_PIPELINE_HTTP_COOLDOWN_DIR': str(self.home / 'hidden_files/http-cooldowns')})):
            contexts.enter_context(context)
        self.ledger = ResourceLedger(self.home / 'budget.sqlite3')
        self.ledger.create_scope('shared', {'calls': 100, 'compute_ms': 1000000})

    def fetch(self, url, timeout):
        self.calls.append(url)
        return {'jobs': [{'id': 1, 'title': 'Synthetic Operations Manager'}]}

    def run_trial(self, trial_id='candidate', **changes):
        options = {'trial_id': trial_id, 'ledger': self.ledger, 'scope_id': 'shared', 'max_cycles': 1,
                   'live': True, 'fetcher': self.fetch, 'clock': lambda: self.now}
        options.update(changes)
        return trials.run_trial(self.home, **options)

    def advise(self, **changes):
        options = {'trial_id': 'candidate', 'ledger': self.ledger, 'scope_id': 'shared',
                   'clock': lambda: self.now}
        options.update(changes)
        return advisor.diagnose(self.home, **options)

    def source(self):
        pipeline.add_source(self.home, 'greenhouse:synthetic')

    def business_files(self):
        return {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob('*')
                if path.is_file() and not path.name.endswith(('-wal', '-shm'))}

    def codes(self, result):
        return {item['code'] for item in result['diagnosis']}

    def test_complete_discovery_proposes_bounded_verification_without_new_authority(self):
        self.source()
        trial = self.run_trial()
        result = self.advise()
        self.assertEqual(result['status'], 'PROPOSAL')
        proposal = result['next_trial_proposal']
        self.assertEqual(proposal['max_cycles'], 3)
        self.assertEqual(proposal['numeric_options']['max_requests'], 8)
        self.assertEqual(proposal['transport'], 'injected')
        self.assertTrue(proposal['trusted_in_process_adapter_required'])
        self.assertTrue(proposal['reuse_prior_configuration'])
        self.assertEqual(proposal['source_plan_sha256'], trial['plan_sha256'])
        self.assertIn('discover_measured_progress', self.codes(result))
        self.assertEqual(result['evidence']['stage_measurements']['discover']['result_unit'], 'deduplicated_lead_added')
        for key in ('execution_authorized', 'submission_authorized', 'budget_reserved',
                    'production_savings_proven', 'causal_improvement_established', 'coherent_cross_store_snapshot'):
            self.assertFalse(result[key])
        self.assertIsNone(result['external_credit_micros'])
        self.assertIsNone(result['application_completions_verified'])
        self.assertEqual(len(self.calls), 1)

    def test_readonly_reopens_ledger_and_performs_no_business_writes_or_network(self):
        self.source()
        self.run_trial()
        before = self.business_files()
        checkpoint = self.ledger.checkpoint()
        with (patch.object(ResourceLedger, '__init__', side_effect=AssertionError('constructor forbidden')),
              patch.object(ResourceLedger, 'reserve', side_effect=AssertionError('reserve forbidden')),
              patch.object(ResourceLedger, 'settle', side_effect=AssertionError('settle forbidden')),
              patch.object(pipeline, 'supply_report', side_effect=AssertionError('writer lock forbidden')),
              patch('socket.create_connection', side_effect=AssertionError('network forbidden')),
              patch('socket.getaddrinfo', side_effect=AssertionError('network forbidden'))):
            result = self.advise()
        self.assertEqual(result['status'], 'PROPOSAL')
        self.assertEqual(before, self.business_files())
        self.assertEqual(checkpoint, self.ledger.checkpoint())

    def test_ancestor_remaining_budget_and_reservations_limit_whole_candidate(self):
        self.ledger.create_scope('parent', {'calls': 20, 'compute_ms': 1000000})
        self.ledger.create_scope('child', {'calls': 100, 'compute_ms': 1000000}, parent_id='parent')
        self.source()
        self.run_trial(scope_id='child')
        self.ledger.reserve('other-work', 'parent', {'calls': 3})
        result = self.advise(scope_id='child', max_cycles=100)
        self.assertEqual(result['status'], 'PROPOSAL')
        self.assertEqual(result['resource_envelope']['available_across_ancestors']['calls'], 16)
        self.assertEqual(result['next_trial_proposal']['max_cycles'], 2)
        self.assertEqual(result['resource_envelope']['total_candidate_estimate']['calls'], 16)

    def test_compute_allowance_limits_cycles_independently_of_calls(self):
        self.ledger.create_scope('tight', {'calls': 1000, 'compute_ms': 65000})
        self.source()
        self.run_trial(scope_id='tight')
        result = self.advise(scope_id='tight', max_cycles=100)
        self.assertEqual(result['status'], 'PROPOSAL')
        self.assertEqual(result['next_trial_proposal']['max_cycles'], 2)
        self.assertEqual(result['resource_envelope']['total_candidate_estimate']['compute_ms'], 60000)

    def test_locked_ancestor_never_proposes_even_when_child_looks_available(self):
        self.ledger.create_scope('parent', {'calls': 100, 'compute_ms': 1000000})
        self.ledger.create_scope('child', {'calls': 100, 'compute_ms': 1000000}, parent_id='parent')
        self.source()
        self.run_trial(scope_id='child')
        self.ledger.reserve('overrun', 'parent', {'calls': 1})
        self.ledger.mark_dispatched('overrun')
        self.ledger.settle('overrun', {'calls': 2})
        result = self.advise(scope_id='child')
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('shared_budget_locked', self.codes(result))
        self.assertIsNone(result['next_trial_proposal'])

    def test_insufficient_budget_does_not_shrink_filters_or_raise_limits(self):
        self.ledger.create_scope('tight', {'calls': 8, 'compute_ms': 1000000})
        self.source()
        self.run_trial(scope_id='tight')
        result = self.advise(scope_id='tight')
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(result['resource_envelope']['cycles_fitting_observed_budget'], 0)
        self.assertIsNone(result['next_trial_proposal'])

    def test_actual_prior_limits_override_preflight_defaults(self):
        self.ledger.create_scope('tight', {'calls': 7, 'compute_ms': 1000000})
        self.source()
        self.run_trial(scope_id='tight', max_requests=2, timeout=5)
        result = self.advise(scope_id='tight')
        self.assertEqual(result['status'], 'PROPOSAL')
        self.assertEqual(result['next_trial_proposal']['max_cycles'], 3)
        self.assertEqual(result['next_trial_proposal']['numeric_options']['max_requests'], 2)

    def test_exact_target_met_yields_no_spend_after_clean_canonical_stop(self):
        self.source()
        self.run_trial(max_cycles=4, target_verified=1)
        result = self.advise()
        self.assertEqual(result['status'], 'NO_SPEND')
        self.assertEqual(result['evidence']['measurement_status'], 'STOPPED')
        self.assertIn('verified_posting_target_met', self.codes(result))
        self.assertIsNone(result['next_trial_proposal'])
        self.assertEqual(result['evidence']['stage_measurements']['verify']['result_unit'], 'committed_live_posting_presence')

    def test_empty_sources_clean_idle_is_no_spend_not_inconclusive(self):
        self.run_trial(max_cycles=3)
        result = self.advise()
        self.assertEqual(result['status'], 'NO_SPEND')
        self.assertIn('no_registered_sources', self.codes(result))
        self.assertEqual(result['evidence']['measurement_status'], 'STOPPED')
        self.assertIsNone(result['next_trial_proposal'])

    def test_stopped_without_canonical_receipt_stays_inconclusive(self):
        self.ledger.create_scope('empty', {})
        self.run_trial(scope_id='empty')
        result = self.advise(scope_id='empty')
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(result['evidence']['measurement_status'], 'INCONCLUSIVE')
        self.assertIn('trial_measurements_incomplete', self.codes(result))

    def test_interrupted_trial_missing_run_ids_never_become_successes(self):
        self.source()
        original = service.run_once
        count = 0
        def stop_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise SystemExit('fixture interruption')
            return original(*args, **kwargs)
        with patch.object(service, 'run_once', side_effect=stop_second):
            with self.assertRaises(SystemExit):
                self.run_trial(max_cycles=3)
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('trial_measurements_incomplete', self.codes(result))
        self.assertIsNone(result['next_trial_proposal'])
        self.assertIsNone(result['evidence']['stage_measurements']['discover']['calls_per_stage_result'])

    def test_unknown_progress_is_held_and_never_divided_into_efficiency(self):
        self.source()
        with patch.object(service.source_scheduler, 'scheduled_discover', side_effect=RuntimeError('PRIVATE-SEED')):
            self.run_trial(max_cycles=2)
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('trial_measurements_incomplete', self.codes(result))
        self.assertNotIn('PRIVATE-SEED', json.dumps(result))
        self.assertIsNone(result['next_trial_proposal'])

    def test_recovery_acknowledgment_does_not_promote_unknown_trial_measurement(self):
        self.source()
        with patch.object(service.source_scheduler, 'scheduled_discover', side_effect=RuntimeError('fixture')):
            trial = self.run_trial(max_cycles=2)
        service.recover(self.home, run_id=trial['receipts'][0]['run_id'], ledger=self.ledger,
                        scope_id='shared', clock=lambda: self.now)
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(result['evidence']['measurement_status'], 'INCONCLUSIVE')
        self.assertIsNone(result['next_trial_proposal'])

    def test_pending_run_in_another_scope_blocks_followup_of_complete_trial(self):
        self.source()
        self.run_trial()
        self.ledger.create_scope('other', {'calls': 100, 'compute_ms': 1000000})
        with patch.object(pipeline, 'flush_outbox', side_effect=KeyboardInterrupt('fixture')):
            with self.assertRaises(KeyboardInterrupt):
                service.run_once(self.home, run_id='other-pending', ledger=self.ledger, scope_id='other',
                                 live=True, fetcher=self.fetch, clock=lambda: self.now)
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('host_observation_blocked', self.codes(result))
        self.assertIsNone(result['next_trial_proposal'])

    def test_zero_yield_never_invents_source_cause_or_efficiency_gain(self):
        self.source()
        self.run_trial(fetcher=lambda url, timeout: {'jobs': []})
        result = self.advise()
        self.assertEqual(result['status'], 'NO_SPEND')
        self.assertIn('discover_zero_measured_yield', self.codes(result))
        self.assertFalse(result['evidence']['source_attribution_available'])
        self.assertIsNone(result['next_trial_proposal'])
        self.assertFalse(result['causal_improvement_established'])

    def test_private_labels_filters_paths_and_source_urls_not_copied_into_advice(self):
        self.source()
        marker = 'PRIVATE-SEED https://private.example.com/filter'
        self.run_trial(label=marker, titles=[marker],
                       fetcher=lambda url, timeout: {'jobs': [{'id': 1, 'title': marker}]})
        result = self.advise()
        self.assertEqual(result['status'], 'PROPOSAL')
        encoded = json.dumps(result)
        for private in ('PRIVATE-SEED', 'private.example.com', str(self.home), 'greenhouse:synthetic'):
            self.assertNotIn(private, encoded)
        self.assertEqual(result['next_trial_proposal']['titles_sha256'], digest([marker]))

    def test_replaced_or_wrong_scope_ledger_cannot_supply_evidence(self):
        self.source()
        self.run_trial()
        self.ledger.create_scope('other', {'calls': 100, 'compute_ms': 1000000})
        result = self.advise(scope_id='other')
        self.assertEqual(result['status'], 'HOLD')
        self.assertIsNone(result['evidence'])
        self.assertIsNone(result['next_trial_proposal'])

    def test_replaced_ledger_instance_fails_the_history_anchor(self):
        self.source()
        self.run_trial()
        replacement = ResourceLedger(self.home / 'replacement.sqlite3')
        replacement.create_scope('shared', {'calls': 100, 'compute_ms': 1000000})
        Path(self.ledger.path).write_bytes(Path(replacement.path).read_bytes())
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertIsNone(result['evidence'])

    def test_corrupt_canonical_receipt_held_without_echoing_content(self):
        self.source()
        self.run_trial()
        path = self.home / 'data/productivity/history.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE runs SET body=?', ('PRIVATE-SEED',))
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertIsNone(result['evidence'])
        self.assertNotIn('PRIVATE-SEED', json.dumps(result))

    def test_budget_change_during_observation_discards_partial_proposal(self):
        self.source()
        self.run_trial()
        original = advisor._observation
        calls = 0
        def concurrent(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.ledger.reserve('concurrent', 'shared', {'calls': 1})
            return original(*args, **kwargs)
        with patch.object(advisor, '_observation', side_effect=concurrent):
            result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertIsNone(result['evidence'])
        self.assertIsNone(result['resource_envelope'])
        self.assertIsNone(result['next_trial_proposal'])

    def test_build_change_requires_requalification_before_followup(self):
        self.source()
        self.run_trial()
        with patch.object(trials, '_build', return_value={'changed': '0' * 64}):
            result = self.advise()
        self.assertEqual(result['status'], 'NO_SPEND')
        self.assertIn('runtime_build_changed', self.codes(result))
        self.assertIsNone(result['next_trial_proposal'])

    def test_missing_trial_or_ledger_cannot_create_state(self):
        before = self.business_files()
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(before, self.business_files())
        self.ledger.path = str(self.home / 'absent.sqlite3')
        result = self.advise()
        self.assertEqual(result['status'], 'HOLD')
        self.assertFalse(Path(self.ledger.path).exists())

    def test_invalid_advice_limits_rejected_before_reads(self):
        for invalid in (0, -1, 101, True, 1.0, '3'):
            with self.subTest(limit=invalid), self.assertRaisesRegex(ValueError, 'advice_cycle_limit_invalid'):
                self.advise(max_cycles=invalid)


if __name__ == '__main__':
    unittest.main()
