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


    def test_consent_statement_in_required_option_needs_decision(self):
        for kind in ('checkbox', 'radio', 'dropdown'):
            with self.subTest(kind=kind):
                self.intel['questions'] = [{'label': 'Employment terms', 'type': kind,
                    'required': True, 'options': ['I acknowledge at-will employment']}]
                self.assertEqual(self.screen({'answers': {}}), 'PARK')


    def test_malformed_bank_does_not_become_empty_authority(self):
        self.intel['questions'] = []
        for bank in ([], 'malformed', {'answers': []}, {'answers': None}):
            with self.subTest(bank=bank):
                self.assertEqual(self.screen(bank), 'PARK')
        self.assertEqual(self.screen({'answers': {}}), 'CLEAN')


    def test_adjacent_receipted_identity_and_consent_both_remain_usable(self):
        self.intel['questions'].append({'label': 'I agree to arbitration', 'type': 'checkbox',
                                       'required': True, 'options': ['Yes', 'No']})
        bank = copy.deepcopy(self.bank)
        bank['answers']['arbitration_agreement'] = 'Yes'
        bank['_provenance']['arbitration_agreement'] = packet_contract.answer_receipt(
            'Yes', 'synthetic consent', role_id='fixture-role')
        self.assertEqual(self.screen(bank), 'CLEAN')


if __name__ == '__main__':
    unittest.main()


class CaptureToPrescreenTests(unittest.TestCase):
    def setUp(self):
        PrescreenAnswerAuthorityTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'bank.json'
        self.path.write_text(json.dumps({'answers': {}, '_provenance': {}}))
        import field_question_protocol as frp
        self.frp = frp
        for key, value in [('BANK_PATH', str(self.path)), ('QUEUE_DIR', str(self.root)),
                           ('BACKLOG_PATH', str(self.root / 'backlog.jsonl')),
                           ('HITCOUNTS_PATH', str(self.root / 'hits.json'))]:
            p = patch.object(frp, key, value, create=True)
            p.start(); self.addCleanup(p.stop)

    def capture(self, writer, *, key='first_name', question='What is your first name?', scope='global', answer='Riley'):
        if writer == 'tray':
            import tray_answer
            result = tray_answer.apply_bank_write(str(self.path), key, answer, scope,
                {'key': 'synthetic-card', 'question': question}, 'synthetic reply', employers=set())
            return result
        return self.frp.close_question(question, answer, bank_key=key,
            provenance='synthetic applicant own words', bank_path=str(self.path),
            backlog_path=str(self.root / 'backlog.jsonl'), scope=scope)

    def screen(self, bank):
        return prescreen.screen_packet(self.packet, bank, {})['verdict']

    def test_new_human_captures_clear_required_question(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                self.capture(writer)
                bank = json.loads(self.path.read_text())
                self.assertEqual(self.screen(bank), 'CLEAN')
                self.assertEqual(bank['_provenance']['first_name']['asserted_by'], 'applicant')

    def test_captured_answers_remain_value_scope_and_time_bound(self):
        for writer in ('tray', 'frp'):
            self.capture(writer, scope='employer:Fixture')
            captured = json.loads(self.path.read_text())
            self.assertEqual(self.screen(captured), 'CLEAN')
            for damage in ('value', 'scope', 'question-metadata', 'expired', 'wrong-role', 'unreceipted'):
                with self.subTest(writer=writer, damage=damage):
                    bank = copy.deepcopy(captured)
                    if damage == 'value': bank['answers']['first_name']['value'] = 'Changed'
                    elif damage == 'scope': bank['answers']['first_name']['scope'] = 'global'
                    elif damage == 'question-metadata': bank['answers']['first_name']['question_patterns'] = ['Different obligation']
                    elif damage == 'expired': bank['_provenance']['first_name']['expires_at'] = (utc_now()-timedelta(seconds=1)).isoformat()
                    elif damage == 'wrong-role': bank['_provenance']['first_name']['scope'] = 'other-role'
                    else: bank['_provenance'] = {}
                    self.assertEqual(self.screen(bank), 'PARK')
            self.packet['company'] = 'Other'
            self.assertEqual(self.screen(captured), 'PARK')
            self.packet['company'] = 'Fixture'

    def test_custom_frp_capture_survives_validated_projection(self):
        question = 'Which synthetic workflow have you practiced?'
        self.capture('frp', key='synthetic_workflow', question=question)
        self.packet['frp_enabled'] = True
        self.intel['questions'][0]['label'] = question
        self.assertEqual(self.screen(json.loads(self.path.read_text())), 'CLEAN')
        self.assertEqual(self.packet['frp_derived_answers'][question]['answer'], 'Riley')

    def test_ambiguous_consent_does_not_gain_receipt_or_close_backlog(self):
        question = 'I agree to arbitration'
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                self.path.write_text('{"answers": {}, "_provenance": {}}')
                self.capture(writer, key='arbitration_agreement', question=question, scope=None, answer='Yes')
                bank = json.loads(self.path.read_text())
                self.assertNotIn('arbitration_agreement', bank['answers'])
                self.assertNotIn('asserted_by', bank.get('_provenance', {}).get('arbitration_agreement', {}))

    def test_malformed_banks_are_not_replaced_or_authorized(self):
        for writer in ('tray', 'frp'):
            for content in ('{bad', '[]', '{"answers": []}', '{"answers": {}, "_provenance": []}'):
                with self.subTest(writer=writer, content=content):
                    self.path.write_text(content)
                    try:
                        self.capture(writer)
                    except ValueError:
                        pass
                    self.assertEqual(self.path.read_text(), content)

    def test_explicit_consent_scope_is_required_and_employer_bound(self):
        self.intel['questions'][0].update(label='I agree to arbitration', type='checkbox')
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                self.capture(writer, key='arbitration_agreement', question='I agree to arbitration', scope='employer:Fixture', answer='Yes')
                bank = json.loads(self.path.read_text())
                self.assertEqual(self.screen(bank), 'CLEAN')
                self.packet['company'] = 'Other'
                self.assertEqual(self.screen(bank), 'PARK')
                self.packet['company'] = 'Fixture'

    def test_capture_does_not_receipt_unrelated_legacy_or_derived_answers(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                legacy = {'answers': {'legacy_fact': 'Synthetic old fact'},
                          '_provenance': {'legacy_fact': {'source': 'old note'}}}
                self.path.write_text(json.dumps(legacy))
                self.capture(writer)
                bank = json.loads(self.path.read_text())
                self.assertEqual(bank['answers']['legacy_fact'], legacy['answers']['legacy_fact'])
                self.assertEqual(bank['_provenance']['legacy_fact'], legacy['_provenance']['legacy_fact'])
        self.frp.ensure_bank_key('derived_fact', 'Synthetic derivation', 'derived source', bank_path=str(self.path))
        self.assertNotIn('asserted_by', json.loads(self.path.read_text())['_provenance']['derived_fact'])

    def test_frp_dry_run_and_ambiguous_consent_leave_backlog_open(self):
        question = 'I agree to arbitration'
        backlog = self.root / 'backlog.jsonl'
        row = {'norm': self.frp.normalize_question(question), 'status': 'open', 'role_id': 'fixture-role'}
        backlog.write_text(json.dumps(row)+'\n')
        before = self.path.read_bytes(), backlog.read_bytes()
        result = self.frp.close_question(question, 'Yes', bank_key='arbitration_agreement',
            bank_path=str(self.path), backlog_path=str(backlog))
        self.assertTrue(result['scope_required'])
        self.assertEqual((self.path.read_bytes(), backlog.read_bytes()), before)
        self.frp.close_question(question, 'Yes', bank_key='arbitration_agreement', scope='employer:Fixture',
            bank_path=str(self.path), backlog_path=str(backlog), dry_run=True)
        self.assertEqual((self.path.read_bytes(), backlog.read_bytes()), before)

    def test_answer_placeholders_and_no_ai_still_park_after_capture(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                self.capture(writer)
                bank = json.loads(self.path.read_text())
                entry = bank['answers']['first_name']
                entry['value'] = 'YOUR_FIRST_NAME'
                bank['_provenance']['first_name'] = packet_contract.answer_receipt(entry, 'synthetic')
                self.assertEqual(self.screen(bank), 'PARK')
        self.intel['questions'][0].update(label='I certify no AI assistance was used', type='checkbox')
        self.capture('tray', key='information_truthfulness_attestation', question='I certify no AI assistance was used')
        self.assertEqual(self.screen(json.loads(self.path.read_text())), 'PARK')

    def test_frp_does_not_inherit_consent_authority_or_widen_fact_scope(self):
        self.capture('frp', scope='employer:Fixture')
        self.capture('frp', scope=None)
        bank = json.loads(self.path.read_text())
        self.assertEqual(bank['answers']['first_name']['scope'], 'employer:Fixture')
        self.capture('frp', key='arbitration_agreement', question='I agree to arbitration', scope='global', answer='Yes')
        before = self.path.read_bytes()
        result = self.capture('frp', key='arbitration_agreement', question='I agree to arbitration', scope=None, answer='Yes')
        self.assertTrue(result['scope_required'])
        self.assertEqual(before, self.path.read_bytes())

    def test_fresh_plain_fact_capture_uses_existing_scope_policy(self):
        for writer in ('tray', 'frp'):
            with self.subTest(writer=writer):
                self.path.write_text('{"answers": {}, "_provenance": {}}')
                self.capture(writer, scope=None)
                self.assertEqual(self.screen(json.loads(self.path.read_text())), 'CLEAN')
