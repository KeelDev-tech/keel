"""Fair inspection coverage, durable metadata boundaries, and real tray rotation."""
import copy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from engines import qresolve_schedule as schedule

ROOT = Path(__file__).resolve().parents[1]


def hashed(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def candidates(count):
    return [(hashed(index), hashed(('revision', index))) for index in range(count)]


class FairScheduleTests(unittest.TestCase):
    def test_more_than_fifty_persistent_cards_receive_a_turn(self):
        rows, state, covered = candidates(123), schedule.empty_state(), set()
        for _ in range(3):
            selected, state, report = schedule.plan(rows, state, 50)
            covered.update(selected)
            self.assertEqual(len(selected), 50)
            self.assertEqual(report['fairness_reserved'], 25)
        self.assertEqual(covered, set(range(123)))

    def test_changed_cards_get_priority_without_starving_unchanged_cards(self):
        rows = candidates(80)
        _, state, _ = schedule.plan(rows, schedule.empty_state(), 100)
        covered = set()
        for generation in range(16):
            churn = [(identity, hashed((generation, index)) if index < 10 else revision)
                     for index, (identity, revision) in enumerate(rows)]
            selected, state, report = schedule.plan(churn, state, 10)
            covered.update(selected)
            self.assertTrue(any(index < 10 for index in selected))
            self.assertLessEqual(report['change_priority_selected'], 5)
        self.assertEqual(covered, set(range(80)))

    def test_constant_new_arrivals_cannot_jump_existing_waiters_forever(self):
        rows = candidates(40)
        _, state, _ = schedule.plan(rows, schedule.empty_state(), 50)
        original, covered = {identity for identity, _ in rows}, set()
        for generation in range(8):
            newcomers = [(hashed(('new', generation, index)), hashed(index)) for index in range(20)]
            rows = newcomers + rows
            selected, state, _ = schedule.plan(rows, state, 10)
            covered.update(rows[index][0] for index in selected)
        self.assertTrue(original <= covered)

    def test_low_ranked_new_card_gets_priority_and_changed_waiter_keeps_priority(self):
        rows = candidates(20)
        _, state, _ = schedule.plan(rows, schedule.empty_state(), 20)
        changed = [(identity, hashed(('changed', index))) for index, (identity, _) in enumerate(rows)]
        selected, state, report = schedule.plan(changed, state, 4)
        self.assertEqual(report['new_or_changed_cards'], 20)
        selected2, _, report2 = schedule.plan(changed, state, 4)
        self.assertEqual(report2['new_or_changed_cards'], 16)
        self.assertTrue(set(selected).isdisjoint(selected2))
        added = rows + [(hashed('new lowest value'), hashed('new revision'))]
        _, fresh_state, _ = schedule.plan(rows, schedule.empty_state(), 20)
        chosen, _, report = schedule.plan(added, fresh_state, 4)
        self.assertIn(20, chosen)
        self.assertEqual(report['change_priority_selected'], 1)

    def test_single_slot_is_fair_and_preview_does_not_mutate_input(self):
        rows, state = candidates(8), schedule.empty_state()
        covered = []
        for _ in range(8):
            before = copy.deepcopy(state)
            first = schedule.plan(rows, state, 1)
            self.assertEqual(state, before)
            self.assertEqual(first, schedule.plan(rows, state, 1))
            selected, state, _ = first
            covered.extend(selected)
        self.assertEqual(covered, list(range(8)))

    def test_inactive_entries_are_pruned_and_active_revisions_are_not_authority(self):
        rows = candidates(3)
        _, state, _ = schedule.plan(rows, schedule.empty_state(), 3)
        selected, replacement, _ = schedule.plan(rows[1:], state, 1)
        self.assertNotIn(rows[0][0], replacement['entries'])
        self.assertEqual(len(replacement['entries']), 2)
        self.assertEqual(set(replacement), {'schema', 'generation', 'entries'})
        self.assertEqual(selected, [0])

    def test_invalid_or_over_capacity_state_never_resets_history(self):
        rows = candidates(2)
        _, state, _ = schedule.plan(rows, schedule.empty_state(), 1)
        corruptions = [None, [], {}, dict(state, generation=True),
                       dict(state, schema='unknown'), dict(state, authorization=True)]
        for field, value in [('last_scanned', 99), ('first_seen', 0), ('revision', 'bad')]:
            bad = copy.deepcopy(state)
            bad['entries'][rows[0][0]][field] = value
            corruptions.append(bad)
        for bad in corruptions:
            with self.subTest(state=bad), self.assertRaises(ValueError):
                schedule.plan(rows, bad, 1)
        with self.assertRaises(ValueError):
            schedule.plan(rows + rows[:1], state, 1)
        with mock.patch.object(schedule, 'MAX_CARDS', 1), self.assertRaises(ValueError):
            schedule.plan(rows, schedule.empty_state(), 1)
        with self.assertRaises(ValueError):
            schedule.plan([], dict(schedule.empty_state(), generation=schedule.MAX_GENERATION), 1)


class DurableScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keel-fair-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.rows = [{'role_id': 'fixture-' + str(index), 'company': 'Example Employer',
                      'title': 'Example Role', 'fit_score': 95 - (index % 30),
                      'status': 'NEEDS-INPUT',
                      'unresolved': ['Preferred shift for assignment ' + chr(97 + index // 26) + chr(97 + index % 26) + '?'],
                      'queue_notes': [], 'last_verify_attempt': '2026-01-01'}
                     for index in range(75)]
        bank = {'answers': {str(index): {'value': 'Example shift ' + str(index),
                'scope': 'global', 'question': row['unresolved'][0],
                'provenance': "the applicant's own words " + date.today().isoformat()}
                for index, row in enumerate(self.rows)}}
        self.write('data/answer_bank.json', bank)
        self.write('data/queues/needs_input-queue.json', self.rows)
        self.write('data/queues/standard-queue.json', [])
        self.write('data/application-ledger.json', [])

    def write(self, name, value):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def read(self, name):
        return json.loads((self.home / name).read_text())

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest())
                for path in self.home.rglob('*') if path.is_file()}

    def cli(self, *args):
        env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
        env.update(PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-B', '-S', str(ROOT / 'keel.py'),
            '--home', str(self.home), 'qresolve', *args], env=env, cwd=ROOT,
            capture_output=True, text=True, timeout=30)

    def success(self, *args):
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_live_rotates_past_fifty_and_retains_earlier_evidence_drafts(self):
        before = (self.home / 'data/queues/needs_input-queue.json').read_bytes()
        one = self.success('--live')
        first = self.read('hidden_files/qresolve-proposals.json')['decisions']
        self.assertEqual(len(first), 50)
        self.assertEqual(one['scheduling']['generation'], 1)
        two = self.success('--live')
        all_proposals = self.read('hidden_files/qresolve-proposals.json')['decisions']
        self.assertEqual(len(all_proposals), 75)
        self.assertEqual(two['scheduling']['generation'], 2)
        self.assertEqual(two['scheduling']['new_or_changed_cards'], 25)
        self.assertEqual(two['metrics']['attached_drafts'], 75)
        self.assertEqual(two['metrics']['tray_revalidated_cards'], 75)
        self.assertEqual(two['cards_seen'], 50)
        tray = {row['key']: row for row in self.read('hidden_files/input-tray.json')['cards']}
        for decision in first:
            self.assertEqual(tray[decision['card_key']]['draft']['decision_id'], decision['decision_id'])
        self.assertEqual((self.home / 'data/queues/needs_input-queue.json').read_bytes(), before)
        self.assertFalse(list((self.home / 'hidden_files').glob('*backup*qresolve-schedule*')))

    def test_preview_is_read_only_before_and_after_schedule_exists(self):
        for initialized in (False, True):
            if initialized:
                self.success('--live')
            before = self.snapshot()
            report = self.success()
            self.assertFalse(report['scheduling']['state_advanced'])
            self.assertEqual(self.snapshot(), before)

    def test_corruption_symlink_fifo_and_directory_hold_before_live_changes(self):
        folder = self.home / 'hidden_files'
        folder.mkdir()
        path = folder / 'qresolve-schedule.json'
        outside = self.home / 'outside.json'
        outside.write_text(json.dumps(schedule.empty_state()))
        before_queues = (self.home / 'data/queues/needs_input-queue.json').read_bytes()
        for kind in ('corrupt', 'null', 'symlink', 'fifo', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'corrupt':
                    path.write_text('{"schema":"bad"}')
                elif kind == 'null':
                    path.write_text('null')
                elif kind == 'symlink':
                    path.symlink_to(outside)
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    path.mkdir()
                result = self.cli('--live')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((folder / 'qresolve-proposals.json').exists())
                self.assertEqual((self.home / 'data/queues/needs_input-queue.json').read_bytes(), before_queues)
                if kind == 'directory':
                    path.rmdir()
                else:
                    path.unlink()
        self.assertEqual(json.loads(outside.read_text()), schedule.empty_state())

    def test_pending_intent_survives_rotation_and_inactive_entry_pruning(self):
        self.success('--live')
        journal = self.home / 'hidden_files/qresolve-resolutions.jsonl'
        raw = json.dumps({'action': 'INTENT', 'decision_id': 'a' * 64}) + '\n'
        journal.write_text(raw)
        self.write('data/queues/needs_input-queue.json', self.rows[-1:])
        report = self.success('--live')
        self.assertTrue(report['pending_intent'])
        self.assertEqual(journal.read_text(), raw)
        self.assertEqual(len(self.read('hidden_files/qresolve-schedule.json')['entries']), 1)
        self.assertEqual(len(self.read('hidden_files/qresolve-proposals.json')['decisions']), 1)
        self.assertEqual(report['canonical_writes'], 0)

    def test_invalid_duplicate_proposals_hold_without_advancing_state(self):
        self.success('--live')
        before = (self.home / 'hidden_files/qresolve-schedule.json').read_bytes()
        saved = self.read('hidden_files/qresolve-proposals.json')
        saved['decisions'].append(saved['decisions'][0])
        self.write('hidden_files/qresolve-proposals.json', saved)
        result = self.cli('--live')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual((self.home / 'hidden_files/qresolve-schedule.json').read_bytes(), before)

    def test_modified_proposal_payload_holds_without_overwriting_it(self):
        self.success('--live')
        saved = self.read('hidden_files/qresolve-proposals.json')
        saved['decisions'][0]['answer'] = 'Changed after review'
        self.write('hidden_files/qresolve-proposals.json', saved)
        before = self.snapshot()
        result = self.cli('--live')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for name in ('hidden_files/qresolve-proposals.json', 'hidden_files/qresolve-schedule.json',
                     'data/queues/needs_input-queue.json'):
            self.assertEqual(self.snapshot()[name], before[name])


if __name__ == '__main__':
    unittest.main()
