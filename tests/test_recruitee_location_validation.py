"""Synthetic malformed-location isolation through the actual discovery sweep."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
from ats_discovery import recruitee, sweep


class RecruiteeLocationTests(unittest.TestCase):
    def offer(self, **fields):
        return {'slug': 'synthetic-role', 'title': 'Operations Manager',
                'status': 'published', **fields}

    def test_invalid_location_fields_rejected_without_reflection(self):
        for field in ('city', 'country_code', 'location'):
            for bad in (123, 0, 1.5, True, False, [], {}, ['SYNTHETIC-CANARY'], {'SYNTHETIC-CANARY': 1}):
                with self.subTest(field=field, bad=bad):
                    raw = self.offer(**{field: bad})
                    before = copy.deepcopy(raw)
                    with self.assertRaisesRegex(ValueError, '^invalid offer location type$'):
                        recruitee.normalize(raw, 'synthetic', 'Example Company')
                    self.assertEqual(raw, before)

    def test_valid_location_values_keep_existing_contract(self):
        for city in (None, '', 'Napa', '  Napa  ', 'Québec'):
            for country in (None, '', 'US'):
                for label in (None, '', 'Remote'):
                    raw = self.offer(city=city, country_code=country, location=label)
                    before = copy.deepcopy(raw)
                    result = recruitee.normalize(raw, 'synthetic', 'Example Company')
                    self.assertEqual(result['location'], {'city': city or '', 'country': country or '',
                        'raw': label or ', '.join(v for v in (city, country) if v), 'region': '', 'remote': None})
                    self.assertEqual(raw, before)
        self.assertEqual(recruitee.normalize(self.offer(), 'synthetic', 'Example Company')['location']['raw'], '')

    def test_sweep_preserves_valid_neighbor_and_sanitized_skip(self):
        for field in ('city', 'country_code', 'location'):
            with self.subTest(field=field):
                offers = [self.offer(**{field: ['SYNTHETIC-CANARY']}),
                          self.offer(slug='valid-neighbor', city='Napa', country_code='US')]
                context = {'blocked': [], 'ledger': [], 'queue': [], 'desc_hashes': {}, 'staged': []}
                with patch.object(sweep, 'paced_get', return_value=(200, 'application/json', json.dumps({'offers': offers}))), \
                     patch.object(sweep.time, 'sleep'):
                    report = sweep.sweep_platform('recruitee', recruitee,
                        {'synthetic': {'status': 'active', 'employer': 'Example Company'}},
                        context, SimpleNamespace(no_triage=True))
                self.assertEqual(report['staged'], 1)
                self.assertEqual(report['completed_boards'], 1)
                self.assertEqual(report['raw'], 2)
                self.assertEqual(report['skipped'], ['synthetic: normalize invalid offer location type'])
                self.assertEqual(context['staged'][0]['source_job_id'], 'valid-neighbor')
                self.assertEqual(context['staged'][0]['status'], 'PARKED')
                self.assertEqual(context['staged'][0]['action_band'], 'PARKED')
                self.assertNotIn('fit_score', context['staged'][0])

    def test_label_does_not_hide_invalid_structured_location(self):
        for field in ('city', 'country_code'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                recruitee.normalize(self.offer(location='Remote', **{field: ['bad']}), 'synthetic', 'Example Company')

if __name__ == '__main__':
    unittest.main()
