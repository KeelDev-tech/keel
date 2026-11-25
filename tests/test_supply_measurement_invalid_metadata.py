"""One malformed persisted enum must not suppress the supply census."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import keel
import muse_bridge as bridge
import queue_io
import supply_measurements as measured
from safe_io import atomic_json, utc_now


class InvalidMeasurementMetadataTest(unittest.TestCase):
    def test_bad_enum_is_unknown_and_other_rows_survive(self):
        cases = [('transport_class', []), ('transport_class', {'bad': True}),
                 ('signal', []), ('signal', {'bad': True})]
        for field, value in cases:
            with self.subTest(field=field, value=value), TemporaryDirectory() as tmp:
                home = Path(tmp)
                for name in bridge.QUEUES:
                    atomic_json(home / f'data/queues/{name}-queue.json', [])
                atomic_json(home / 'data/application-ledger.json', [])
                for name in ('answer_bank', 'policy', 'applicant_profile'):
                    atomic_json(home / f'data/{name}.json', {})
                (home / 'data/employer-blocklist.md').write_text('')
                (home / 'hidden_files').mkdir()
                (home / 'hidden_files/measurement.key').write_bytes(b'x' * 32)
                now = utc_now()

                def lead(n):
                    return {
                        'role_id': f'synthetic-role-{n}', 'company': 'Synthetic',
                        'title': 'Synthetic', 'status': 'PARKED-PENDING-VERIFICATION',
                        'application_url': f'https://job-boards.greenhouse.io/synthetic/jobs/{n}',
                        'fit_score': 80, 'action_band': 'APPLY',
                        'verification_attempt': {
                            'signal': 'NONE', 'transport_class': 'HTTP_429',
                            'observation_id': f'synthetic:role-{n}',
                            'evidence': {'request_attempts': [{
                                'request_number': 1, 'observed_at': now.isoformat(),
                                'source_url': 'https://boards-api.greenhouse.io/v1/boards/synthetic/jobs',
                            }]},
                        },
                    }

                bad, good = lead(1), lead(2)
                bad['verification_attempt'][field] = value
                atomic_json(home / 'data/queues/standard-queue.json', [bad, good])
                output, errors = StringIO(), StringIO()
                with (patch.object(queue_io, '_LOCK_PATH', str(home / 'hidden_files/queue.lock')),
                      redirect_stdout(output), redirect_stderr(errors)):
                    status = keel.main(['--home', str(home), 'supply-measurements',
                                        '--key-file', 'hidden_files/measurement.key'])
                self.assertEqual(status, 0, errors.getvalue())
                report = json.loads(output.getvalue())
                key = 'verification_signal' if field == 'signal' else field
                self.assertCountEqual([r[key] for r in report['roles']],
                                      ['UNKNOWN', good['verification_attempt'][field]])
                self.assertEqual(report['queue_rows'], 2)
                self.assertTrue(report['conservation_pass'])
                self.assertEqual(report['retained_dispatches'], 1)
                self.assertIsNone(report['launchable_ready'])
                self.assertIs(report['execution_authorized'], False)
                self.assertIs(report['submission_authorized'], False)
                self.assertNotIn('source_url', json.dumps(report))
                measured.validate(report)


if __name__ == '__main__':
    unittest.main()
