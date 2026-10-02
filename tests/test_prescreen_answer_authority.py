"""Receipt and scope authority must survive legacy packet screening."""
import copy
import json
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import apply_loop
import packet_contract
import prescreen
from safe_io import utc_now


class PrescreenAnswerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.now = utc_now()
        self.url = 'https://fixture.invalid/jobs/1'
        self.intel = {'ats': 'fixture', 'source_url': self.url, 'extraction_complete': True,
                      'rendered_option_fetch_needed': [], 'questions': [
                          {'label': 'First Name', 'type': 'text', 'required': True, 'options': []}]}
        self.packet = {'role_id': 'fixture-role', 'company': 'Fixture', 'title': 'Fixture role',
                       'ats_url': self.url, 'form_intel': self.intel, 'form_intel_complete': True,
                       'posting_text': 'Synthetic posting.', 'posting_text_complete': True,
                       'posting_text_url': self.url, 'posting_text_source': 'synthetic',
                       'frp_enabled': False, 'brief': ''}
        self.bank = {'answers': {'first_name': 'Riley'}, '_provenance': {
            'first_name': packet_contract.answer_receipt('Riley', 'synthetic assertion',
                                                        role_id='fixture-role', now=self.now)}}

    def screen(self, bank):
        return prescreen.screen_packet(self.packet, bank, {})['verdict']

    def test_invalid_raw_values_cannot_clear_required_question(self):
        for damage in ('none', 'empty', 'unreceipted', 'expired', 'wrong-role', 'changed', 'wrong-employer'):
            with self.subTest(damage=damage):
                bank = copy.deepcopy(self.bank)
                if damage == 'none': bank['answers']['first_name'] = None
                elif damage == 'empty': bank['answers']['first_name'] = ''
                elif damage == 'unreceipted': bank['_provenance'] = {}
                elif damage == 'expired': bank['_provenance']['first_name']['expires_at'] = (self.now-timedelta(seconds=1)).isoformat()
                elif damage == 'wrong-role': bank['_provenance']['first_name']['scope'] = 'other-role'
                elif damage == 'changed': bank['answers']['first_name'] = 'Changed'
                else:
                    value = {'value': 'Riley', 'scope': 'employer:Other', 'provenance': 'synthetic'}
                    bank['answers']['first_name'] = value
                    bank['_provenance']['first_name'] = packet_contract.answer_receipt(value, 'synthetic', role_id='fixture-role')
                self.assertEqual(self.screen(bank), 'PARK')
                self.assertEqual(apply_loop._checked_prescreen(self.packet, bank)['verdict'], 'PARK')

    def test_valid_role_and_employer_scoped_assertions_remain_usable(self):
        self.assertEqual(self.screen(self.bank), 'CLEAN')
        value = {'value': 'Riley', 'scope': 'employer:Fixture', 'provenance': 'synthetic'}
        bank = {'answers': {'first_name': value}, '_provenance': {
            'first_name': packet_contract.answer_receipt(value, 'synthetic', role_id='fixture-role')}}
        self.assertEqual(self.screen(bank), 'CLEAN')
        with patch('form_intel.probe_url', return_value=self.intel):
            self.assertEqual(prescreen.screen_entry_prepromotion(self.packet, answer_bank=bank,
                             posting_text='Synthetic posting.')['verdict'], 'CLEAN')

    def test_banded_rule_without_value_receipt_cannot_clear_question(self):
        self.assertEqual(self.screen({'answers': {}, 'banded_questions': {'first_name': {'rule': 'Use reference'}}}), 'PARK')

    def test_legacy_build_does_not_publish_unconfirmed_answer_packet(self):
        with tempfile.TemporaryDirectory() as home:
            entry = {**self.packet, 'materials': {'resume': 'resume.txt'}}
            Path(home, 'resume.txt').write_text('Synthetic material')
            dest = Path(home, 'packets')
            with patch.object(apply_loop, 'HOME', home), patch.object(apply_loop, 'load_answer_bank', return_value={'answers': {'first_name': 'Riley'}}), patch('form_intel.probe_url', return_value=self.intel):
                with self.assertRaises(apply_loop.PacketPrescreenParked):
                    apply_loop.build_packet(entry, dest_dir=str(dest))
            self.assertFalse((dest / 'fixture-role.json').exists())


    def test_legacy_build_preserves_valid_review_only_packet(self):
        with tempfile.TemporaryDirectory() as home:
            entry = {**self.packet, 'materials': {'resume': 'resume.txt'}}
            Path(home, 'resume.txt').write_text('Synthetic material')
            with patch.object(apply_loop, 'HOME', home), patch.object(apply_loop, 'load_answer_bank', return_value=self.bank), patch('form_intel.probe_url', return_value=self.intel):
                path = apply_loop.build_packet(entry, dest_dir=str(Path(home, 'packets')))
            packet = json.loads(Path(path).read_text())
            self.assertFalse(packet['execution_authorized'])
            self.assertEqual(packet['status'], 'PREPARED_REVIEW_REQUIRED')


    def test_required_consent_controls_need_receipted_decision(self):
        labels = ['I acknowledge at-will employment', 'I agree to arbitration',
                  'I consent to a background check', 'I consent to data processing',
                  'I consent to interview recording']
        for kind in ('checkbox', 'radio', 'dropdown'):
            for label in labels:
                with self.subTest(kind=kind, label=label):
                    self.intel['questions'] = [{'label': label, 'type': kind, 'required': True,
                                               'options': ['Yes', 'No']}]
                    self.assertEqual(self.screen({'answers': {}}), 'PARK')

    def test_consent_uses_its_own_question_not_adjacent_approved_answer(self):
        self.intel['questions'] = [
            {'label': 'I agree to arbitration', 'type': 'checkbox', 'required': True, 'options': []},
            {'label': 'I acknowledge at-will employment', 'type': 'checkbox', 'required': True, 'options': []}]
        bank = {'answers': {'arbitration_agreement': 'Yes'}, '_provenance': {
            'arbitration_agreement': packet_contract.answer_receipt('Yes', 'synthetic', role_id='fixture-role')}}
        self.assertEqual(self.screen(bank), 'PARK')

    def test_receipted_consent_and_ordinary_options_remain_usable(self):
        for kind in ('checkbox', 'radio', 'dropdown'):
            for label, key in [('I acknowledge at-will employment', 'at_will_acknowledgment'),
                               ('I agree to arbitration', 'arbitration_agreement'),
                               ('I consent to a background check', 'background_check_consent'),
                               ('I consent to data processing', 'data_privacy_consent')]:
                with self.subTest(kind=kind, key=key):
                    self.intel['questions'] = [{'label': label, 'type': kind, 'required': True,
                                               'options': ['Yes', 'No']}]
                    bank = {'answers': {key: 'Yes'}, '_provenance': {
                        key: packet_contract.answer_receipt('Yes', 'synthetic consent', role_id='fixture-role')}}
                    self.assertEqual(self.screen(bank), 'CLEAN')
        self.intel['questions'] = [{'label': 'Preferred interface language', 'type': 'dropdown',
                                   'required': True, 'options': ['English', 'French']}]
        self.assertEqual(self.screen({'answers': {}}), 'CLEAN')


    def test_consent_control_rejects_invalid_receipt_authority(self):
        key = 'at_will_acknowledgment'
        for kind in ('checkbox', 'radio', 'dropdown'):
            for damage in ('empty', 'unreceipted', 'expired', 'wrong-role'):
                with self.subTest(kind=kind, damage=damage):
                    self.intel['questions'] = [{'label': 'I acknowledge at-will employment',
                        'type': kind, 'required': True, 'options': ['Yes', 'No']}]
                    bank = {'answers': {key: 'Yes'}, '_provenance': {
                        key: packet_contract.answer_receipt('Yes', 'synthetic consent', role_id='fixture-role')}}
                    if damage == 'empty': bank['answers'][key] = ''
                    elif damage == 'unreceipted': bank['_provenance'] = {}
                    elif damage == 'expired': bank['_provenance'][key]['expires_at'] = (self.now-timedelta(seconds=1)).isoformat()
                    else: bank['_provenance'][key]['scope'] = 'other-role'
                    self.assertEqual(self.screen(bank), 'PARK')

    def test_explicit_negative_fact_is_not_discarded_as_empty(self):
        self.intel['questions'] = [{'label': 'Are you authorized to work?', 'type': 'text',
                                   'required': True, 'options': []}]
        bank = {'answers': {'us_work_auth': False}, '_provenance': {
            'us_work_auth': packet_contract.answer_receipt(False, 'synthetic assertion', role_id='fixture-role')}}
        self.assertEqual(self.screen(bank), 'CLEAN')


if __name__ == '__main__':
    unittest.main()
