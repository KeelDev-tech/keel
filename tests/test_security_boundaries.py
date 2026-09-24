"""Adversarial regression cases, entirely synthetic and network-free."""
import contextlib
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import tempfile

ENGINES=Path(__file__).resolve().parents[1]/'engines'
sys.path.insert(0,str(ENGINES))
import safe_http as http
import safe_io as storage
import launch_lock as locks
import log_event
import packet_contract as contract
import live_cache
import page_capture
from outcome_tracking import evidence_gate as evidence


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)


class NetworkBoundaryTests(unittest.TestCase):
    def test_non_https_and_credential_urls_rejected(self):
        for url in ('http://example.org/a','file:///etc/passwd','ftp://example.org/a',
                    'https://user:pass@example.org/','https://localhost/',
                    'https://example.org:8443/','https://example.org/\nX',
                    'https://example.org/?access_token=private','https://example.org\\@evil.test/'):
            with self.subTest(url=url),self.assertRaises((ValueError,http.NetworkPolicyError)):
                http.validate_url(url)

    def test_public_job_identifier_exception_is_exact(self):
        self.assertIn('token=123',http.validate_url('https://job-boards.greenhouse.io/embed/job_app?for=demo&token=123'))
        for url in ('https://job-boards.greenhouse.io.evil.org/embed/job_app?for=demo&token=123',
                    'https://job-boards.greenhouse.io/other?token=123',
                    'https://job-boards.greenhouse.io/embed/job_app?token=private-secret'):
            with self.subTest(url=url),self.assertRaises(http.NetworkPolicyError):
                http.validate_url(url)

    def test_every_private_address_class_is_refused(self):
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','172.16.0.1','192.168.0.1',
                        '0.0.0.0','224.0.0.1','100.64.0.1','::1','fc00::1','fe80::1',
                        '::ffff:8.8.8.8','64:ff9b::808:808','2002:0808:0808::1'):
            with self.subTest(address=address):
                self.assertFalse(http._public(address))

    def test_mixed_public_private_dns_refused(self):
        records=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443)),
                 (socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',443))]
        with patch.object(http.socket,'getaddrinfo',return_value=records),self.assertRaises(http.NetworkPolicyError):
            http.resolve_public('example.org')

    def test_redirect_rechecks_destination_before_second_request(self):
        def resolve(host, **kwargs):
            if host=='internal.example.org':
                raise http.NetworkPolicyError('private DNS')
            return [('public',)]
        response=http.Response(b'',302,{'Location':'https://internal.example.org/secrets'},'https://example.org/')
        with patch.object(http,'resolve_public',side_effect=resolve),patch.object(http,'_exchange',return_value=response) as exchange:
            with self.assertRaises(http.NetworkPolicyError):
                http.urlopen('https://example.org/')
            self.assertEqual(exchange.call_count,1)

    def test_redirect_loop_is_bounded(self):
        def exchange(url,*args):
            return http.Response(b'',302,{'Location':'/again'},url)
        with patch.object(http,'resolve_public',return_value=[]),patch.object(http,'_exchange',side_effect=exchange) as call:
            with self.assertRaises(http.NetworkPolicyError):
                http.urlopen('https://example.org/')
            self.assertEqual(call.call_count,2)

    def test_no_post_cookies_or_authorization(self):
        from urllib.request import Request
        requests=[Request('https://example.org/',data=b'private'),
                  Request('https://example.org/',method='DELETE'),
                  Request('https://example.org/',headers={'Cookie':'session=secret'}),
                  Request('https://example.org/',headers={'Authorization':'Bearer private'})]
        for req in requests:
            with self.subTest(req=req),self.assertRaises(http.NetworkPolicyError):
                http.urlopen(req)

    def test_response_and_timeout_limits_validated(self):
        for timeout in (-1,0,61,float('nan'),float('inf')):
            with self.subTest(timeout=timeout),self.assertRaises(http.NetworkPolicyError):
                http.urlopen('https://example.org/',timeout=timeout)
        for limit in (0,-1,http.MAX_BYTES+1,True):
            with self.subTest(limit=limit),self.assertRaises(http.NetworkPolicyError):
                http.urlopen('https://example.org/',max_bytes=limit)

    def test_no_raw_urllib_fetches_in_bundled_engines(self):
        import ast
        for file in ENGINES.rglob('*.py'):
            if file.name=='safe_http.py':
                continue
            tree=ast.parse(file.read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Call):
                    target=ast.unparse(node.func)
                    self.assertNotIn(target,{'urllib.request.urlopen','requests.get','requests.post','requests.request'},str(file))


class StorageTests(Isolated):
    def test_json_duplicate_keys_and_nonfinite_rejected(self):
        for value in ('{"a":1,"a":2}','{"x":NaN}','{"x":Infinity}','{"x":1e999}'):
            with self.assertRaises(ValueError): storage.loads(value)

    def test_all_supported_wrappers_preserve_metadata(self):
        for key in storage.ROW_KEYS:
            data={key:[{'role_id':'R'}],'revision':9,'owner':'fixture'}
            updated=storage.with_rows(data,[{'role_id':'R','status':'PARKED'}])
            self.assertEqual(updated['revision'],9)
            self.assertEqual(updated['owner'],'fixture')
            self.assertEqual(storage.rows(updated)[0]['status'],'PARKED')
        with self.assertRaises(ValueError): storage.rows({'rows':[],'entries':[]})
        with self.assertRaises(ValueError): storage.rows([1])

    def test_atomic_replace_failure_keeps_prior_bytes(self):
        path=self.root/'state.json'
        storage.atomic_json(path,{'revision':1})
        with patch.object(storage.os,'replace',side_effect=OSError('injected failure')):
            with self.assertRaises(OSError): storage.atomic_json(path,{'revision':2})
        self.assertEqual(storage.read_json(path),{'revision':1})
        self.assertFalse(list(self.root.glob('.keel-*')))
        self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_path_escape_and_external_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside=Path(other)/'private.txt';outside.write_text('private')
            (self.root/'link').symlink_to(outside)
            for path in (outside,self.root/'link'):
                with self.assertRaises(ValueError): storage.contained_path(self.root,path)

    def test_future_and_naive_timestamps_not_fresh(self):
        now=storage.utc_now()
        self.assertFalse(storage.fresh((now+timedelta(seconds=1)).isoformat(),3600,now=now))
        self.assertFalse(storage.fresh(now.replace(tzinfo=None).isoformat(),3600,now=now))
        self.assertFalse(storage.fresh('nonsense',3600,now=now))
        self.assertTrue(storage.fresh(now.isoformat(),3600,now=now))

    def test_contending_threads_do_not_lose_updates(self):
        path=self.root/'counter.json';storage.atomic_json(path,{'n':0})
        failures=[]
        def worker():
            try:
                for _ in range(10):
                    with storage.file_lock(str(path)+'.lock'):
                        data=storage.read_json(path);data['n']+=1;storage.atomic_json(path,data)
            except Exception as exc: failures.append(exc)
        threads=[threading.Thread(target=worker) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join(10)
        self.assertFalse(failures);self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(storage.read_json(path)['n'],80)

    def test_lock_timeout_and_reentrancy(self):
        path=str(self.root/'guard');result=[]
        def waiter():
            try:
                with storage.file_lock(path,timeout=.03):result.append('entered')
            except TimeoutError:result.append('timeout')
        with storage.file_lock(path),storage.file_lock(path):
            thread=threading.Thread(target=waiter);thread.start();thread.join(1)
        self.assertEqual(result,['timeout'])
        with storage.file_lock(path,timeout=.03):pass


class LockRegressionTests(Isolated):
    def setUp(self):
        super().setUp()
        self.patch=patch.object(locks,'LOCK_DIR',str(self.root/'locks'));self.patch.start();self.addCleanup(self.patch.stop)

    def test_previous_sanitization_collision_is_removed(self):
        self.assertNotEqual(locks._lock_path('a/b'),locks._lock_path('a?b'))

    def test_corrupt_lock_is_not_stolen(self):
        path=Path(locks._lock_path('R'));path.parent.mkdir();path.write_text('{bad')
        with self.assertRaises(ValueError):locks.acquire('R','new')
        self.assertEqual(path.read_text(),'{bad')

    def test_expired_foreign_owner_cannot_release_new_lease(self):
        locks.acquire('R','old')
        path=locks._lock_path('R');record=storage.read_json(path)
        record['acquired_at']=(storage.utc_now()-timedelta(hours=3)).isoformat();storage.atomic_json(path,record)
        self.assertTrue(locks.acquire('R','new')[0])
        self.assertFalse(locks.release('R','old')[0])
        self.assertEqual(locks.check('R')['task_id'],'new')

    def test_future_lease_cannot_be_stolen(self):
        locks.acquire('R','old');path=locks._lock_path('R');record=storage.read_json(path)
        record['acquired_at']=(storage.utc_now()+timedelta(days=1)).isoformat();storage.atomic_json(path,record)
        self.assertFalse(locks.acquire('R','new')[0])

    def test_corrupt_ledger_stops_guard(self):
        path=self.root/'ledger.json';path.write_text('{bad')
        with self.assertRaises(ValueError):locks.prelaunch_guard('R','T',ledger_path=str(path))


class TelemetryTests(Isolated):
    def setUp(self):
        super().setUp();self.events=str(self.root/'events.jsonl')
        self.patch=patch.object(log_event,'EVENTS',self.events);self.patch.start();self.addCleanup(self.patch.stop)

    def test_nested_secrets_redacted(self):
        event=log_event.log('error',details={'nested':[{'accessToken':'never-log','password':'also-private'}],
                                           'note':'Bearer secret123 token=abc&ok=yes'})
        raw=Path(self.events).read_text()
        self.assertNotIn('never-log',raw);self.assertNotIn('also-private',raw)
        self.assertNotIn('secret123',raw);self.assertNotIn('token=abc',raw)
        self.assertIn('[REDACTED]',raw)

    def test_same_event_id_is_idempotent_and_conflicts_refused(self):
        first=log_event.log('error',event_id='attempt:1',details={'note':'broken'})
        self.assertEqual(log_event.log('error',event_id='attempt:1',details={'note':'broken'}),first)
        with self.assertRaises(ValueError):log_event.log('error',event_id='attempt:1',details={'note':'changed'})
        self.assertEqual(len(Path(self.events).read_text().splitlines()),1)

    def test_submission_is_always_an_unverified_claim(self):
        event=log_event.log('submitted',role_id='R',details={'verified':True,'confirmation':'"Received ABC123"'})
        self.assertEqual(event['event_type'],'submission_claimed')
        self.assertEqual(event['details']['verification_status'],'UNVERIFIED')

    def test_nonfinite_values_never_written(self):
        with self.assertRaises(ValueError):log_event.log('error',details={'score':float('nan')})
        self.assertFalse(Path(self.events).exists())

    def test_concurrent_event_records_remain_parseable(self):
        errors=[]
        def emit(i):
            try:log_event.log('error',event_id='t:'+str(i),details={'i':i})
            except Exception as exc:errors.append(exc)
        workers=[threading.Thread(target=emit,args=(i,)) for i in range(20)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(10)
        self.assertFalse(errors)
        self.assertEqual(len(list(evidence.events(self.events))),20)


class EvidenceAndCacheTests(Isolated):
    def test_forged_quotes_flags_and_role_duplicates_never_verified(self):
        now=storage.utc_now();eventpath=self.root/'events.jsonl'
        row={'role_id':'R','status':'SUBMITTED','date_submitted':now.isoformat()}
        storage.append_jsonl(eventpath,{'event_type':'submitted','role_id':'R','ts':now.isoformat(),
                                       'details':{'verified':True,'provider_receipt':'fake','confirmation':'"Received application"'}})
        report=evidence.coverage_report([row,row],str(eventpath),now=now)
        self.assertEqual(report['submission_claims'],1);self.assertEqual(report['verified'],0)
        self.assertEqual(report['pending'],1)
        self.assertEqual(evidence.coverage([row],str(eventpath),now=now+timedelta(hours=25)),(0,0,1))

    def test_invalid_claim_time_has_no_correlation(self):
        path=self.root/'events.jsonl';storage.append_jsonl(path,{'event_type':'submitted','role_id':'R','ts':storage.utc_now().isoformat()})
        report=evidence.coverage_report([{'role_id':'R','status':'SUBMITTED','date_submitted':'invalid'}],str(path))
        self.assertEqual(report['correlated_unverified'],0);self.assertEqual(report['unevidenced'],1)

    def test_missing_capture_hash_or_path_escape_fails(self):
        with patch.object(page_capture,'CAPTURE_DIR',str(self.root/'captures')),patch.object(page_capture,'MANIFEST',str(self.root/'captures/manifest.jsonl')):
            capture=page_capture.capture('https://example.org/jobs/1',html='<html>Untrusted listing</html>')
            self.assertTrue(capture['captured']);self.assertFalse(capture['usable_for_liveness'])
            self.assertIsNotNone(page_capture.load_capture(capture))
            changed={**capture,'sha256':'0'*64}
            self.assertIsNone(page_capture.load_capture(changed))
            Path(capture['capture_path']).write_text('different bytes')
            self.assertIsNone(page_capture.load_capture(capture))
            outside=self.root/'secret';outside.write_text('private')
            self.assertIsNone(page_capture.load_capture({**capture,'capture_path':str(outside)}))

    def test_live_cache_preserves_url_path_case_and_rejects_future(self):
        cache=str(self.root/'cache.json')
        with patch.object(live_cache,'CACHE',cache):
            live_cache.record('R','https://example.org/jobs/Case?key=A','live')
            self.assertTrue(live_cache.fresh_live('R','https://example.org/jobs/Case?key=A'))
            self.assertFalse(live_cache.fresh_live('R','https://example.org/jobs/case?key=a'))
            data=storage.read_json(cache);data['R']['ts']=(storage.utc_now()+timedelta(hours=1)).isoformat();storage.atomic_json(cache,data)
            self.assertFalse(live_cache.fresh_live('R','https://example.org/jobs/Case?key=A'))


class PacketTests(Isolated):
    def setUp(self):
        super().setUp();self.now=storage.utc_now()
        self.bank={'answers':{'first_name':'Alex','last_name':'Tester','email':'alex@fixture.invalid'},'_provenance':{}}
        for key,value in self.bank['answers'].items():self.bank['_provenance'][key]=contract.answer_receipt(value,'synthetic test',now=self.now)
        (self.root/'resume.txt').write_text('Synthetic local resume fixture. No external submission.')
        self.entry={'role_id':'R-1','company':'Fixture','title':'Engineer','status':'READY','action_band':'APPLY','ats_url':'https://example.org/jobs/Case'}
        self.policy={'scope':'prepare'};self.material={'resume':'resume.txt'}
        self.intel={'ats':'unknown','questions':[]}

    def packet(self):
        return contract.prepare(self.entry,self.bank,self.policy,self.root,self.material,self.intel,now=self.now)

    def validate(self,packet,**kwargs):
        return contract.validate(packet,self.entry,self.bank,self.policy,self.root,self.material,now=kwargs.get('now',self.now))

    def test_packet_is_scoped_and_valid(self):
        packet=self.packet();self.assertTrue(self.validate(packet));self.assertFalse(packet['execution_authorized'])
        self.assertEqual(packet['status'],'PREPARED_REVIEW_REQUIRED')

    def test_answer_changes_require_new_assertion(self):
        self.bank['answers']['us_work_auth']='Yes'
        with self.assertRaises(ValueError):self.packet()
        del self.bank['answers']['us_work_auth'];self.bank['answers']['email']='changed@fixture.invalid'
        with self.assertRaises(ValueError):self.packet()

    def test_out_of_scope_and_expired_assertions_fail(self):
        receipt=self.bank['_provenance']['email'];receipt['scope']='different-role'
        with self.assertRaises(ValueError):self.packet()
        receipt['scope']='general';receipt['expires_at']=self.now.isoformat()
        with self.assertRaises(ValueError):self.packet()

    def test_template_values_never_pass_with_a_receipt(self):
        self.bank['answers']['email']='you@example.com';self.bank['_provenance']['email']=contract.answer_receipt('you@example.com','test',now=self.now)
        with self.assertRaises(ValueError):self.packet()

    def test_all_input_drift_invalidates_packet(self):
        for target,key,value in ((self.entry,'title','changed'),(self.policy,'scope','changed'),(self.bank,'new_field',True)):
            packet=self.packet();old=target.get(key);target[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.validate(packet)
            if old is None:target.pop(key)
            else:target[key]=old

    def test_source_and_attachment_changes_invalidate(self):
        packet=self.packet();(self.root/'resume.txt').write_text('changed')
        with self.assertRaises(ValueError):self.validate(packet)
        packet=self.packet();Path(packet['upload_files'][0]).write_text('tampered')
        with self.assertRaises(ValueError):self.validate(packet)

    def test_packet_expiry_and_escalated_authority_fail(self):
        packet=self.packet()
        with self.assertRaises(ValueError):self.validate(packet,now=self.now+timedelta(hours=3))
        packet['execution_authorized']=True
        packet['integrity_sha256']=storage.digest({k:v for k,v in packet.items() if k!='integrity_sha256'})
        with self.assertRaises(ValueError):self.validate(packet)

    def test_boolean_schema_version_rejected(self):
        packet=self.packet();packet['schema_version']=True
        packet['integrity_sha256']=storage.digest({k:v for k,v in packet.items() if k!='integrity_sha256'})
        with self.assertRaises(ValueError):self.validate(packet)

    def test_path_traversal_in_role_cannot_choose_filename(self):
        self.entry['role_id']='../../secret'
        packet=self.packet()
        self.assertEqual(len(packet['packet_id']),64)
        self.assertFalse((self.root/'secret').exists())

    def test_external_material_cannot_be_read(self):
        self.material['resume']='/etc/passwd'
        with self.assertRaises(ValueError):self.packet()

    def test_prescreen_exception_publishes_no_packet(self):
        import apply_loop
        dest=self.root/'packets'
        with patch.multiple(apply_loop,HOME=str(self.root)),patch.object(apply_loop,'load_answer_bank',return_value=self.bank),patch.object(apply_loop,'load_policy',return_value=self.policy),patch.object(apply_loop,'live',return_value=None),patch.object(apply_loop.form_intel,'probe_url',return_value=self.intel),patch.object(apply_loop.prescreen,'screen_packet',side_effect=RuntimeError('injected')):
            self.entry['materials']=self.material
            with self.assertRaises(RuntimeError):apply_loop.build_packet(self.entry,dest_dir=str(dest))
        self.assertFalse(list(dest.glob('*.json')))

    def test_guard_exception_cannot_proceed(self):
        import apply_loop
        with patch.object(apply_loop.launch_lock,'prelaunch_guard',side_effect=RuntimeError('injected')):
            go,_,_=apply_loop._launch_guard('R','Fixture','Engineer')
            self.assertFalse(go)


if __name__=='__main__':unittest.main()
