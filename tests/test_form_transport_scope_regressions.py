"""Passive form hints and transport routing remain scoped to the actual URL."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import api_direct_detect
import form_intel
import prescreen


class FormTransportScopeRegressions(unittest.TestCase):
    def tearDown(self):
        form_intel._form_intel_memo.clear()

    def test_direct_parser_requires_exact_https_authority(self):
        invalid = [
            'https://fakeboards.greenhouse.io/demo/jobs/12',
            'https://evil.example/boards.greenhouse.io/demo/jobs/12',
            'http://boards.greenhouse.io/demo/jobs/12',
            'https://person@boards.greenhouse.io/demo/jobs/12',
            'https://boards.greenhouse.io:8443/demo/jobs/12',
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                api_direct_detect.parse_board_url(url)
        self.assertEqual(api_direct_detect.parse_board_url('https://boards.greenhouse.io/demo/jobs/12'), ('demo', '12'))

    def test_candidate_reader_uses_bounded_public_transport(self):
        with patch.object(api_direct_detect, 'public_urlopen', return_value=io.BytesIO(b'fixture')) as safe:
            with patch.object(api_direct_detect.urllib.request, 'urlopen', side_effect=AssertionError('raw transport prohibited')):
                self.assertEqual(api_direct_detect.fetch_html('https://boards.greenhouse.io/demo/jobs/12'), 'fixture')
        self.assertEqual(safe.call_count, 1)

    def test_lookalike_host_only_yields_nested_passive_hints(self):
        html = '<label for="x">Unexpected mandatory commitment</label><input id="x" required>'
        for url in ['https://fakeboards.greenhouse.io/demo/jobs/12',
                    'https://evil.example/boards.greenhouse.io/demo/jobs/12']:
            with self.subTest(url=url), patch.object(form_intel, 'fetch', return_value=html):
                with patch.object(form_intel, 'greenhouse_embed_intel', side_effect=AssertionError('not a provider host')):
                    result = form_intel.probe_url(url)
            self.assertTrue(result['advisory'])
            self.assertFalse(result['extraction_complete'])
            self.assertEqual(result['questions'], [])
            self.assertEqual(len(result['passive_intel']['questions']), 1)
            self.assertNotIn('Unexpected mandatory commitment', prescreen.render_probe_brief(result))

    def test_distinct_roles_do_not_share_question_cache(self):
        def probe(board, token):
            return {'ats': 'greenhouse', 'form_url': token, 'questions': [{'label': 'Question ' + token}]}
        with patch.object(form_intel, 'greenhouse_embed_intel', side_effect=probe) as fetch:
            first = form_intel.probe_url('https://boards.greenhouse.io/demo/jobs/1', employer='Fixture')
            second = form_intel.probe_url('https://boards.greenhouse.io/demo/jobs/2', employer='Fixture')
            again = form_intel.probe_url('https://boards.greenhouse.io/demo/jobs/1', employer='Fixture')
        self.assertNotEqual(first['questions'], second['questions'])
        self.assertEqual(first, again)
        self.assertEqual(fetch.call_count, 2)

    def test_embed_dispatch_parses_parameter_order_and_rejects_duplicates(self):
        with patch.object(form_intel, 'greenhouse_embed_intel', return_value={'fixture': True}) as fetch:
            self.assertEqual(form_intel.probe_url('https://job-boards.greenhouse.io/embed/job_app?token=12&for=demo'), {'fixture': True})
            fetch.assert_called_once_with('demo', '12')
        with self.assertRaises(ValueError):
            form_intel.probe_url('https://job-boards.greenhouse.io/embed/job_app?token=12&for=demo&for=other')


if __name__ == '__main__': unittest.main()
