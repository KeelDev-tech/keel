"""Synthetic staging continuity tests; no browser, network or applicant data."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import apply_loop
import ready_gate
import stage_ready_launches as stage


class StagedReadyStatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='keel-staged-states-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.data = self.home / 'data'
        queues = self.data / 'queues'
        queues.mkdir(parents=True)
        self.buffer = self.data / 'launch-packets' / 'buffer'
        self.buffer.mkdir(parents=True)
        self.resume = self.home / 'resume.txt'
        self.resume.write_text('Synthetic prepared resume')
        for name in ('standard', 'strategic', 'needs_input', 'rejected'):
            (queues / (name + '-queue.json')).write_text('[]')
        (self.data / 'application-ledger.json').write_text('[]')
        (self.data / 'employer-blocklist.md').write_text('# Blocked employers\n')
        self.bank = {'answers': {}, 'gates': {}, 'banded_questions': {}}
        settings = {'HOME': str(self.home), 'DATA': str(self.data),
                    'STD_Q': str(queues / 'standard-queue.json'),
                    'NI_Q': str(queues / 'needs_input-queue.json'),
                    'LEDGER': str(self.data / 'application-ledger.json'),
                    'BLOCKLIST': str(self.data / 'employer-blocklist.md'),
                    'STAGED': str(self.data / 'staged.json'),
                    'BUFFER_DIR': str(self.buffer)}
        patches = [patch.multiple(stage, **settings),
                   patch.object(ready_gate, 'HOME', str(self.home)),
                   patch.object(apply_loop, 'load_answer_bank', return_value=self.bank),
                   patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def fixture(self, status='READY-FOR-BROWSER', **changes):
        entry = {'role_id': 'fixture-role', 'company': 'Synthetic Employer',
                 'title': 'Operations Manager', 'status': status,
                 'action_band': 'APPLY', 'fit_score': 75,
                 'ats_url': 'https://jobs.lever.co/synthetic/exact-job-id',
                 'materials': {'resume': str(self.resume)}, **changes}
        Path(stage.STD_Q).write_text(json.dumps([entry]))
        packet = {**{key: entry[key] for key in ('role_id', 'company', 'title', 'ats_url')},
                  'brief': 'Synthetic reviewed application', 'upload_files': [str(self.resume)],
                  'scope': 'application', 'execution_authorized': True}
        ready_gate.seal_packet(packet, entry, self.bank, workspace=str(self.home))
        path = self.buffer / 'fixture-role.json'
        path.write_text(json.dumps(packet))
        return entry, path

    def test_both_canonical_ready_states_supply_new_staged_packets(self):
        for status in sorted(ready_gate.READY_STATES):
            with self.subTest(status=status):
                entry, _ = self.fixture(status)
                self.assertTrue(ready_gate.entry_admission(entry)['allowed'])
                self.assertTrue(stage._entry_fireable(entry, set(), set(), set(), set(), self.bank)[0])
                plan = stage.build_plan()
                self.assertEqual(plan['ready_total'], 1)
                self.assertEqual(plan['admissible_packet_total'], 1)
                self.assertEqual([row['role_id'] for row in plan['new']], ['fixture-role'])

    def test_existing_browser_ready_packet_is_retained_without_queue_writes(self):
        self.fixture()
        staged = {'role_id': 'fixture-role', 'status': 'STAGED'}
        Path(stage.STAGED).write_text(json.dumps({'entries': [staged]}))
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        plan = stage.build_plan()
        self.assertEqual(plan['kept'], [staged])
        self.assertEqual(plan['pruned'], [])
        self.assertEqual(plan['new'], [])
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_browser_ready_revival_rechecks_packet_before_clearing_block(self):
        _, path = self.fixture()
        staged = {'role_id': 'fixture-role', 'status': 'STAGED',
                  'blocked_status': 'BLOCKED-INPUT', 'blocked_reason': 'prior hold',
                  'reverify_required': True, 'fireable': False}
        Path(stage.STAGED).write_text(json.dumps({'entries': [staged]}))
        plan = stage.build_plan()
        self.assertEqual(plan['kept'], [{'role_id': 'fixture-role', 'status': 'STAGED'}])
        self.assertEqual(plan['admissible_packet_total'], 1)
        self.assertEqual(plan['new'], [])
        path.unlink()
        refused = stage.build_plan()
        self.assertEqual(refused['kept'], [])
        self.assertEqual(refused['admissible_packet_total'], 0)
        self.assertEqual(refused['pruned'][0]['reason'], 'packet missing')

    def test_browser_ready_still_requires_current_packet_and_clear_holds(self):
        for defect in ('missing', 'stale', 'changed_material', 'human_hold', 'ledger', 'needs_input'):
            with self.subTest(defect=defect):
                Path(stage.LEDGER).write_text('[]')
                Path(stage.NI_Q).write_text('[]')
                self.resume.write_text('Synthetic prepared resume')
                entry, path = self.fixture(**({'human_hold': True} if defect == 'human_hold' else {}))
                if defect == 'missing': path.unlink()
                if defect == 'stale': os.utime(path, (time.time()-13*3600,)*2)
                if defect == 'changed_material': self.resume.write_text('Changed synthetic material')
                if defect == 'ledger': Path(stage.LEDGER).write_text(json.dumps([{**entry, 'status': 'SUBMITTED'}]))
                if defect == 'needs_input': Path(stage.NI_Q).write_text(json.dumps([copy.deepcopy(entry)]))
                plan = stage.build_plan()
                self.assertEqual(plan['ready_total'], 1)
                self.assertEqual(plan['admissible_packet_total'], 0)
                self.assertEqual(plan['new'], [])
                self.assertTrue(plan['skipped'])

    def test_other_and_malformed_states_are_not_ready_supply(self):
        for status in ('PARKED-PENDING-VERIFICATION', 'IN-FLIGHT', 'BLOCKED-INPUT', None, [], {}):
            with self.subTest(status=status):
                Path(stage.STD_Q).write_text(json.dumps([{'role_id': 'fixture-role', 'status': status}]))
                plan = stage.build_plan()
                self.assertEqual(plan['ready_total'], 0)
                self.assertEqual(plan['admissible_packet_total'], 0)


if __name__ == '__main__':
    unittest.main()
