"""Exact provider receipt projection, entirely local synthetic host fixtures."""
import copy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import keel_paths
import log_event
import pipeline_service as pipeline
import queue_io
import receipt_projection as projection
import submit_intent
from outcome_tracking.receipt_intake import ReceiptStore, ProviderValidation
from safe_io import atomic_json, digest, read_json, utc_now


class ReceiptProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-receipt-projection-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.now = utc_now()
        self.queue = self.home / 'data/queues/standard-queue.json'
        self.ledger = self.home / 'data/application-ledger.json'
        self.intents = self.home / 'hidden_files/submit-intents.json'
        self.receipts = self.home / 'data/receipt-observations.json'
        self.events = self.home / 'data/telemetry/events.jsonl'
        self.url = 'https://job-boards.greenhouse.io/fixture/jobs/1'
        self.claim = {'role_id': 'R', 'attempt_id': 'A', 'application_id': 'APP',
                      'attempt_id_source': 'writer', 'status': 'SUBMISSION_CLAIMED',
                      'date_submitted': (self.now - timedelta(minutes=3)).isoformat(),
                      'application_url': self.url, 'company': 'Synthetic'}
        self.entry = {'role_id': 'R', 'attempt_id': 'A', 'application_id': 'APP',
                      'application_url': self.url, 'status': 'PARKED-PENDING-VERIFICATION',
                      'holds': [], 'approval': {'seven_family_revisions': 'preserve'},
                      'verification_next_at': 'retain', 'execution_authorized': False,
                      'materials': {'resume': 'data/synthetic.txt'}}
        self.intent = {'role_id': 'R', 'attempt_id': 'A', 'application_id': 'APP',
                       'application_url': self.url, 'state': 'SUBMITTED',
                       'bundle_digest': 'a' * 64, 'page_ref': {}}
        for name in pipeline.QUEUES:
            atomic_json(self.home / f'data/queues/{name}-queue.json', [])
        atomic_json(self.queue, {'entries': [self.entry], 'version': 3, 'metadata': {'preserve': True}})
        atomic_json(self.ledger, {'rows': [self.claim], 'version': 1})
        atomic_json(self.intents, {'A': self.intent})
        self.record = {'kind': 'provider_receipt', 'source': 'synthetic-provider', 'receipt_id': 'receipt-1',
                       'role_id': 'R', 'attempt_id': 'A', 'application_id': 'APP',
                       'outcome': 'AUTO_ACK', 'content_sha256': 'c' * 64,
                       'received_at': (self.now - timedelta(minutes=2)).isoformat(),
                       'recorded_at': (self.now - timedelta(minutes=1)).isoformat()}
        ReceiptStore(self.receipts).put(self.record, now=self.now)
        self.validations = []
        contexts = [patch.object(keel_paths, 'HOME', str(self.home)),
                    patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
                    patch.object(log_event, 'EVENTS', str(self.events)),
                    patch.object(submit_intent, 'STORE_PATH', str(self.intents)),
                    patch.object(submit_intent, 'LOCK_PATH', str(self.intents) + '.lock'),
                    patch.dict(os.environ, {'KEEL_HOME': str(self.home)})]
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def validator(self, record, claim):
        # Fixture only. A real host must independently authenticate provider,
        # account/recipient and exact attempt acceptance before returning this.
        self.validations.append((digest(record), digest(claim)))
        return ProviderValidation(digest(record), digest(claim), 'fixture-host', self.now.isoformat(), True)

    def run_projection(self, **changes):
        options = dict(role_id='R', attempt_id='A', receipts_path=self.receipts,
                       validators={'synthetic-provider': self.validator}, now=self.now, live=True)
        options.update(changes)
        return projection.reconcile(self.home, **options)

    def queue_entry(self):
        return read_json(self.queue)['entries'][0]

    def write_entry(self, entry):
        doc = read_json(self.queue)
        doc['entries'] = [entry]
        atomic_json(self.queue, doc)

    def event_rows(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_projection_changes_only_whitelisted_fields_and_one_event(self):
        protected = {key: value for key, value in self.entry.items() if key not in projection.WRITABLE}
        stores = {path: path.read_bytes() for path in (self.ledger, self.intents, self.receipts)}
        with patch.object(submit_intent, 'mark_submitted', side_effect=AssertionError('no attempt transition')):
            result = self.run_projection()
        self.assertEqual(result['status'], 'PROJECTED')
        self.assertTrue(result['provider_verified'])
        self.assertFalse(result['event_pending'])
        self.assertFalse(result['submission_authorized'])
        entry = self.queue_entry()
        self.assertEqual(entry['status'], 'SUBMITTED')
        self.assertEqual({key: value for key, value in entry.items() if key not in projection.WRITABLE}, protected)
        self.assertEqual(read_json(self.queue)['metadata'], {'preserve': True})
        self.assertNotIn(projection.OUTBOX, entry)
        self.assertEqual([row['event_type'] for row in self.event_rows()], ['pipeline_recovery'])
        for path, original in stores.items():
            self.assertEqual(path.read_bytes(), original)

    def test_no_flags_or_kind_can_supply_provider_authentication(self):
        for kind in ('provider_receipt', 'manual_attestation', 'imported_mail'):
            with self.subTest(kind=kind):
                self.receipts.unlink()
                ReceiptStore(self.receipts).put({**self.record, 'kind': kind, 'verified': True, 'authenticated': True}, now=self.now)
                before = self.queue.read_bytes()
                result = self.run_projection(validators=None)
                self.assertEqual(result['status'], 'HELD')
                self.assertFalse(result['provider_verified'])
                self.assertEqual(self.queue.read_bytes(), before)
                if kind != 'provider_receipt':
                    self.assertEqual(self.run_projection()['status'], 'HELD')
        self.assertEqual(self.validations, [])
        self.assertEqual(self.event_rows(), [])

    def test_review_is_read_only_even_with_host_validation(self):
        original = self.queue.read_bytes()
        result = self.run_projection(live=False)
        self.assertEqual(result['status'], 'ELIGIBLE')
        self.assertTrue(result['dry_run'])
        self.assertFalse(result['queue_committed'])
        self.assertEqual(self.queue.read_bytes(), original)
        self.assertEqual(self.event_rows(), [])

    def test_replay_revalidates_host_and_does_not_duplicate_event(self):
        first = self.run_projection()
        original = self.queue.read_bytes()
        second = self.run_projection()
        self.assertEqual(second['status'], 'REPLAYED')
        self.assertEqual(second['event_id'], first['event_id'])
        self.assertEqual(len(self.validations), 2)
        self.assertEqual(self.queue.read_bytes(), original)
        self.assertEqual(len(self.event_rows()), 1)
        self.assertEqual(self.run_projection(validators=None)['status'], 'HELD')

    def test_invalid_validator_outputs_do_not_project(self):
        callbacks = [lambda r, c: {'accepted': True},
                     lambda r, c: ProviderValidation('0' * 64, digest(c), 'fixture', self.now.isoformat(), True),
                     lambda r, c: ProviderValidation(digest(r), digest(c), 'fixture', self.now.isoformat(), False),
                     lambda r, c: ProviderValidation(digest(r), digest(c), 'fixture', (self.now + timedelta(seconds=1)).isoformat(), True)]
        before = self.queue.read_bytes()
        for callback in callbacks:
            with self.subTest(callback=callback):
                self.assertEqual(self.run_projection(validators={'synthetic-provider': callback})['status'], 'HELD')
        self.assertEqual(self.queue.read_bytes(), before)

    def test_unavailable_validator_is_distinct_and_preserves_queue(self):
        def unavailable(record, claim):
            raise OSError('synthetic private provider detail')
        before = self.queue.read_bytes()
        report = self.run_projection(validators={'synthetic-provider': unavailable})
        self.assertEqual(report['status'], 'HELD')
        self.assertEqual(report['reason'], 'provider_validation_unavailable')
        self.assertFalse(report['provider_verified'])
        self.assertNotIn('synthetic private provider detail', str(report))
        self.assertEqual(self.queue.read_bytes(), before)

    def test_real_clock_accepts_check_timestamp_taken_when_validator_finishes(self):
        calls = []
        def checked_now(record, claim):
            calls.append(True)
            return ProviderValidation(digest(record), digest(claim), 'fixture-host', utc_now().isoformat(), True)
        result = self.run_projection(now=None, validators={'synthetic-provider': checked_now})
        self.assertEqual(result['status'], 'PROJECTED')
        self.assertEqual(calls, [True])
        self.assertEqual(len(self.event_rows()), 1)

    def test_every_attempt_application_and_posting_binding_is_required(self):
        changes = [('queue', 'attempt_id', ''), ('queue', 'attempt_id', 'other'),
                   ('queue', 'application_id', 'other'), ('queue', 'application_url', self.url + '99'),
                   ('ledger', 'application_id', ''), ('ledger', 'attempt_id_source', 'guess'),
                   ('ledger', 'application_url', ''), ('ledger', 'status', 'UNKNOWN'),
                   ('intent', 'state', 'UNKNOWN'), ('intent', 'role_id', 'other'),
                   ('intent', 'application_id', 'other'), ('intent', 'application_url', self.url + '99')]
        original = {path: path.read_bytes() for path in (self.queue, self.ledger, self.intents)}
        for source, key, value in changes:
            with self.subTest(source=source, key=key, value=value):
                for path, raw in original.items():
                    path.write_bytes(raw)
                if source == 'queue':
                    self.write_entry({**self.entry, key: value})
                elif source == 'ledger':
                    atomic_json(self.ledger, [{**self.claim, key: value}])
                else:
                    atomic_json(self.intents, {'A': {**self.intent, key: value}})
                before = self.queue.read_bytes()
                self.assertEqual(self.run_projection()['status'], 'HELD')
                self.assertEqual(self.queue.read_bytes(), before)

    def test_all_declared_target_urls_must_agree(self):
        original = {path: path.read_bytes() for path in (self.queue, self.ledger, self.intents)}
        for source in ('queue', 'ledger', 'intent', 'page_ref'):
            for url in (self.url + '99', 'https://example.org/unknown'):
                with self.subTest(source=source, url=url):
                    for path, raw in original.items():
                        path.write_bytes(raw)
                    if source == 'queue':
                        self.write_entry({**self.entry, 'ats_url': url})
                    elif source == 'ledger':
                        atomic_json(self.ledger, [{**self.claim, 'ats_url': url}])
                    else:
                        changed = {**self.intent, **({'ats_url': url} if source == 'intent' else {'page_ref': {'application_url': url}})}
                        atomic_json(self.intents, {'A': changed})
                    self.assertEqual(self.run_projection()['status'], 'HELD')

    def test_protected_statuses_holds_and_pending_publications_are_preserved(self):
        entries = [{**self.entry, 'status': state} for state in
                   ('UNKNOWN_OUTCOME', 'REJECTED', 'DEAD', 'CANCELLED', 'WITHDRAWN', 'UNRECOGNIZED')]
        entries += [{**self.entry, field: ['retain']} for field in
                    ('holds', 'human_hold', 'revoked', 'structurally_blocked',
                     'verification_event_pending', 'reconciliation_publication')]
        for entry in entries:
            with self.subTest(entry=entry):
                self.write_entry(entry)
                before = self.queue.read_bytes()
                self.assertEqual(self.run_projection()['status'], 'HELD')
                self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(self.validations, [])

    def test_duplicate_queue_role_attempt_application_or_posting_is_held(self):
        path = self.home / 'data/queues/strategic-queue.json'
        unrelated = {**self.entry, 'role_id': 'other', 'attempt_id': 'B', 'application_id': 'APP2', 'application_url': self.url + '99'}
        for field, value in (('role_id', 'R'), ('attempt_id', 'A'), ('application_id', 'APP'), ('application_url', self.url), ('ats_url', self.url)):
            with self.subTest(field=field):
                atomic_json(path, [{**unrelated, field: value}])
                self.assertEqual(self.run_projection()['status'], 'HELD')
        # Queue application ID need not be stamped yet, but it cannot belong to
        # a different row while the ledger binds the selected attempt to it.
        unstamped = dict(self.entry)
        unstamped.pop('application_id')
        self.write_entry(unstamped)
        atomic_json(path, [{**unrelated, 'application_id': 'APP'}])
        self.assertEqual(self.run_projection()['status'], 'HELD')

    def test_duplicate_ledger_attempt_or_application_is_held(self):
        for extra in ({**self.claim, 'role_id': 'other', 'status': 'FAILED'},
                      {**self.claim, 'attempt_id': 'B', 'role_id': 'other'}):
            with self.subTest(extra=extra):
                atomic_json(self.ledger, [self.claim, extra])
                self.assertEqual(self.run_projection()['status'], 'HELD')

    def test_other_open_attempt_for_role_or_equivalent_posting_is_held(self):
        for other in ({'role_id': 'R', 'attempt_id': 'B', 'state': 'UNKNOWN'},
                      {'role_id': 'other', 'attempt_id': 'B', 'state': 'INTENT', 'application_url': self.url},
                      {'role_id': 'other', 'attempt_id': 'B', 'state': 'UNKNOWN', 'page_ref': {'posting_url': self.url}}):
            with self.subTest(other=other):
                atomic_json(self.intents, {'A': self.intent, 'B': other})
                self.assertEqual(self.run_projection()['reason'], 'other_open_attempt_preserved')

    def test_full_receipt_snapshot_conflict_overrides_other_valid_receipt(self):
        store = ReceiptStore(self.receipts)
        second = {**self.record, 'receipt_id': 'receipt-2'}
        store.put(second, now=self.now)
        store.put({**second, 'content_sha256': 'd' * 64}, now=self.now)
        before = self.queue.read_bytes()
        result = self.run_projection()
        self.assertEqual(result['reason'], 'receipt_conflict_requires_review')
        self.assertEqual(self.queue.read_bytes(), before)

    def test_wrong_attempt_or_application_receipt_cannot_authenticate(self):
        for field in ('role_id', 'attempt_id', 'application_id'):
            with self.subTest(field=field):
                self.receipts.unlink()
                ReceiptStore(self.receipts).put({**self.record, field: 'other'}, now=self.now)
                self.assertEqual(self.run_projection()['status'], 'HELD')
        self.assertEqual(self.validations, [])

    def test_validation_concurrent_retarget_is_held_without_overwrite(self):
        def changed(record, claim):
            self.write_entry({**self.entry, 'application_url': self.url + '99'})
            return self.validator(record, claim)
        result = self.run_projection(validators={'synthetic-provider': changed})
        self.assertEqual(result['reason'], 'inputs_changed_after_validation')
        self.assertEqual(self.queue_entry()['application_url'], self.url + '99')
        self.assertNotIn(projection.MARKER, self.queue_entry())

    def test_receipt_conflict_or_intent_change_during_validation_is_held(self):
        for change in ('receipt', 'intent'):
            with self.subTest(change=change):
                if change == 'receipt':
                    def changed(record, claim):
                        ReceiptStore(self.receipts).put({**self.record, 'content_sha256': 'd' * 64}, now=self.now)
                        return self.validator(record, claim)
                else:
                    self.receipts.unlink()
                    ReceiptStore(self.receipts).put(self.record, now=self.now)
                    def changed(record, claim):
                        atomic_json(self.intents, {'A': {**self.intent, 'state': 'UNKNOWN'}})
                        return self.validator(record, claim)
                self.assertEqual(self.run_projection(validators={'synthetic-provider': changed})['reason'], 'inputs_changed_after_validation')
                self.assertNotIn(projection.MARKER, self.queue_entry())

    def test_failed_queue_write_is_unknown_and_can_reconcile_fresh_evidence(self):
        before = self.queue.read_bytes()
        with patch.object(projection, 'atomic_json', side_effect=OSError('fixture disk unavailable')):
            result = self.run_projection()
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['queue_committed'])
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(self.run_projection()['status'], 'PROJECTED')
        self.assertEqual(len(self.event_rows()), 1)

    def test_crash_after_queue_replace_recovers_marker_without_duplicate_effect(self):
        def uncertain(path, document):
            atomic_json(path, document)
            raise OSError('fixture directory fsync uncertainty')
        with patch.object(projection, 'atomic_json', side_effect=uncertain):
            first = self.run_projection()
        self.assertEqual(first['status'], 'UNKNOWN')
        self.assertEqual(self.queue_entry()['status'], 'SUBMITTED')
        self.assertIn(projection.OUTBOX, self.queue_entry())
        self.assertEqual(self.event_rows(), [])
        second = self.run_projection()
        self.assertEqual(second['status'], 'REPLAYED')
        self.assertEqual(first['event_id'], second['event_id'])
        self.assertEqual(len(self.event_rows()), 1)

    def test_telemetry_failure_retains_stable_outbox_then_replays_once(self):
        with patch.object(log_event, 'log', side_effect=OSError('fixture event unavailable')):
            first = self.run_projection()
        marker = copy.deepcopy(self.queue_entry()[projection.MARKER])
        self.assertEqual(first['status'], 'PROJECTED')
        self.assertTrue(first['event_pending'])
        self.assertIn(projection.OUTBOX, self.queue_entry())
        result = self.run_projection()
        self.assertEqual(result['status'], 'REPLAYED')
        self.assertFalse(result['event_pending'])
        self.assertEqual(self.queue_entry()[projection.MARKER], marker)
        self.assertEqual(len(self.event_rows()), 1)

    def test_append_succeeded_then_error_does_not_duplicate_durable_event(self):
        real = log_event.log
        def interrupted(**kwargs):
            real(**kwargs)
            raise OSError('fixture response lost after event fsync')
        with patch.object(log_event, 'log', side_effect=interrupted):
            self.assertTrue(self.run_projection()['event_pending'])
        self.assertEqual(len(self.event_rows()), 1)
        self.assertFalse(self.run_projection()['event_pending'])
        self.assertEqual(len(self.event_rows()), 1)

    def test_retarget_after_event_append_retains_outbox_for_review(self):
        real = log_event.log
        def changed(**kwargs):
            result = real(**kwargs)
            entry = self.queue_entry()
            entry['ats_url'] = self.url
            entry['application_url'] = self.url + '99'
            self.write_entry(entry)
            return result
        with patch.object(log_event, 'log', side_effect=changed):
            result = self.run_projection()
        self.assertEqual(result['status'], 'PROJECTED')
        self.assertTrue(result['event_pending'])
        self.assertEqual(result['telemetry_error'], 'acknowledgment_conflict')
        self.assertIn(projection.OUTBOX, self.queue_entry())
        self.assertEqual(self.run_projection()['status'], 'HELD')
        self.assertEqual(len(self.event_rows()), 1)

    def test_concurrent_projection_commits_one_marker_and_one_event(self):
        barrier = threading.Barrier(2)
        def concurrent(record, claim):
            decision = self.validator(record, claim)
            barrier.wait(timeout=5)
            return decision
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.run_projection(validators={'synthetic-provider': concurrent}), range(2)))
        self.assertEqual(sorted(result['status'] for result in results), ['HELD', 'PROJECTED'])
        self.assertEqual(self.run_projection()['status'], 'REPLAYED')
        self.assertEqual(len(self.event_rows()), 1)

    def test_ack_failure_keeps_projection_and_recovers_without_event_duplicate(self):
        for after in (False, True):
            with self.subTest(after=after):
                self.write_entry(self.entry)
                if self.events.exists():
                    self.events.unlink()
                def write(path, document):
                    is_ack = projection.MARKER in document['entries'][0] and projection.OUTBOX not in document['entries'][0]
                    if is_ack and not after:
                        raise OSError('fixture ack failed before replacement')
                    atomic_json(path, document)
                    if is_ack:
                        raise OSError('fixture ack failed after replacement')
                with patch.object(projection, 'atomic_json', side_effect=write):
                    result = self.run_projection()
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertTrue(result['queue_committed'])
                self.assertEqual(self.run_projection()['status'], 'REPLAYED')
                self.assertEqual(len(self.event_rows()), 1)

    def test_ack_lock_timeout_preserves_known_commit_and_replay_deduplicates(self):
        real_lock = queue_io.queue_lock

        @contextmanager
        def unavailable_ack(*args, **kwargs):
            if kwargs.get('owner') == 'receipt-projection:ack':
                raise TimeoutError('synthetic private lock detail')
            with real_lock(*args, **kwargs):
                yield

        with patch.object(queue_io, 'queue_lock', unavailable_ack):
            result = self.run_projection()
        self.assertEqual(result['status'], 'PROJECTED')
        self.assertEqual(result['reason'], 'acknowledgment_unavailable')
        self.assertTrue(result['queue_committed'])
        self.assertTrue(result['event_pending'])
        self.assertEqual(result['telemetry_error'], 'TimeoutError')
        self.assertNotIn('synthetic private lock detail', str(result))
        self.assertIn(projection.OUTBOX, self.queue_entry())
        self.assertEqual(len(self.event_rows()), 1)
        replay = self.run_projection()
        self.assertEqual(replay['status'], 'REPLAYED')
        self.assertFalse(replay['event_pending'])
        self.assertEqual(replay['event_id'], result['event_id'])
        self.assertEqual(len(self.event_rows()), 1)

    def test_ack_read_failure_preserves_known_commit_and_replay_deduplicates(self):
        real_read, real_log = projection._read, log_event.log
        event_appended = False

        def appended(**kwargs):
            nonlocal event_appended
            receipt = real_log(**kwargs)
            event_appended = True
            return receipt

        def unreadable_ack(path):
            if event_appended and path == self.queue:
                raise OSError('synthetic private filesystem detail')
            return real_read(path)

        with patch.object(log_event, 'log', appended), patch.object(projection, '_read', unreadable_ack):
            result = self.run_projection()
        self.assertEqual(result['status'], 'PROJECTED')
        self.assertEqual(result['reason'], 'acknowledgment_unavailable')
        self.assertTrue(result['queue_committed'])
        self.assertTrue(result['event_pending'])
        self.assertEqual(result['telemetry_error'], 'OSError')
        self.assertNotIn('synthetic private filesystem detail', str(result))
        self.assertIn(projection.OUTBOX, self.queue_entry())
        self.assertEqual(len(self.event_rows()), 1)
        replay = self.run_projection()
        self.assertEqual(replay['status'], 'REPLAYED')
        self.assertFalse(replay['event_pending'])
        self.assertEqual(replay['event_id'], result['event_id'])
        self.assertEqual(len(self.event_rows()), 1)

    def test_ack_unlock_failure_after_write_keeps_outcome_unknown(self):
        real_lock = queue_io.queue_lock

        @contextmanager
        def uncertain_ack(*args, **kwargs):
            with real_lock(*args, **kwargs):
                yield
            if kwargs.get('owner') == 'receipt-projection:ack':
                raise OSError('synthetic acknowledgment unlock uncertainty')

        with patch.object(queue_io, 'queue_lock', uncertain_ack):
            result = self.run_projection()
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['reason'], 'acknowledgment_write_outcome_unknown')
        self.assertTrue(result['queue_committed'])
        self.assertIsNone(result['event_pending'])
        self.assertEqual(result['error_class'], 'OSError')
        self.assertNotIn(projection.OUTBOX, self.queue_entry())
        self.assertEqual(self.run_projection()['status'], 'REPLAYED')
        self.assertEqual(len(self.event_rows()), 1)

    def test_retained_marker_or_pending_payload_tampering_is_held(self):
        with patch.object(log_event, 'log', side_effect=OSError('keep outbox')):
            self.run_projection()
        original = self.queue_entry()
        for mutate in ('event', 'provider', 'future'):
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(original)
                if mutate == 'event':
                    changed[projection.OUTBOX]['details']['attempt_id'] = 'other'
                elif mutate == 'provider':
                    changed[projection.MARKER]['provider'] = 'other'
                else:
                    changed[projection.MARKER]['projected_at'] = (self.now + timedelta(days=1)).isoformat()
                self.write_entry(changed)
                self.assertEqual(self.run_projection()['reason'], 'retained_projection_conflict')

    def test_paths_runtime_binding_and_non_callable_validator_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'receipts.json'
            external.write_bytes(self.receipts.read_bytes())
            with self.assertRaises(ValueError):
                self.run_projection(receipts_path=external)
            alias = self.home / 'data/alias.json'
            alias.symlink_to(self.receipts)
            with self.assertRaises(ValueError):
                self.run_projection(receipts_path=alias)
        with patch.object(submit_intent, 'STORE_PATH', str(self.home / 'wrong.json')):
            with self.assertRaisesRegex(ValueError, 'runtime_binding'):
                self.run_projection()
        with self.assertRaises(ValueError):
            self.run_projection(validators={'synthetic-provider': 'import.this.callback'})
        with self.assertRaises(ValueError):
            self.run_projection(live=1)
        self.assertEqual(self.event_rows(), [])


if __name__ == '__main__':
    unittest.main()
