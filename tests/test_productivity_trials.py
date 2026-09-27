"""Bounded trials execute real local pipeline fixtures and canonical receipts."""
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import keel_paths
import log_event
import pipeline_service as pipeline
import productivity_service as service
import productivity_trials as trials
import queue_io
from safe_io import atomic_json, digest, read_json
from keel_efficiency.ledger import ResourceLedger


class ProductivityTrialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-trials-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.now = 1800000000.0
        self.calls = []
        self.queue = self.home / 'data/queues/standard-queue.json'
        for name in pipeline.QUEUES:
            atomic_json(self.home / 'data/queues' / (name + '-queue.json'), [])
        atomic_json(self.home / 'data/application-ledger.json', [])
        atomic_json(self.home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        for context in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
                        patch.object(log_event, 'EVENTS', str(self.home / 'data/telemetry/events.jsonl')),
                        patch.object(keel_paths, 'HOME', str(self.home)),
                        patch.dict(os.environ, {'KEEL_HOME': str(self.home),
                                   'JOB_PIPELINE_HTTP_COOLDOWN_DIR': str(self.home / 'hidden_files/http-cooldowns')})):
            context.start()
            self.addCleanup(context.stop)
        self.ledger = ResourceLedger(self.home / 'budget.sqlite3')
        self.ledger.create_scope('shared', {'calls': 100, 'compute_ms': 1000000})

    def fetch(self, url, timeout):
        self.calls.append(url)
        return {'jobs': [{'id': 1, 'title': 'Synthetic Operations Manager'}]}

    def run_trial(self, trial_id='candidate', **kwargs):
        options = dict(trial_id=trial_id, ledger=self.ledger, scope_id='shared', live=True,
                       fetcher=self.fetch, target_verified=1, clock=lambda: self.now)
        options.update(kwargs)
        return trials.run_trial(self.home, **options)

    def report(self, trial_id='candidate'):
        return trials.trial_report(self.home, trial_id=trial_id, ledger=self.ledger,
                                   scope_id='shared', clock=lambda: self.now)

    def compare(self, first, second):
        return trials.compare_trials(self.home, baseline_trial_id=first, candidate_trial_id=second,
                                     ledger=self.ledger, scope_id='shared', clock=lambda: self.now)

    def manifest(self, trial_id='candidate'):
        return self.home / 'data/productivity/trials' / (digest(trial_id) + '.json')

    def test_inspection_creates_no_trial_and_makes_no_network_call(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')
        result = trials.run_trial(self.home, trial_id='preview', fetcher=self.fetch,
                                  clock=lambda: self.now)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['network_calls'], 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / 'data/productivity').exists())

    def test_actual_two_cycle_discovery_and_verification_have_separate_units(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')
        result = self.run_trial(max_cycles=2)
        self.assertEqual(result['measurement_status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['stages']['discover']['stage_results_known'], 1)
        self.assertEqual(result['stages']['verify']['stage_results_known'], 1)
        self.assertEqual(result['stages']['verify']['calls_per_stage_result'], 1)
        self.assertEqual(self.ledger.snapshot('shared')['used']['calls'], 2)
        self.assertEqual(self.report()['receipts'], result['receipts'])
        self.assertIsNone(result['credits_per_verified_completion'])
        self.assertIsNone(result['external_credit_micros'])
        self.assertFalse(result['causal_improvement_established'])
        self.assertFalse(result['submission_authorized'])

    def test_restart_replays_same_ids_without_rehashing_changed_queue_or_spending(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')
        first = self.run_trial(max_cycles=2)
        manifest = self.manifest().read_bytes()
        ledger = self.ledger.snapshot('shared')
        second = self.run_trial(max_cycles=2)
        self.assertEqual(first['initial_workload_sha256'], second['initial_workload_sha256'])
        self.assertEqual(first['receipts'], second['receipts'])
        self.assertEqual(self.manifest().read_bytes(), manifest)
        self.assertEqual(self.ledger.snapshot('shared'), ledger)
        self.assertEqual(len(self.calls), 2)

    def test_changed_limits_labels_options_or_scope_cannot_reuse_trial_id(self):
        self.run_trial(max_cycles=1)
        self.ledger.create_scope('other', {'calls': 100, 'compute_ms': 1000000})
        for change in ({'max_cycles': 2}, {'label': 'changed'}, {'max_requests': 3},
                       {'titles': ['different']}, {'scope_id': 'other'}):
            with self.subTest(change=change):
                options = {'max_cycles': 1, **change}
                with self.assertRaisesRegex(ValueError, 'binding_conflict'):
                    self.run_trial(**options)
        self.assertEqual(self.calls, [])

    def test_invalid_cycle_counts_and_missing_budget_fail_before_manifest(self):
        for value in (0, 101, True, 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'cycle_limit'):
                self.run_trial(max_cycles=value)
        with self.assertRaises(ValueError):
            trials.run_trial(self.home, trial_id='unbudgeted', live=True)
        self.assertFalse((self.home / 'data/productivity').exists())

    def test_idle_stops_once_and_explicitly_labels_undispatched_tail(self):
        first = self.run_trial(max_cycles=4)
        self.assertEqual(first['measurement_status'], 'STOPPED')
        self.assertEqual(first['stop_reason'], 'IDLE')
        self.assertEqual(first['receipt_runs'], 1)
        self.assertEqual(len(first['planned_not_dispatched']), 3)
        self.assertEqual(first['missing_run_ids'], [])
        self.assertEqual(first['stages']['idle']['stage_results_known'], 0)
        self.assertIsNone(first['stages']['idle']['calls_per_stage_result'])
        self.assertEqual(self.run_trial(max_cycles=4)['receipts'], first['receipts'])
        self.assertEqual(self.calls, [])

    def test_budget_stop_has_no_fabricated_receipt_or_outcome(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')
        self.ledger.create_scope('tiny', {'calls': 1, 'compute_ms': 100000})
        result = self.run_trial(max_cycles=2, scope_id='tiny', max_requests=2)
        self.assertEqual(result['stop_reason'], 'HELD_BUDGET')
        self.assertEqual(result['measurement_status'], 'INCONCLUSIVE')
        self.assertEqual(result['stop_evidence'], 'local_stop_record_only')
        self.assertEqual(result['receipt_runs'], 0)
        self.assertEqual(len(result['planned_not_dispatched']), 2)
        self.assertEqual(result['stages'], {})
        self.assertEqual(self.calls, [])

    def test_http429_stops_future_cycles_and_never_invents_progress(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')

        def limited(url, timeout):
            self.calls.append(url)
            raise HTTPError(url, 429, 'synthetic', {}, None)

        result = self.run_trial(max_cycles=5, fetcher=limited)
        self.assertEqual(result['stop_reason'], 'HELD_HTTP_429')
        self.assertEqual(result['stages']['discover']['calls'], 1)
        self.assertEqual(result['stages']['discover']['stage_results_known'], 0)
        self.assertEqual(len(result['planned_not_dispatched']), 4)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_outcome_retains_cost_and_makes_cohort_inconclusive(self):
        atomic_json(self.queue, [{'role_id': 'R', 'status': 'PARKED',
                                 'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/1'}])
        original = pipeline.verify

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('synthetic interruption after actual queue effect')

        with patch.object(pipeline, 'verify', side_effect=interrupted):
            result = self.run_trial(max_cycles=2)
        self.assertEqual(result['measurement_status'], 'INCONCLUSIVE')
        self.assertEqual(result['stages']['verify']['calls'], 1)
        self.assertEqual(result['stages']['verify']['unknown_progress_runs'], 1)
        self.assertIsNone(result['stages']['verify']['calls_per_stage_result'])
        self.assertEqual(len(result['unknown_outcome_run_ids']), 1)

    def test_interruption_before_any_cycle_leaves_missing_predeclared_cohort(self):
        with patch.object(service, 'run_once', side_effect=SystemExit('synthetic interruption')):
            with self.assertRaises(SystemExit):
                self.run_trial(max_cycles=2)
        report = self.report()
        self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
        self.assertEqual(len(report['missing_run_ids']), 2)
        self.assertEqual(report['planned_not_dispatched'], [])
        self.assertEqual(report['stages'], {})

    def test_recorded_unsettled_receipt_retains_known_cost_without_complete_claim(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')
        with patch.object(service, '_settle_recorded', side_effect=SystemExit('synthetic settlement interruption')):
            with self.assertRaises(SystemExit):
                self.run_trial(max_cycles=1)
        report = self.report()
        self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
        self.assertEqual(report['receipts'][0]['phase'], 'RECORDED')
        self.assertEqual(report['stages']['discover']['calls'], 1)
        self.assertIsNone(report['stages']['discover']['calls_per_stage_result'])
        self.assertEqual(self.ledger.snapshot('shared')['reserved']['calls'], 8)

    def test_predeclared_id_executed_with_different_options_is_rejected(self):
        with patch.object(service, 'run_once', side_effect=SystemExit('synthetic interruption')):
            with self.assertRaises(SystemExit):
                self.run_trial(max_cycles=1)
        run_id = read_json(self.manifest())['plan']['run_ids'][0]
        service.run_once(self.home, run_id=run_id, ledger=self.ledger, scope_id='shared',
                         target_verified=999, fetcher=self.fetch, live=True, clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, 'trial_run_configuration_mismatch'):
            self.report()

    def test_trial_cannot_adopt_a_run_id_executed_before_preregistration(self):
        run_id = trials._run_ids(self.home, 'candidate', 1)[0]
        service.run_once(self.home, run_id=run_id, ledger=self.ledger, scope_id='shared',
                         target_verified=1, fetcher=self.fetch, live=True, clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, 'trial_run_id_already_exists'):
            self.run_trial(max_cycles=1)
        self.assertFalse(self.manifest().exists())

    def test_unverified_stop_record_cannot_turn_missing_work_into_complete_measurement(self):
        with patch.object(service, 'run_once', side_effect=SystemExit('synthetic interruption')):
            with self.assertRaises(SystemExit):
                self.run_trial(max_cycles=2)
        document = read_json(self.manifest())
        document['stop'] = {'run_id': document['plan']['run_ids'][0], 'index': 0, 'status': 'IDLE',
            'response_status': 'IDLE', 'receipt_sha256': None, 'at': self.now, 'reason': 'claimed_stop'}
        atomic_json(self.manifest(), document)
        report = self.report()
        self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
        self.assertEqual(report['stop_evidence'], 'local_stop_record_only')
        self.assertEqual(report['receipt_runs'], 0)
        self.assertEqual(report['stages'], {})

    def test_same_snapshot_comparison_is_descriptive_even_for_complete_cohorts(self):
        first = self.run_trial('baseline', label='baseline', max_cycles=1)
        second = self.run_trial('candidate', label='candidate', max_cycles=1)
        comparison = self.compare('baseline', 'candidate')
        self.assertEqual(first['initial_workload_sha256'], second['initial_workload_sha256'])
        self.assertTrue(comparison['comparable'])
        self.assertFalse(comparison['causal_improvement_established'])
        self.assertFalse(comparison['production_savings_proven'])
        self.assertFalse(comparison['live_external_inputs_controlled'])
        self.assertIsNone(comparison['credits_per_verified_completion'])

    def test_changed_actual_workload_is_incomparable_without_error(self):
        self.run_trial('baseline', max_cycles=1)
        pipeline.add_source(self.home, 'greenhouse:fixture')
        self.run_trial('candidate', max_cycles=1)
        comparison = self.compare('baseline', 'candidate')
        self.assertFalse(comparison['comparable'])
        self.assertIn('starting_workloads_differ', comparison['reasons'])
        self.assertEqual(comparison['candidate']['stages']['discover']['calls'], 1)
        self.assertEqual(comparison['stage_differences'], {})

    def test_overlap_and_filter_changes_are_not_comparable(self):
        self.run_trial('baseline', max_cycles=1)
        self.assertIn('run_cohorts_overlap', self.compare('baseline', 'baseline')['reasons'])
        self.run_trial('candidate', max_cycles=1, titles=['different cohort'])
        comparison = self.compare('baseline', 'candidate')
        self.assertIn('work_defining_options_differ', comparison['reasons'])
        self.assertFalse(comparison['comparable'])

    def test_missing_or_changed_accounting_cannot_validate_report_json(self):
        pipeline.add_source(self.home, 'greenhouse:fixture')
        result = self.run_trial(max_cycles=1)
        request_id = result['receipts'][0]['request_id']
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('DELETE FROM efficiency_requests WHERE request_id=?', (request_id,))
        with self.assertRaises(ValueError):
            self.report()

    def test_private_plan_rejects_symlink_and_changed_content(self):
        self.run_trial(max_cycles=1)
        path = self.manifest()
        original = read_json(path)
        changed = {**original, 'plan': {**original['plan'], 'label': 'forged'}}
        atomic_json(path, changed)
        with self.assertRaisesRegex(ValueError, 'digest_invalid'):
            self.report()
        atomic_json(path, original)
        moved = path.with_suffix('.saved')
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(OSError):
            self.report()


if __name__ == '__main__':
    unittest.main()
