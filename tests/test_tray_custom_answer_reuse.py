"""Captured custom answers retain exact question, receipt and scope boundaries."""
import copy
import json
import socket
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_prescreen_answer_authority as fixtures
import packet_contract
import prescreen
import tray_answer
from safe_io import utc_now

QUESTION = 'Which synthetic workflow have you practiced?'
ANSWER = 'Synthetic workflow one'
KEY = 'synthetic_workflow'


class TrayCustomAnswerReuseTests(unittest.TestCase):
    def setUp(self):
        fixtures.CaptureToPrescreenTests.setUp(self)
        self.packet['frp_enabled'] = True
        self.intel['questions'][0]['label'] = QUESTION
        self.packet['execution_authorized'] = False
        self.packet['submission_authorized'] = False
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network prohibited'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def capture(self, *, scope='global', question=QUESTION, variants=(), answer=ANSWER, writer='tray'):
        if writer == 'tray':
            result = tray_answer.apply_bank_write(str(self.path), KEY, answer, scope,
                {'key': 'synthetic-card', 'question': question}, 'synthetic reply',
                employers=set(), question_variants=variants)
            self.assertEqual(result, (True, False, scope))
        else:
            fixtures.CaptureToPrescreenTests.capture(self, writer, key=KEY,
                question=question, scope=scope, answer=answer)
        return json.loads(self.path.read_text())

    def screen(self, bank, question=QUESTION):
        self.intel['questions'][0]['label'] = question
        self.packet.pop('frp_derived_answers', None)
        before = copy.deepcopy(bank), self.path.read_bytes()
        result = prescreen.screen_packet(self.packet, bank, {})
        self.assertEqual((bank, self.path.read_bytes()), before)
        self.assertIs(self.packet['execution_authorized'], False)
        self.assertIs(self.packet['submission_authorized'], False)
        return result['verdict']

    def test_both_capture_formats_supply_the_confirmed_custom_value(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                bank = self.capture(writer=writer)
                approved, problems = packet_contract.confirmed_answers(bank, role_id='fixture-role',
                    employer='Fixture', role_context=self.packet)
                self.assertEqual(problems, [])
                self.assertEqual(approved[KEY], ANSWER)
                self.assertEqual(self.screen(bank), 'CLEAN')
                self.assertEqual(self.packet['frp_derived_answers'][QUESTION]['answer'], ANSWER)

    def test_recorded_variants_and_display_whitespace_match_exactly(self):
        variant = 'Which synthetic checklist have you practiced?'
        bank = self.capture(variants=[variant])
        for question in (QUESTION, variant, QUESTION.replace(' ', '  ')):
            with self.subTest(question=question):
                self.assertEqual(self.screen(bank, question), 'CLEAN')
                self.assertEqual(next(iter(self.packet['frp_derived_answers'].values()))['answer'], ANSWER)

    def test_added_obligations_and_negation_are_not_question_aliases(self):
        bank = self.capture()
        for question in (QUESTION + ' and for how many years?',
                         'Which synthetic workflow have you not practiced?',
                         'Which synthetic workflow have you practiced independently?',
                         'Which unrelated checklist have you practiced?'):
            with self.subTest(question=question):
                self.assertEqual(self.screen(bank, question), 'PARK')
                self.assertNotIn('frp_derived_answers', self.packet)
        bank = self.capture(question='Which synthetic workflow 1-2 have you practiced?')
        self.assertEqual(self.screen(bank, 'Which synthetic workflow 1+2 have you practiced?'), 'PARK')

    def test_receipt_value_metadata_scope_and_time_are_revalidated(self):
        captured = self.capture(scope='employer:Fixture')
        self.assertEqual(self.screen(captured), 'CLEAN')
        for damage in ('missing', 'expired', 'future', 'wrong-role', 'value', 'question', 'variants', 'scope'):
            with self.subTest(damage=damage):
                bank = copy.deepcopy(captured)
                receipt = bank['_provenance'][KEY]
                if damage == 'missing': bank['_provenance'] = {}
                elif damage == 'expired': receipt['expires_at'] = (utc_now()-timedelta(seconds=1)).isoformat()
                elif damage == 'future': receipt['recorded_at'] = (utc_now()+timedelta(days=1)).isoformat()
                elif damage == 'wrong-role': receipt['scope'] = 'other-role'
                elif damage == 'value': bank['answers'][KEY]['value'] = 'Changed'
                elif damage == 'question': bank['answers'][KEY]['question'] = 'Changed question'
                elif damage == 'variants': bank['answers'][KEY]['question_variants'] = ['Changed question']
                else: bank['answers'][KEY]['scope'] = 'global'
                self.assertEqual(self.screen(bank), 'PARK')
                self.assertNotIn('frp_derived_answers', self.packet)
        self.packet['company'] = 'Other'
        self.assertEqual(self.screen(captured), 'PARK')

    def test_negative_value_is_preserved_without_truthiness_fallback(self):
        bank = self.capture(answer=False)
        self.assertEqual(self.screen(bank), 'CLEAN')
        self.assertIs(self.packet['frp_derived_answers'][QUESTION]['answer'], False)

    def test_hard_gates_and_unanswered_neighbors_remain_parked(self):
        bank = self.capture()
        for label in ('I agree to arbitration', 'I certify I did not use AI',
                      'Which unrelated checklist have you practiced?'):
            with self.subTest(label=label):
                self.intel['questions'] = [self.intel['questions'][0],
                    {'label': label, 'type': 'text', 'required': True, 'options': []}]
                self.assertEqual(self.screen(bank), 'PARK')
                self.assertEqual(self.packet['frp_derived_answers'][QUESTION]['answer'], ANSWER)
        self.intel['questions'] = self.intel['questions'][:1]
        for label in ('I consent to data processing', 'I certify I did not use AI'):
            with self.subTest(captured_gate=label):
                captured = self.capture(question=label, scope='employer:Fixture', answer='Yes')
                self.assertEqual(self.screen(captured, label), 'PARK')

    def test_frp_opt_in_is_still_required(self):
        bank = self.capture()
        self.packet['frp_enabled'] = False
        self.assertEqual(self.screen(bank), 'PARK')
        self.assertNotIn('frp_derived_answers', self.packet)


if __name__ == '__main__':
    unittest.main()
