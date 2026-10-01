"""Offline tests for the dashboard gate-blocked panel (no network)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))

import build_dashboard as bd  # noqa: E402


def make_home(files):
    tmp = tempfile.mkdtemp()
    for rel, content in files.items():
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return tmp


TELEMETRY = "\n".join([
    json.dumps({"ts": "2026-09-15T00:00:00Z", "event_type": "gate_blocked",
                "role_id": "R1", "details": {"gate": "needs_input"}}),
    json.dumps({"ts": "2026-09-15T01:00:00Z", "type": "gate_blocked",
                "role_id": "R2", "details": {"gate": "low_fit"}}),
    json.dumps({"ts": "2026-09-15T02:00:00Z", "event_type": "gate_blocked",
                "role_id": "R3", "details": {"gate": "needs_input"}}),
    json.dumps({"ts": "2026-09-15T03:00:00Z", "event_type": "lead_discovered",
                "role_id": "R4", "details": {}}),
    "not-json {{{",
])


class TestGatePanel(unittest.TestCase):
    def test_counts_by_gate(self):
        # The counts test uses complete valid input; corruption is tested below.
        home = make_home({"data/telemetry/events.jsonl": TELEMETRY.rsplit('\n', 1)[0]})
        data = bd.collect(home=home)
        self.assertEqual(data["gate_blocks"], {"needs_input": 2, "low_fit": 1})
        html = bd.render(data)
        self.assertIn("Gate blocks", html)
        self.assertIn("needs_input", html)
        self.assertIn("low_fit", html)

    def test_missing_telemetry_is_unknown_not_zero(self):
        home = make_home({})
        data = bd.collect(home=home)
        self.assertIsNone(data["gate_blocks"])
        html = bd.render(data)
        self.assertIn("Unknown", html)

    def test_malformed_lines_make_total_unknown(self):
        home = make_home({"data/telemetry/events.jsonl": "not-json {{{"})
        data = bd.collect(home=home)
        self.assertIsNone(data["gate_blocks"])
        self.assertTrue(any('malformed' in warning for warning in data['warnings']))



class TestSourceAvailability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def panel(self, document, heading):
        return document.split('<h2>' + heading + '</h2>', 1)[1].split('<h2>', 1)[0]

    def test_missing_sources_never_render_as_empty_activity(self):
        document = bd.render(bd.collect(self.home))
        for heading, absent in [
            ('Interview pipeline — needs you', 'No active interview threads.'),
            ('Recent submission claims', 'No submissions yet.'),
            ('Queues', 'No queues yet.'),
            ('Parked / blocked', 'Nothing parked.'),
        ]:
            with self.subTest(panel=heading):
                panel = self.panel(document, heading)
                self.assertIn('Unknown', panel)
                self.assertNotIn(absent, panel)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_corrupt_sources_never_render_as_empty_activity(self):
        for relative in ('data/application-ledger.json', 'data/parked.json'):
            self.write(relative, [])
            (self.home / relative).write_text('{broken')
        document = bd.render(bd.collect(self.home))
        for heading in ('Interview pipeline — needs you', 'Recent submission claims', 'Parked / blocked'):
            with self.subTest(panel=heading):
                self.assertIn('Unknown', self.panel(document, heading))
        self.assertIn('Data needs attention', document)

    def test_unreadable_parked_source_stays_unknown(self):
        self.write('data/parked.json', [])
        original = bd.read_json
        def read(path):
            if Path(path).name == 'parked.json':
                raise PermissionError('synthetic read denial')
            return original(path)
        with patch.object(bd, 'read_json', side_effect=read):
            data = bd.collect(self.home)
        self.assertFalse(data['parked_known'])
        self.assertIn('Unknown', self.panel(bd.render(data), 'Parked / blocked'))

    def test_successfully_read_empty_sources_retain_empty_messages(self):
        self.write('data/application-ledger.json', [])
        self.write('data/parked.json', [])
        for queue in ('standard', 'strategic', 'needs_input'):
            self.write('data/queues/' + queue + '-queue.json', [])
        data = bd.collect(self.home)
        document = bd.render(data)
        self.assertTrue(data['parked_known'])
        for heading, text in [
            ('Interview pipeline — needs you', 'No active interview threads.'),
            ('Recent submission claims', 'No submissions yet.'),
            ('Parked / blocked', 'Nothing parked.'),
        ]:
            panel = self.panel(document, heading)
            self.assertIn(text, panel)
            self.assertNotIn('Unknown', panel)
        self.assertEqual(data['queue_total'], 0)

    def test_undated_submission_claims_are_not_reported_as_absent(self):
        self.write('data/application-ledger.json', [{'status': 'SUBMITTED', 'company': 'Synthetic'}])
        data = bd.collect(self.home)
        self.assertEqual(len(data['submitted']), 1)
        panel = self.panel(bd.render(data), 'Recent submission claims')
        self.assertNotIn('No submissions yet.', panel)
        self.assertIn('Submission claims exist, but no dates are available.', panel)

    def test_mixed_dated_and_undated_claims_retain_notice(self):
        self.write('data/application-ledger.json', [
            {'status': 'SUBMITTED', 'company': 'Dated synthetic',
             'submitted_at': '2026-01-01T00:00:00Z'},
            {'status': 'SUBMITTED', 'company': 'Undated synthetic'},
        ])
        data = bd.collect(self.home)
        panel = self.panel(bd.render(data), 'Recent submission claims')
        self.assertEqual(len(data['submitted']), 2)
        self.assertIn('Dated synthetic', panel)
        self.assertIn('1 submission claim has no date and is not shown above.', panel)
        self.assertNotIn('No submissions yet.', panel)

    def test_undated_count_is_independent_of_recent_row_limit(self):
        self.write('data/application-ledger.json', [
            {'status': 'SUBMITTED', 'company': 'Dated synthetic ' + str(i),
             'submitted_at': '', 'date_submitted': f'2026-01-{i+1:02d}T00:00:00Z'}
            for i in range(9)
        ] + [
            {'status': 'SUBMITTED', 'submitted_at': '', 'date_submitted': None},
            {'status': 'SUBMITTED', 'submitted_at': None},
        ])
        data = bd.collect(self.home)
        self.assertEqual(len(data['recent']), 8)
        self.assertEqual(len(data['submitted']), 11)
        panel = self.panel(bd.render(data), 'Recent submission claims')
        self.assertEqual(panel.count('Dated synthetic'), 8)
        self.assertIn('2 submission claims have no date and are not shown above.', panel)

    def test_dated_claims_and_other_statuses_do_not_trigger_undated_notice(self):
        self.write('data/application-ledger.json', [
            {'status': 'SUBMITTED', 'submitted_at': '2026-01-01T00:00:00Z'},
            {'status': 'SUBMITTED', 'date_submitted': '2026-01-02T00:00:00Z'},
            {'status': 'INTERVIEW_INVITED'},
        ])
        panel = self.panel(bd.render(bd.collect(self.home)), 'Recent submission claims')
        self.assertNotIn('no date', panel)
        self.assertNotIn('not shown above', panel)

    def test_known_records_remain_visible_and_escaped(self):
        label = '<img src=x onerror=alert(1)>'
        self.write('data/application-ledger.json', [
            {'status': 'SUBMITTED', 'company': label, 'submitted_at': '2026-01-01T00:00:00Z'},
            {'status': 'INTERVIEW_INVITED', 'company': label},
        ])
        self.write('data/parked.json', [{'title': label, 'detail': 'Synthetic hold'}])
        document = bd.render(bd.collect(self.home))
        for heading in ('Interview pipeline — needs you', 'Recent submission claims', 'Parked / blocked'):
            panel = self.panel(document, heading)
            self.assertIn('&lt;img', panel)
            self.assertNotIn('<img', panel)
            self.assertNotIn('Unknown', panel)
        self.assertIn('Provider verification is not connected', document)
        self.assertIn('does not submit an application or authorize an executor', document)


if __name__ == "__main__":
    unittest.main()
