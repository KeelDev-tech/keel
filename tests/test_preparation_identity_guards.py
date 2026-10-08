"""Review-only preparation respects both role identity and exact posting identity."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import apply_loop
import launch_lock
import packet_contract
import pipeline_service as service
import queue_io
from safe_io import atomic_json, read_json


class PreparationIdentityGuardsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-preparation-guard-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.queue = self.home / 'data/queues/standard-queue.json'
        self.ledger = self.home / 'data/application-ledger.json'
        self.material = self.home / 'resume.txt'
        self.material.write_text('Synthetic review material')
        for name in service.QUEUES:
            atomic_json(self.home / f'data/queues/{name}-queue.json', [])
        atomic_json(self.ledger, [])
        self.row = {'role_id': 'fixture-role', 'company': 'Fixture', 'title': 'Fixture role',
                    'status': 'PARKED-PENDING-VERIFICATION', 'fit_score': 75,
                    'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/1',
                    'posting_verification': {'identity': ['greenhouse', 'fixture', '1'],
                        'verdict': 'live', 'observed_at': service.utc_now().isoformat()}}
        atomic_json(self.queue, [self.row])
        values = {'first_name': 'Synthetic', 'last_name': 'Applicant', 'email': 'synthetic@fixture.test'}
        self.bank = {'answers': values, '_provenance': {
            key: packet_contract.answer_receipt(value, 'synthetic-fixture', role_id=self.row['role_id'])
            for key, value in values.items()}}
        self.release = None
        for item in (patch.object(queue_io, '_LOCK_PATH', str(self.home / 'queue.lock')),
                     patch.object(launch_lock, 'LOCK_DIR', str(self.home / 'launch-locks')),
                     patch.object(apply_loop, 'load_answer_bank', return_value=self.bank),
                     patch.object(apply_loop, 'load_policy', return_value={'main_fit_floor': 75}),
                     patch.object(apply_loop, '_materials_for', side_effect=lambda row, _: row['materials']),
                     patch.object(launch_lock, 'prelaunch_guard',
                                  side_effect=lambda role, task, *args, **kwargs:
                                      launch_lock.acquire(role, task))):
            item.start()
            self.addCleanup(item.stop)
        release_patch = patch.object(launch_lock, 'release', wraps=launch_lock.release)
        self.release = release_patch.start()
        self.addCleanup(release_patch.stop)

    def prepare(self):
        return service.prepare_role(self.home, 'fixture-role', 'resume.txt', _offline_fixture=True)

    def test_same_role_terminal_ledger_without_url_blocks_before_any_preparation_write(self):
        atomic_json(self.ledger, [{'role_id': 'fixture-role', 'status': 'SUBMITTED'}])
        before = self.queue.read_bytes()
        with self.assertRaisesRegex(ValueError, 'active or terminal ledger outcome'):
            self.prepare()
        self.assertEqual(self.queue.read_bytes(), before)
        self.release.assert_not_called()
        self.assertFalse((self.home / 'data/launch-packets').exists())

    def test_same_posting_other_role_terminal_ledger_blocks_before_any_write(self):
        atomic_json(self.ledger, [{**self.row, 'role_id': 'other-role', 'status': 'UNKNOWN_OUTCOME'}])
        before = self.queue.read_bytes()
        with self.assertRaisesRegex(ValueError, 'active or terminal ledger outcome'):
            self.prepare()
        self.assertEqual(self.queue.read_bytes(), before)

    def test_concurrent_same_posting_other_role_terminal_outcome_discards_packet_and_releases(self):
        real_prepare = packet_contract.prepare
        def race(*args, **kwargs):
            packet = real_prepare(*args, **kwargs)
            atomic_json(self.ledger, [{**self.row, 'role_id': 'other-role', 'status': 'SUBMITTED'}])
            return packet
        with patch.object(packet_contract, 'prepare', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'role or ledger changed during preparation'):
                self.prepare()
        self.release.assert_called_once()
        self.assertFalse((self.home / 'data/launch-packets/fixture-role.json').exists())
        copies = list((self.home / 'data/packet-materials').iterdir())
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), self.material.read_bytes())
        selected = read_json(self.queue)[0]
        self.assertEqual(selected['status'], 'PARKED-PENDING-VERIFICATION')
        self.assertEqual(selected['preparation_selection']['scope'], 'review_only')

    def test_concurrent_queue_duplicate_exact_posting_discards_packet(self):
        real_prepare = packet_contract.prepare
        def race(*args, **kwargs):
            packet = real_prepare(*args, **kwargs)
            atomic_json(self.home / 'data/queues/strategic-queue.json', [{**self.row, 'role_id': 'other-role'}])
            return packet
        with patch.object(packet_contract, 'prepare', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'role or ledger changed during preparation'):
                self.prepare()
        self.release.assert_called_once()
        self.assertFalse((self.home / 'data/launch-packets/fixture-role.json').exists())

    def test_other_posting_same_employer_does_not_block_review_only_preparation(self):
        atomic_json(self.ledger, [{**self.row, 'role_id': 'other-role', 'status': 'SUBMITTED',
            'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/2'}])
        result = self.prepare()
        packet = read_json(result['packet'])
        self.assertEqual(packet['scope'], 'preparation_only')
        self.assertFalse(packet['execution_authorized'])
        self.assertEqual(result['status'], 'PREPARED_REVIEW_REQUIRED')
        self.assertEqual(read_json(self.queue)[0]['status'], 'PARKED-PENDING-VERIFICATION')
        self.release.assert_called_once()


    def test_lease_release_failure_leaves_review_packet_and_material(self):
        self.release.return_value = (False, {'status': 'synthetic-release-failure'})
        with self.assertRaisesRegex(RuntimeError, 'preparation lease release failed'):
            self.prepare()
        self.release.assert_called_once()
        packet = read_json(self.home / 'data/launch-packets/fixture-role.json')
        self.assertFalse(packet['execution_authorized'])
        self.assertEqual(packet['status'], 'PREPARED_REVIEW_REQUIRED')
        self.assertEqual(Path(packet['upload_files'][0]).read_bytes(), self.material.read_bytes())


if __name__ == '__main__':
    unittest.main()
