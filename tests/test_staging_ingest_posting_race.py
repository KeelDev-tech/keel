"""Merge-time posting identity checks with real synthetic queue transactions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


# A subprocess binds every imported KEEL_HOME path to this test's fresh home.
# Only the scheduling point is wrapped; validation, locks, I/O and audit run normally.
PROBE = r'''
import copy, json, os, pathlib, socket, sys
from unittest.mock import patch
import staging_ingest as si
import queue_io
from safe_io import atomic_json, read_json
root = pathlib.Path(os.environ['KEEL_HOME'])
options = json.loads(sys.argv[1])
queue_io.set_lock_path(str(root / 'queue.lock'))
queue = root / 'data/queues/standard-queue.json'
atomic_json(queue, [])
atomic_json(root / 'data/application-ledger.json', [])
staged = root / 'hidden_files/discovery-staging/synthetic-leads.json'
staged.parent.mkdir(parents=True)
row = {'role_id': 'SYNTHETIC-NEW', 'company': 'Synthetic Employer',
       'title': 'Operations Manager', 'location': '', 'action_band': 'HOLD',
       'status': 'PARKED', 'fit_score': 80,
       'application_url': options.get('candidate_url', 'https://job-boards.greenhouse.io/fixture/jobs/123'),
       'execution_authorized': False}
held = {**row, 'role_id': 'SYNTHETIC-REVIEWED', 'status': 'PARKED-NEEDS-INPUT',
        'holds': ['synthetic-human-hold'], 'materials': {'resume': 'data/resumes/synthetic.txt'},
        'review_evidence': {'ref': 'synthetic:operator'},
        'application_url': options.get('held_url', row['application_url'])}
if options.get('same_role'):
    held['role_id'] = row['role_id']
if options.get('conflict'):
    held['posting_url'] = 'https://job-boards.greenhouse.io/fixture/jobs/456'
held_before = copy.deepcopy(held)
atomic_json(staged, [row])
staged_bytes = staged.read_bytes()
if options.get('before'):
    atomic_json(queue, [held])
original = si.materials_intake_gate
calls = []
def concurrent(entries, pipe):
    result = original(entries, pipe)
    if not options.get('before'):
        with queue_io.queue_lock(owner='synthetic-concurrent-intake'):
            if options.get('unregistered'):
                atomic_json(root / 'data/queues/unregistered-queue.json', [held])
            else:
                atomic_json(queue, [held])
        calls.append(True)
    if options.get('identity_failure'):
        import dedupe_index
        patch.object(dedupe_index, 'keys_for_urls',
                     side_effect=RuntimeError('synthetic identity failure')).start()
    return result
with patch.object(si, 'materials_intake_gate', side_effect=concurrent), \
     patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
     patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')):
    if options.get('identity_failure'):
        try:
            si.run_ingest([str(staged)], 'synthetic-race', dry_run=False, pipe=str(root))
        except RuntimeError as exc:
            assert str(exc) == 'synthetic identity failure'
        else:
            raise AssertionError('identity failure must stop merge')
        assert read_json(queue) == [held_before]
        assert staged.read_bytes() == staged_bytes
        with queue_io.queue_lock(timeout=0.2, owner='synthetic-release-check'):
            pass
        print(json.dumps({'failed_closed': True}))
        sys.exit(0)
    audit = si.run_ingest([str(staged)], 'synthetic-race', dry_run=False, pipe=str(root))
rows = read_json(queue)
if options.get('unregistered'):
    assert read_json(root / 'data/queues/unregistered-queue.json') == [held_before]
else:
    assert rows[0] == held_before
assert held == held_before
assert calls == ([] if options.get('before') else [True])
with queue_io.queue_lock(timeout=0.2, owner='synthetic-release-check'):
    pass
# Archive preserves the original staged evidence, including its distinct role ID.
archives = [p for p in staged.parent.rglob(staged.name) if p != staged]
assert len(archives) == 1 and archives[0].read_bytes() == staged_bytes
assert not staged.exists()
replay = None
if options.get('replay'):
    atomic_json(staged, [row])
    with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')):
        replay = si.run_ingest([str(staged)], 'synthetic-replay', dry_run=False, pipe=str(root))
    assert read_json(queue) == rows
events_path = root / 'data/telemetry/events.jsonl'
events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
print(json.dumps({'audit': audit, 'rows': rows, 'replay': replay, 'events': events}))
'''


class TestStagingPostingRace(unittest.TestCase):
    def run_case(self, **options):
        with tempfile.TemporaryDirectory(prefix='keel-posting-race-') as home:
            env = dict(os.environ, KEEL_HOME=home, PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'engines'))
            result = subprocess.run([sys.executable, '-B', '-c', PROBE, json.dumps(options)],
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)

    def assert_suppressed(self, result):
        audit = result['audit']
        self.assertEqual(audit['ingested'], 0)
        self.assertEqual(audit['triage_fresh'], 1)
        self.assertEqual(len(audit['merge_suppressed']), 1)
        self.assertEqual(len(result['rows']), 1)
        suppressed = audit['merge_suppressed'][0]
        self.assertEqual(suppressed['role_id'], 'SYNTHETIC-NEW')
        self.assertIn('duplicate posting identity', suppressed['reason'])
        self.assertEqual(suppressed['queued_role_id'], 'SYNTHETIC-REVIEWED')
        self.assertTrue(audit['identity_coverage']['complete'])
        self.assertEqual(audit['ready_before'], audit['ready_after'])
        self.assertTrue(suppressed['delta_vs_queued'])
        rejected = [e for e in result['events'] if e['event_type'] == 'staged_rejected'
                    and e['details'].get('batch') == 'synthetic-race']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['details']['count'], 1)
        self.assertEqual(rejected[0]['details']['role_ids'], ['SYNTHETIC-NEW'])
        admitted = [e for e in result['events'] if e['event_type'] == 'staged_ingested']
        self.assertEqual(admitted, [])

    def test_concurrent_exact_posting_preserves_reviewed_row(self):
        self.assert_suppressed(self.run_case())

    def test_canonical_aliases_are_suppressed(self):
        for left, right in [
            ('https://boards.greenhouse.io/fixture/jobs/123',
             'https://job-boards.greenhouse.io/embed/job_app?for=fixture&token=123'),
            ('https://jobs.lever.co/fixture/abc',
             'https://jobs.lever.co/fixture/abc/apply?utm_source=synthetic'),
            ('https://jobs.ashbyhq.com/fixture/abc',
             'https://jobs.ashbyhq.com/fixture/abc/apply'),
            ('https://example.com/job?id=123',
             'https://EXAMPLE.com:443/job?id=123&utm_source=synthetic'),
        ]:
            with self.subTest(left=left):
                self.assert_suppressed(self.run_case(held_url=left, candidate_url=right))

    def test_distinct_postings_with_same_company_title_survive(self):
        result = self.run_case(held_url='https://job-boards.greenhouse.io/fixture/jobs/456')
        self.assertEqual(result['audit']['ingested'], 1)
        self.assertEqual(result['audit']['merge_suppressed'], [])
        self.assertEqual(len(result['rows']), 2)
        self.assertFalse(result['rows'][1]['execution_authorized'])
        self.assertEqual(result['rows'][1]['action_band'], 'HOLD')

    def test_same_role_id_keeps_existing_suppression(self):
        result = self.run_case(same_role=True)
        self.assertEqual(result['audit']['ingested'], 0)
        self.assertIn('duplicate role_id', result['audit']['merge_suppressed'][0]['reason'])
        self.assertEqual(len(result['rows']), 1)

    def test_pretriage_duplicate_is_still_suppressed(self):
        result = self.run_case(before=True)
        self.assertEqual(result['audit']['ingested'], 0)
        self.assertEqual(len(result['rows']), 1)

    def test_suppressed_posting_replay_is_idempotent(self):
        result = self.run_case(replay=True)
        self.assert_suppressed(result)
        self.assertEqual(result['replay']['ingested'], 0)

    def test_identity_failure_preserves_queue_staging_and_releases_lock(self):
        self.assertTrue(self.run_case(identity_failure=True)['failed_closed'])

    def test_unregistered_queue_does_not_expand_dedupe_scope(self):
        result = self.run_case(unregistered=True)
        self.assertEqual(result['audit']['ingested'], 1)
        self.assertEqual(result['audit']['merge_suppressed'], [])
        self.assertEqual([r['role_id'] for r in result['rows']], ['SYNTHETIC-NEW'])

    def test_conflicting_posting_ids_are_not_duplicate_proof(self):
        result = self.run_case(conflict=True)
        self.assertEqual(result['audit']['ingested'], 1)
        self.assertEqual(len(result['rows']), 2)


if __name__ == '__main__':
    unittest.main()
