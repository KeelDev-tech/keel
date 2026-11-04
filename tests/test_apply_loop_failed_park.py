"""Failed parking must not count as preparation or starve the next candidate."""
import contextlib
import copy
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import apply_loop as loop
import ready_gate


class FailedParkProgressTests(unittest.TestCase):
    def run_case(self, *, phase='builder', park_ok=False, park_raises=False,
                 all_park=False, all_mode=False):
        with tempfile.TemporaryDirectory(prefix='keel-failed-park-') as directory:
            home = Path(directory)
            (home / 'data').mkdir()
            (home / 'data/employer-blocklist.md').write_text('# Blocked employers\n')
            resume = home / 'resume.txt'
            resume.write_text('Synthetic prepared material')
            entries = [{'role_id': f'fixture-{i}', 'company': 'Synthetic Employer',
                        'title': f'Synthetic Role {i}', 'status': 'READY',
                        'action_band': 'APPLY', 'fit_score': 90-i,
                        'ats_url': f'https://jobs.lever.co/synthetic/job-{i}',
                        'materials': {'resume': str(resume)}} for i in range(3)]
            original = copy.deepcopy(entries)
            bank = {'answers': {}, 'gates': {}, 'banded_questions': {}}
            visits, builds, parks, releases, events = [], [], [], [], []
            packets = home / 'packets'
            packets.mkdir()

            def eligible(entry, origin):
                visits.append(entry['role_id'])
                result = ready_gate.entry_admission(entry, origin, workspace=home)
                return result['allowed'], '; '.join(result['reasons'])

            def blocked(rid):
                return all_park or rid == 'fixture-0'

            def build(entry, **kwargs):
                rid = entry['role_id']
                builds.append(rid)
                if phase == 'builder' and blocked(rid):
                    raise loop.PacketPrescreenParked(['synthetic applicant question'])
                packet = {**{key: entry[key] for key in ('role_id', 'company', 'title', 'ats_url')},
                          'brief': 'Synthetic preparation, not execution authority',
                          'scope': 'preparation_only', 'execution_authorized': False,
                          'upload_files': [str(resume)]}
                ready_gate.seal_packet(packet, entry, bank, workspace=home)
                path = packets / (rid + '.json')
                path.write_text(json.dumps(packet))
                return str(path)

            def park(rid, reasons):
                parks.append(rid)
                if park_raises:
                    raise OSError('synthetic parking failure')
                return {'ok': park_ok, 'error': 'synthetic queue conflict' if not park_ok else None}

            def screen(packet, bank):
                return {'verdict': 'PARK' if blocked(packet['role_id']) else 'CLEAN',
                        'reasons': ['synthetic applicant question'] if blocked(packet['role_id']) else []}

            with contextlib.ExitStack() as stack:
                replacements = {
                    'HOME': str(home), 'PACKETS': str(packets),
                    'load_queue': lambda: entries, 'load_strategic_queue': lambda: [],
                    'load_answer_bank': lambda: bank, 'eligible': eligible,
                    '_buffered_fresh_packet': lambda rid: None,
                    '_launch_guard': lambda rid, *args: (True, 'task-'+rid, ''),
                    '_release_launch_lock': lambda *args: releases.append(args),
                    'build_packet': build, '_checked_prescreen': screen,
                }
                for name, value in replacements.items():
                    stack.enter_context(patch.object(loop, name, value))
                stack.enter_context(patch.object(loop.sys, 'argv', ['apply_loop.py'] + (['--all'] if all_mode else [])))
                stack.enter_context(patch.object(loop.rate_limits, 'is_allowed', return_value=(True, '')))
                stack.enter_context(patch.object(loop.prescreen, 'park_lead', side_effect=park))
                stack.enter_context(patch.object(loop.log_event, 'log', side_effect=lambda event, **kw: events.append((event, kw))))
                stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    loop.main()
            self.assertEqual(entries, original)
            self.assertEqual(releases, [(rid, 'task-'+rid) for rid in builds])
            self.assertFalse((packets / 'fixture-0.json').exists())
            if phase == 'recheck':
                self.assertTrue((packets / 'archive/fixture-0.json').exists())
            self.assertFalse(any(event in {'submission_claimed', 'submitted'} for event, _ in events))
            self.assertLessEqual(len(visits), len(entries))
            self.assertEqual(len(visits), len(set(visits)))
            return output.getvalue(), visits, builds, parks, events

    def test_failed_park_reaches_next_candidate_and_counts_only_its_packet(self):
        for phase in ('builder', 'recheck'):
            with self.subTest(phase=phase):
                output, visits, builds, parks, events = self.run_case(phase=phase)
                self.assertEqual(builds, ['fixture-0', 'fixture-1'])
                self.assertEqual(visits, builds)
                self.assertEqual(parks, ['fixture-0'])
                self.assertIn('PRESCREEN-PARK-FAILED fixture-0', output)
                self.assertIn('PACKET fixture-1', output)
                self.assertIn('Done: 1 preparation packet(s)', output)
                self.assertEqual([kw['role_id'] for event, kw in events if event == 'brief_built'], ['fixture-1'])

    def test_parking_exception_releases_lock_and_allows_next_candidate(self):
        for phase in ('builder', 'recheck'):
            with self.subTest(phase=phase):
                output, _, builds, _, _ = self.run_case(phase=phase, park_raises=True)
                self.assertEqual(builds, ['fixture-0', 'fixture-1'])
                self.assertIn('Done: 1 preparation packet(s)', output)

    def test_successful_park_still_consumes_default_slot_but_is_not_a_packet(self):
        for phase in ('builder', 'recheck'):
            with self.subTest(phase=phase):
                output, _, builds, parks, _ = self.run_case(phase=phase, park_ok=True)
                self.assertEqual(builds, ['fixture-0'])
                self.assertEqual(parks, ['fixture-0'])
                self.assertIn('Done: 0 preparation packet(s)', output)

    def test_all_failed_parks_visit_snapshot_once_and_count_zero(self):
        for phase in ('builder', 'recheck'):
            with self.subTest(phase=phase):
                output, _, builds, parks, events = self.run_case(phase=phase, all_park=True)
                self.assertEqual(builds, ['fixture-0', 'fixture-1', 'fixture-2'])
                self.assertEqual(parks, builds)
                self.assertIn('Done: 0 preparation packet(s)', output)
                self.assertFalse(any(event == 'brief_built' for event, _ in events))

    def test_explicit_all_mode_counts_packets_not_successful_parks(self):
        for phase in ('builder', 'recheck'):
            with self.subTest(phase=phase):
                output, _, builds, _, _ = self.run_case(phase=phase, park_ok=True, all_mode=True)
                self.assertEqual(builds, ['fixture-0', 'fixture-1', 'fixture-2'])
                self.assertIn('Done: 2 preparation packet(s)', output)


if __name__ == '__main__':
    unittest.main()
