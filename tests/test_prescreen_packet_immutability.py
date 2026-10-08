"""Prescreen must not rewrite packets that have already been sealed."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_prescreen_answer_authority as fixtures
import apply_loop
import packet_contract
import prescreen
import ready_gate
from safe_io import canonical, digest


QUESTION = 'Which synthetic workflow have you practiced?'
ANSWER = 'Synthetic workflow one'
KEY = 'synthetic_workflow'


class PrescreenPacketImmutabilityTests(unittest.TestCase):
    def setUp(self):
        fixtures.CaptureToPrescreenTests.setUp(self)
        self.packets = self.root / 'packets'
        self.packets.mkdir()
        (self.root / 'resume.txt').write_text('Synthetic review-only material.\n')
        self.entry = {'role_id': 'fixture-role', 'company': 'Fixture',
                      'title': 'Fixture role', 'ats_url': self.url,
                      'materials': {'resume': 'resume.txt'},
                      'posting_text': 'Synthetic posting.', 'posting_text_url': self.url}
        self.intel['questions'][0]['label'] = QUESTION
        self.packet.update(frp_enabled=True, brief='Synthetic preparation-only packet.',
                           scope='preparation_only', execution_authorized=False,
                           submission_authorized=False,
                           upload_files=[str(self.root / 'resume.txt')])
        for module, key, value in ((prescreen, 'PACKET_DIR', str(self.packets)),
                                   (apply_loop, 'HOME', str(self.root)),
                                   (apply_loop, 'PACKETS', str(self.packets)),
                                   (ready_gate, 'HOME', str(self.root))):
            self.enterContext(patch.object(module, key, value))
        self.enterContext(patch.dict('os.environ', {'KEEL_FRP': '1'}))
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo'):
            mock = self.enterContext(patch(target, side_effect=AssertionError('network prohibited')))
            self.addCleanup(mock.assert_not_called)

    def capture(self, writer='tray', answer=ANSWER):
        fixtures.CaptureToPrescreenTests.capture(self, writer, key=KEY,
                                               question=QUESTION, answer=answer)
        return json.loads(self.path.read_text())

    def sealed(self, bank, *, annotation=False, seal='launch_integrity_sha256'):
        packet = copy.deepcopy(self.packet)
        if annotation:
            self.assertEqual(prescreen.screen_packet(packet, bank, {})['verdict'], 'CLEAN')
        if seal == 'launch_integrity_sha256':
            ready_gate.seal_packet(packet, self.entry, bank, workspace=str(self.root))
        else:
            packet[seal] = digest(packet)
        return packet

    def persist(self, packet, filename='fixture-role.json'):
        path = self.packets / filename
        # Deliberately non-default formatting makes any needless rewrite visible.
        path.write_text(json.dumps(packet, indent=4, sort_keys=True) + '\n')
        return path

    def admission(self, packet, bank):
        return ready_gate.packet_admission(packet, self.entry, bank,
                                           workspace=str(self.root), for_execution=False)

    def build(self, bank, dest=None):
        with patch.object(apply_loop, 'load_answer_bank', return_value=bank), \
             patch.object(apply_loop.form_intel, 'probe_url', return_value=copy.deepcopy(self.intel)):
            return Path(apply_loop.build_packet(self.entry, dest_dir=str(dest or self.packets)))

    def test_persisted_missing_annotation_requires_rebuild_without_invalidating_packet(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                bank = self.capture(writer)
                packet = self.sealed(bank)
                self.assertTrue(self.admission(packet, bank)['allowed'])
                path = self.persist(packet)
                before = path.read_bytes()
                result = prescreen.scan_packets(str(self.packets), bank, {})[0]
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(result['verdict'], 'ERROR')
                self.assertIn('Packet rebuild required', result['reasons'][0])
                self.assertTrue(self.admission(json.loads(path.read_text()), bank)['allowed'])

    def test_in_memory_consumer_holds_instead_of_requesting_another_answer(self):
        bank = self.capture()
        packet = self.sealed(bank)
        before = canonical(packet)
        with self.assertRaisesRegex(ValueError, 'Packet rebuild required') as raised:
            apply_loop._checked_prescreen(packet, bank)
        self.assertEqual(type(raised.exception).__name__, 'PacketRebuildRequired')
        self.assertEqual(canonical(packet), before)
        self.assertTrue(self.admission(packet, bank)['allowed'])

    def test_scan_continues_after_rebuild_required_packet(self):
        bank = self.capture()
        missing = self.sealed(bank)
        complete = self.sealed(bank, annotation=True)
        paths = [self.persist(missing, 'a.json'), self.persist(complete, 'b.json')]
        before = [path.read_bytes() for path in paths]
        results = prescreen.scan_packets(str(self.packets), bank, {})
        self.assertEqual([result['verdict'] for result in results], ['ERROR', 'CLEAN'])
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_fresh_builder_and_repeated_rescreen_preserve_packet_bytes_and_seal(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                bank = self.capture(writer)
                path = self.build(bank)
                packet = json.loads(path.read_text())
                self.assertEqual(packet['frp_derived_answers'][QUESTION]['answer'], ANSWER)
                self.assertIs(packet['execution_authorized'], False)
                self.assertFalse(packet.get('submission_authorized', False))
                self.assertTrue(self.admission(packet, bank)['allowed'])
                path = self.persist(packet)
                before = path.read_bytes(), canonical(packet)
                for _ in range(2):
                    self.assertEqual(apply_loop._checked_prescreen(packet, bank)['verdict'], 'CLEAN')
                    self.assertEqual(prescreen.scan_packets(str(self.packets), bank, {})[0]['verdict'], 'CLEAN')
                    self.assertEqual((path.read_bytes(), canonical(packet)), before)
                    self.assertTrue(self.admission(packet, bank)['allowed'])

    def test_buffer_build_never_overwrites_existing_same_role_root_packet(self):
        bank = self.capture()
        original = self.sealed(bank)
        path = self.persist(original)
        before = path.read_bytes()
        buffer = self.root / 'buffer'
        built = self.build(bank, buffer)
        self.assertTrue(built.exists())
        self.assertEqual(path.read_bytes(), before)
        self.intel['questions'].append({'label': 'I certify I did not use AI',
                                       'type': 'text', 'required': True, 'options': []})
        with self.assertRaises(apply_loop.PacketPrescreenParked):
            self.build(bank, self.root / 'held-buffer')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / 'held-buffer' / path.name).exists())

    def test_both_seal_fields_protect_missing_changed_and_malformed_annotations(self):
        bank = self.capture()
        for seal in ('launch_integrity_sha256', 'integrity_sha256'):
            for damage in ('missing', 'answer', 'provenance', 'malformed', 'empty-seal', 'null-seal'):
                with self.subTest(seal=seal, damage=damage):
                    packet = self.sealed(bank, annotation=True, seal=seal)
                    if damage in ('missing', 'empty-seal', 'null-seal'):
                        packet.pop('frp_derived_answers')
                    elif damage == 'malformed':
                        packet['frp_derived_answers'] = []
                    else:
                        packet['frp_derived_answers'][QUESTION][damage] = 'Different synthetic value'
                    if damage == 'empty-seal': packet[seal] = ''
                    if damage == 'null-seal': packet[seal] = None
                    before = canonical(packet)
                    with self.assertRaisesRegex(ValueError, 'Packet rebuild required'):
                        prescreen.screen_packet(packet, bank, {})
                    self.assertEqual(canonical(packet), before)

    def test_existing_annotation_comparison_preserves_json_value_types(self):
        for answer, different in ((False, 0), (1, 1.0)):
            bank = self.capture(answer=answer)
            for seal in ('launch_integrity_sha256', 'integrity_sha256'):
                with self.subTest(answer=answer, seal=seal):
                    packet = self.sealed(bank, annotation=True, seal=seal)
                    before = canonical(packet)
                    self.assertEqual(prescreen.screen_packet(packet, bank, {})['verdict'], 'CLEAN')
                    self.assertEqual(canonical(packet), before)
                    packet['frp_derived_answers'][QUESTION]['answer'] = different
                    before = canonical(packet)
                    with self.assertRaisesRegex(ValueError, 'Packet rebuild required'):
                        prescreen.screen_packet(packet, bank, {})
                    self.assertEqual(canonical(packet), before)

    def test_tampered_packet_is_never_repaired_by_screening(self):
        bank = self.capture()
        for seal in ('launch_integrity_sha256', 'integrity_sha256'):
            with self.subTest(seal=seal):
                packet = self.sealed(bank, annotation=True, seal=seal)
                packet['schema_version'] = 1
                packet['brief'] += ' Changed after sealing.'
                path = self.persist(packet)
                before = path.read_bytes(), canonical(packet)
                self.assertEqual(prescreen.screen_packet(packet, bank, {})['verdict'], 'CLEAN')
                self.assertEqual(prescreen.scan_packets(str(self.packets), bank, {})[0]['verdict'], 'CLEAN')
                self.assertEqual((path.read_bytes(), canonical(packet)), before)
                if seal == 'launch_integrity_sha256':
                    self.assertEqual(self.admission(packet, bank)['reason_codes'], ['packet_integrity_mismatch'])
                else:
                    with self.assertRaisesRegex(ValueError, 'packet integrity mismatch'):
                        packet_contract.validate(packet, self.entry, bank, {}, self.root,
                                                 self.entry['materials'])

    def test_noncanonical_annotation_errors_without_aborting_scan_neighbors(self):
        bank = self.capture()
        complete = self.sealed(bank, annotation=True)
        malformed = copy.deepcopy(complete)
        malformed['frp_derived_answers'][QUESTION]['answer'] = float('nan')
        paths = [self.persist(malformed, 'a.json'), self.persist(complete, 'b.json')]
        before = [path.read_bytes() for path in paths]
        results = prescreen.scan_packets(str(self.packets), bank, {})
        self.assertEqual([result['verdict'] for result in results], ['ERROR', 'CLEAN'])
        self.assertEqual([path.read_bytes() for path in paths], before)


if __name__ == '__main__':
    unittest.main()
