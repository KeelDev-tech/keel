"""Malformed JSON role identities cannot starve valid, still-gated neighbors."""
import contextlib, copy, io, json, socket, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engines'))
import apply_loop as loop
import ready_gate,queue_io

BAD_IDS=([],['tail'],{}, {'role':'tail'},None,True,7,'','   ')

class InvalidRoleProgressTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory(prefix='keel-invalid-role-');self.addCleanup(tmp.cleanup)
  self.home=Path(tmp.name); self.data=self.home/'data';self.q=self.data/'queues';self.q.mkdir(parents=True)
  self.resume=self.home/'resume.txt';self.resume.write_text('Synthetic material')
  self.buffer=self.home/'buffer';self.buffer.mkdir()
  self.bank={'answers':{},'gates':{},'banded_questions':{}}
  for n in ('standard','strategic','needs_input','rejected'):(self.q/(n+'-queue.json')).write_text('[]')
  (self.data/'application-ledger.json').write_text('[]');(self.data/'employer-blocklist.md').write_text('# Blocked employers\n')
  self.visits=[];self.saved=[];self.closed=[]
  def deny(e,o):self.visits.append((e['role_id'],o));return False,'synthetic hold'
  values={'HOME':str(self.home),'DATA':str(self.data),'QUEUE':str(self.q/'standard-queue.json'),
   'SQUEUE':str(self.q/'strategic-queue.json'),'LEDGER':str(self.data/'application-ledger.json'),
   'BLOCKLIST':str(self.data/'employer-blocklist.md'),'BUFFER_WATERMARK':str(self.home/'watermark.json'),
   'BUFFER_DIR':str(self.buffer),'load_answer_bank':lambda:self.bank,'load_buffer_state':lambda:[],
   'save_buffer_state':lambda s:self.saved.append(copy.deepcopy(s)),'eligible':deny,
   '_buffer_lock':lambda:SimpleNamespace(close=lambda:self.closed.append(True))}
  for name,value in values.items():p=patch.object(loop,name,value);p.start();self.addCleanup(p.stop)
  for p in [patch.object(ready_gate,'HOME',str(self.home)),patch.object(queue_io,'_LOCK_PATH',str(self.home/'queue.lock')),
    patch.object(loop,'_launch_guard',side_effect=AssertionError('denied row must not acquire lease')),
    patch.object(loop,'build_packet',side_effect=AssertionError('denied row must not build')),
    patch.object(loop.log_event,'log',return_value=None),
    patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'))]:
   p.start();self.addCleanup(p.stop)
 def row(self,rid):
  return {'role_id':rid,'company':'Synthetic','title':'Role','status':'READY','fit_score':80,'action_band':'APPLY',
    'ats_url':'https://jobs.lever.co/synthetic/'+(rid if isinstance(rid,str) and rid.strip() else 'bad'),
    'materials':{'resume':str(self.resume)}}
 def write(self,standard,strategic=()):
  (self.q/'standard-queue.json').write_text(json.dumps(standard));(self.q/'strategic-queue.json').write_text(json.dumps(list(strategic)))
  return {p:p.read_bytes() for p in self.q.iterdir()}
 def assert_unchanged(self,before):self.assertEqual(before,{p:p.read_bytes() for p in self.q.iterdir()})
 def test_main_reaches_denied_valid_neighbors_without_identity_coercion(self):
  for bad in BAD_IDS:
   with self.subTest(bad=bad):
    self.visits.clear();before=self.write([self.row(bad),self.row('tail')],[self.row('strategic')])
    self.assertFalse(ready_gate.entry_admission(self.row(bad))['allowed'])
    with patch.object(sys,'argv',['apply_loop.py']),contextlib.redirect_stdout(io.StringIO()):loop.main()
    self.assertEqual(set(self.visits),{('tail','standard'),('strategic','strategic')});self.assert_unchanged(before)
 def test_refresh_preserves_bounded_pending_tail_progress(self):
  for bad in BAD_IDS:
   with self.subTest(bad=bad):
    self.visits.clear();Path(loop.BUFFER_WATERMARK).unlink(missing_ok=True)
    before=self.write([self.row(bad),self.row('first'),self.row('tail')])
    with patch.dict('os.environ',{'KEEL_BUFFER_SCAN_LIMIT':'1'}),contextlib.redirect_stdout(io.StringIO()):
     self.assertEqual(loop.refresh_buffer(),0);self.assertEqual(self.visits,[('first','standard')])
     self.assertEqual(len(loop._load_buffer_watermark()['scan_pending']),1)
     self.assertEqual(loop.refresh_buffer(),0)
    self.assertEqual(self.visits,[('first','standard'),('tail','standard')]);self.assert_unchanged(before)
 def test_fresh_count_retains_valid_packet_but_not_held_or_malformed_role(self):
  tail=self.row('tail');packet={**{k:tail[k] for k in ('role_id','company','title','ats_url')},'brief':'Synthetic preparation',
   'scope':'preparation_only','execution_authorized':False,'upload_files':[str(self.resume)]}
  ready_gate.seal_packet(packet,tail,self.bank,workspace=self.home)
  self.assertFalse(ready_gate.packet_admission(packet,tail,self.bank,workspace=self.home)['allowed'])
  path=self.buffer/'tail.json';path.write_text(json.dumps(packet))
  state=[{'role_id':'tail','packet_path':str(path),'built_at':datetime.now(timezone.utc).isoformat()}]
  for bad in BAD_IDS:
   with self.subTest(bad=bad):
    before=self.write([self.row(bad),tail]);self.assertEqual(loop._buffer_fresh_count(state),1);self.assert_unchanged(before)
    self.write([self.row(bad),{**tail,'human_hold':True}]);self.assertEqual(loop._buffer_fresh_count(state),0)
  self.write([tail],[tail]);self.assertEqual(loop._buffer_fresh_count(state),0)
 def test_valid_duplicate_ids_stay_refused_in_both_origins(self):
  before=self.write([self.row([]),self.row('duplicate'),self.row('tail')],[self.row('duplicate')])
  with patch.object(sys,'argv',['apply_loop.py']),contextlib.redirect_stdout(io.StringIO()):loop.main()
  self.assertEqual(self.visits,[('tail','standard')]);self.visits.clear()
  with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(loop.refresh_buffer(),0)
  self.assertEqual(self.visits,[('tail','standard')]);self.assert_unchanged(before)

if __name__=='__main__':unittest.main(verbosity=2)
