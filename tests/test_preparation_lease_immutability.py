"""Synthetic review preparation owns its lease before changing shared inputs."""
import copy
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import apply_loop
import launch_lock
import packet_contract
import parked_task_sweep
import pipeline_service as service
import queue_io
from safe_io import atomic_json, read_json


class PreparationLeaseImmutabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-preparation-lease-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.queue = self.home / 'data/queues/standard-queue.json'
        self.ledger = self.home / 'data/application-ledger.json'
        self.packet_path = self.home / 'data/launch-packets/fixture-role.json'
        for name in service.QUEUES:
            atomic_json(self.home / f'data/queues/{name}-queue.json', [])
        atomic_json(self.ledger, [])
        atomic_json(self.home / 'data/policy.json', {'main_fit_floor': 75})
        values = {'first_name': 'Synthetic', 'last_name': 'Applicant',
                  'email': 'synthetic@fixture.test'}
        atomic_json(self.home / 'data/answer_bank.json', {'answers': values,
            '_provenance': {key: packet_contract.answer_receipt(
                value, 'synthetic-fixture', role_id='fixture-role')
                for key, value in values.items()}})
        self.row = {'role_id': 'fixture-role', 'company': 'Fixture', 'title': 'Fixture role',
                    'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': 75,
                    'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/1',
                    'posting_verification': {'identity': ['greenhouse', 'fixture', '1'],
                        'verdict': 'live', 'observed_at': service.utc_now().isoformat()}}
        atomic_json(self.queue, [self.row])
        (self.home / 'first.txt').write_text('Synthetic first material')
        (self.home / 'second.txt').write_text('Synthetic second material')
        for item in (
            patch.object(queue_io, '_LOCK_PATH', str(self.home / 'hidden_files/queue.lock')),
            patch.object(launch_lock, 'LOCK_DIR', str(self.home / 'hidden_files/launch-locks')),
            patch.object(apply_loop, 'HOME', str(self.home)),
            patch.object(apply_loop, 'DATA', str(self.home / 'data')),
            patch.object(parked_task_sweep, 'EVENTS_JSONL', str(self.home / 'data/telemetry/events.jsonl')),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')),
            patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')),
        ):
            item.start()
            self.addCleanup(item.stop)

    def prepare(self, resume='first.txt'):
        return service.prepare_role(self.home, 'fixture-role', resume, _offline_fixture=True)

    def snapshot(self):
        # Diagnostic lock metadata is not applicant/packet state.
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob('*')
                if path.is_file() and 'hidden_files' not in path.relative_to(self.home).parts}

    def hold_for_other(self):
        self.assertTrue(launch_lock.acquire('fixture-role', 'other-owner')[0])
        path = Path(launch_lock._lock_path('fixture-role'))
        return path, path.read_bytes()

    def test_held_lease_preserves_prior_valid_packet_and_all_input_bytes(self):
        self.prepare()
        service.validate_preparation_packet(self.home, self.packet_path)
        lock_path, lock_before = self.hold_for_other()
        before = self.snapshot()
        with patch.object(launch_lock, 'release', wraps=launch_lock.release) as release:
            with self.assertRaisesRegex(ValueError, 'preparation lease refused: HELD'):
                self.prepare('second.txt')
        release.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lock_path.read_bytes(), lock_before)
        self.assertTrue(service.validate_preparation_packet(
            self.home, self.packet_path)['valid_preparation_packet'])

    def test_corrupt_lease_refusal_preserves_existing_state(self):
        self.prepare()
        lock_path = Path(launch_lock._lock_path('fixture-role'))
        lock_path.write_bytes(b'not a lease')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'corrupt lease'):
            self.prepare('second.txt')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lock_path.read_bytes(), b'not a lease')

    def test_guard_exception_before_acquisition_does_not_change_selection(self):
        before = self.snapshot()
        with patch.object(launch_lock, 'prelaunch_guard', side_effect=OSError('synthetic guard failure')):
            with self.assertRaisesRegex(OSError, 'synthetic guard failure'):
                self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_normal_success_is_valid_review_only_and_releases_lease(self):
        result = self.prepare()
        packet = read_json(result['packet'])
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(packet['execution_authorized'])
        self.assertEqual(packet['scope'], 'preparation_only')
        self.assertEqual(read_json(self.queue)[0]['status'], 'PARKED-PENDING-VERIFICATION')
        self.assertTrue(service.validate_preparation_packet(
            self.home, result['packet'])['valid_preparation_packet'])
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_changes_during_acquisition_are_rechecked_without_stale_rollback(self):
        mutations = ('hold', 'ledger', 'duplicate', 'expired', 'moved')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                for name in service.QUEUES:
                    atomic_json(self.home / f'data/queues/{name}-queue.json', [])
                atomic_json(self.queue, [self.row])
                atomic_json(self.ledger, [])
                real_guard = launch_lock.prelaunch_guard
                changed = {}

                def race(*args, **kwargs):
                    acquired = real_guard(*args, **kwargs)
                    self.assertTrue(acquired[0])
                    row = copy.deepcopy(self.row)
                    with queue_io.queue_lock(owner='synthetic-concurrent-update'):
                        if mutation == 'hold':
                            row['human_hold'] = True
                            atomic_json(self.queue, [row])
                        elif mutation == 'ledger':
                            atomic_json(self.ledger, [{'role_id': 'fixture-role', 'status': 'SUBMITTED'}])
                        elif mutation == 'expired':
                            row['posting_verification']['observed_at'] = (
                                service.utc_now() - timedelta(days=30)).isoformat()
                            atomic_json(self.queue, [row])
                        else:
                            atomic_json(self.home / 'data/queues/strategic-queue.json', [row])
                            if mutation == 'moved':
                                atomic_json(self.queue, [])
                    changed.update(self.snapshot())
                    return acquired

                with patch.object(launch_lock, 'prelaunch_guard', side_effect=race):
                    with self.assertRaises(ValueError):
                        self.prepare()
                self.assertEqual(self.snapshot(), changed)
                self.assertIsNone(launch_lock.check('fixture-role'))

    def test_interruption_after_acquisition_releases_only_own_lease(self):
        before = self.snapshot()
        real_guard = launch_lock.prelaunch_guard

        def interrupt(*args, **kwargs):
            self.assertTrue(real_guard(*args, **kwargs)[0])
            raise KeyboardInterrupt('synthetic interruption after acquisition')

        with patch.object(launch_lock, 'prelaunch_guard', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_interrupted_build_preserves_prior_packet_and_releases(self):
        self.prepare()
        packet_before = self.packet_path.read_bytes()
        with patch.object(packet_contract, 'prepare', side_effect=KeyboardInterrupt('synthetic build interruption')):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare('second.txt')
        self.assertEqual(self.packet_path.read_bytes(), packet_before)
        self.assertIsNone(launch_lock.check('fixture-role'))
        self.assertEqual(read_json(self.queue)[0]['status'], 'PARKED-PENDING-VERIFICATION')

    def test_queue_lock_timeout_after_acquisition_releases_without_selection_write(self):
        real_lock = service.queue_lock

        @contextmanager
        def timeout_on_selection(*args, **kwargs):
            if kwargs.get('owner') == 'prepare-role:select':
                self.assertIsNotNone(launch_lock.check('fixture-role'))
                raise queue_io.QueueLockTimeout(123, 'synthetic-holder', 10, 10)
            with real_lock(*args, **kwargs):
                yield

        before = self.snapshot()
        with patch.object(service, 'queue_lock', side_effect=timeout_on_selection):
            with self.assertRaises(queue_io.QueueLockTimeout):
                self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_interruption_after_selection_commit_does_not_restore_stale_queue(self):
        real_write = service.atomic_json

        def interrupt(path, value):
            real_write(path, value)
            if Path(path) == self.queue:
                queue_io.patch_entry(str(self.queue), 'fixture-role', {'human_hold': True})
                raise KeyboardInterrupt('synthetic interruption after selection commit')

        with patch.object(service, 'atomic_json', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare()
        row = read_json(self.queue)[0]
        self.assertTrue(row['human_hold'])
        self.assertEqual(row['materials']['resume'], 'first.txt')
        self.assertEqual(row['preparation_selection']['scope'], 'review_only')
        self.assertFalse(self.packet_path.exists())
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_publication_failure_preserves_prior_packet(self):
        self.prepare()
        packet_before = self.packet_path.read_bytes()
        real_write = service.atomic_json

        def refuse_packet(path, value):
            if Path(path) == self.packet_path:
                raise OSError('synthetic packet publication failure')
            real_write(path, value)

        with patch.object(service, 'atomic_json', side_effect=refuse_packet):
            with self.assertRaisesRegex(OSError, 'synthetic packet publication failure'):
                self.prepare('second.txt')
        self.assertEqual(self.packet_path.read_bytes(), packet_before)
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_failed_final_validation_never_replaces_prior_packet(self):
        self.prepare()
        packet_before = self.packet_path.read_bytes()
        real_prepare = packet_contract.prepare

        def concurrent_hold(*args, **kwargs):
            packet = real_prepare(*args, **kwargs)
            queue_io.patch_entry(str(self.queue), 'fixture-role', {'human_hold': True})
            return packet

        with patch.object(packet_contract, 'prepare', side_effect=concurrent_hold):
            with self.assertRaisesRegex(ValueError, 'role or ledger changed during preparation'):
                self.prepare('second.txt')
        self.assertEqual(self.packet_path.read_bytes(), packet_before)
        self.assertTrue(read_json(self.queue)[0]['human_hold'])
        self.assertIsNone(launch_lock.check('fixture-role'))

    def test_lost_lease_before_selection_preserves_successor_state(self):
        real_guard = launch_lock.prelaunch_guard
        successor = {}

        def replace(*args, **kwargs):
            acquired = real_guard(*args, **kwargs)
            self.assertTrue(acquired[0])
            self.assertTrue(launch_lock.release(args[0], args[1])[0])
            path, body = self.hold_for_other()
            successor.update(path=path, body=body)
            return acquired

        before = self.snapshot()
        with patch.object(launch_lock, 'prelaunch_guard', side_effect=replace):
            with self.assertRaises((ValueError, RuntimeError)):
                self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(successor['path'].read_bytes(), successor['body'])

    def test_lost_lease_during_build_preserves_successor_packet_and_lease(self):
        real_prepare = packet_contract.prepare
        successor = {}

        def replace(*args, **kwargs):
            packet = real_prepare(*args, **kwargs)
            task = launch_lock.check('fixture-role')['task_id']
            self.assertTrue(launch_lock.release('fixture-role', task)[0])
            path, body = self.hold_for_other()
            successor.update(path=path, body=body)
            atomic_json(self.packet_path, {'synthetic_successor_packet': True})
            successor['packet'] = self.packet_path.read_bytes()
            return packet

        with patch.object(packet_contract, 'prepare', side_effect=replace):
            with self.assertRaises((ValueError, RuntimeError)):
                self.prepare()
        self.assertEqual(self.packet_path.read_bytes(), successor['packet'])
        self.assertEqual(successor['path'].read_bytes(), successor['body'])


if __name__ == '__main__':
    unittest.main()
