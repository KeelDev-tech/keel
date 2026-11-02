"""Buffer refill must release vetoed packets and continue after parking I/O failure."""
import contextlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import apply_loop as loop
import ready_gate
import queue_io


class BufferParkFailureTests(unittest.TestCase):
    def run_case(self, outcome='raise', tail_hold=False, scan_limit='2'):
        with tempfile.TemporaryDirectory(prefix='keel-buffer-park-') as folder:
            home = Path(folder); data = home / 'data'; queues = data / 'queues'
            queues.mkdir(parents=True)
            buffer = home / 'buffer'; buffer.mkdir()
            resume = home / 'resume.txt'; resume.write_text('Synthetic material')
            bank = {'answers': {}, 'gates': {}, 'banded_questions': {}}
            entries = [{'role_id': f'fixture-{i}', 'company': 'Synthetic', 'title': f'Role {i}',
                        'status': 'READY', 'fit_score': 90-i, 'action_band': 'APPLY',
                        'ats_url': f'https://jobs.lever.co/synthetic/job-{i}',
                        'materials': {'resume': str(resume)}} for i in range(2)]
            if tail_hold: entries[1]['human_hold'] = True
            for name in ('standard', 'strategic', 'needs_input', 'rejected'):
                (queues / (name+'-queue.json')).write_text(json.dumps(entries if name == 'standard' else []))
            (data / 'application-ledger.json').write_text('[]')
            (data / 'employer-blocklist.md').write_text('# Blocked employers\n')
            before = {p:p.read_bytes() for p in queues.iterdir()}
            builds, releases, saved, closed, visits, events = [], [], [], [], [], []
            def build(entry, **kwargs):
                builds.append(entry['role_id'])
                packet = {**{key:entry[key] for key in ('role_id','company','title','ats_url')},
                          'brief':'Synthetic preparation', 'scope':'preparation_only',
                          'execution_authorized':False, 'upload_files':[str(resume)]}
                ready_gate.seal_packet(packet, entry, bank, workspace=home)
                path = buffer / (entry['role_id']+'.json'); path.write_text(json.dumps(packet))
                return str(path)
            def eligible(entry, origin):
                visits.append(entry['role_id'])
                result=ready_gate.entry_admission(entry, origin, workspace=home)
                return result['allowed'], '; '.join(result['reasons'])
            def park(*args):
                if outcome == 'raise': raise OSError('synthetic queue I/O failure')
                return {'ok': outcome == 'ok', 'error':'synthetic conflict'}
            with contextlib.ExitStack() as stack:
                for module in (loop, ready_gate): stack.enter_context(patch.object(module,'HOME',str(home)))
                settings = {'DATA':str(data),'QUEUE':str(queues/'standard-queue.json'),
                    'SQUEUE':str(queues/'strategic-queue.json'),'LEDGER':str(data/'application-ledger.json'),
                    'BLOCKLIST':str(data/'employer-blocklist.md'),'BUFFER_DIR':str(buffer),
                    'BUFFER_WATERMARK':str(home/'watermark.json'),'BUFFER_MIN_FRESH':1,
                    'load_buffer_state':lambda:[], 'save_buffer_state':lambda s:saved.extend(s),
                    'load_answer_bank':lambda:bank,'eligible':eligible,'build_packet':build,
                    '_buffer_lock':lambda:SimpleNamespace(close=lambda:closed.append(True)),
                    '_launch_guard':lambda rid,*a:(True,'task-'+rid,''),
                    '_release_launch_lock':lambda *a:releases.append(a),
                    '_checked_prescreen':lambda p,b:{'verdict':'PARK' if p['role_id']=='fixture-0' else 'CLEAN',
                                                    'reasons':['Synthetic question'] if p['role_id']=='fixture-0' else []}}
                for name,value in settings.items(): stack.enter_context(patch.object(loop,name,value))
                stack.enter_context(patch.object(queue_io,'_LOCK_PATH',str(home/'queue.lock')))
                stack.enter_context(patch.object(loop.prescreen,'park_lead',side_effect=park))
                stack.enter_context(patch.object(loop.log_event,'log',side_effect=lambda event,**kw:events.append(event)))
                stack.enter_context(patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')))
                stack.enter_context(patch.dict('os.environ',{'KEEL_BUFFER_SCAN_LIMIT':scan_limit}))
                error = None; count = None
                with contextlib.redirect_stdout(io.StringIO()):
                    try: count=loop.refresh_buffer([(e,'standard') for e in entries])
                    except OSError as exc: error=type(exc).__name__
            self.assertEqual(before,{p:p.read_bytes() for p in queues.iterdir()})
            self.assertEqual(closed,[True])
            self.assertFalse({'submitted','submission_claimed'} & set(events))
            return {'error':error,'count':count,'builds':builds,'releases':releases,
                    'saved':[s['role_id'] for s in saved],'head_exists':(buffer/'fixture-0.json').exists(),
                    'visits':visits,'watermark_exists':(home/'watermark.json').exists()}

    def test_parking_exception_releases_head_and_refills_from_tail(self):
        result=self.run_case()
        self.assertIsNone(result['error'])
        self.assertEqual(result['count'],1)
        self.assertEqual(result['builds'],['fixture-0','fixture-1'])
        self.assertEqual(result['releases'],[('fixture-0','task-fixture-0')])
        self.assertEqual(result['saved'],['fixture-1'])
        self.assertFalse(result['head_exists'])
        self.assertTrue(result['watermark_exists'])

    def test_refused_and_successful_parks_keep_existing_refill_progress(self):
        for outcome in ('false','ok'):
            with self.subTest(outcome=outcome):
                result=self.run_case(outcome)
                self.assertEqual(result['count'],1)
                self.assertEqual(result['saved'],['fixture-1'])
                self.assertEqual(result['releases'],[('fixture-0','task-fixture-0')])
                self.assertFalse(result['head_exists'])

    def test_failed_park_cannot_waive_tail_human_hold(self):
        result=self.run_case(tail_hold=True)
        self.assertIsNone(result['error'])
        self.assertEqual(result['count'],0)
        self.assertEqual(result['builds'],['fixture-0'])
        self.assertEqual(result['saved'],[])
        self.assertEqual(result['releases'],[('fixture-0','task-fixture-0')])
        self.assertFalse(result['head_exists'])

    def test_failed_park_preserves_scan_bound_and_saves_progress(self):
        result=self.run_case(scan_limit='1')
        self.assertIsNone(result['error'])
        self.assertEqual(result['count'],0)
        self.assertEqual(result['visits'],['fixture-0'])
        self.assertTrue(result['watermark_exists'])
        self.assertFalse(result['head_exists'])

if __name__ == '__main__': unittest.main()
