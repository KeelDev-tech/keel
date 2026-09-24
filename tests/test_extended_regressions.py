"""Regression checks for paths found in the full-source audit."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engines'))
import html_form
import form_intel
import record_outcome
import log_event
import outcome_analytics
import safe_io
import safe_http


class PassiveFormTests(unittest.TestCase):
    def test_labels_options_and_required_controls_without_values(self):
        html='''<form><label for="name">Full name</label><input id="name" name="name" required value="PRIVATE_VALUE">
        <label>Location<select name="location" required><option>Remote</option><option>Office &amp; hybrid</option></select></label>
        <input type="hidden" name="csrf" value="SECRET_CSRF"><input type="submit" value="Apply">
        <script><input name="fake"></script></form>'''
        result=html_form.inspect_html(html,'https://example.org/')
        self.assertEqual(len(result['questions']),2)
        self.assertEqual(result['questions'][0]['label'],'Full name*')
        self.assertEqual(result['questions'][1]['options'],['Remote','Office & hybrid'])
        self.assertFalse(result['extraction_complete'])
        self.assertNotIn('PRIVATE_VALUE',json.dumps(result));self.assertNotIn('SECRET_CSRF',json.dumps(result))

    def test_dynamic_form_does_not_claim_completeness(self):
        result=html_form.inspect_html('<div id="root"></div><script src="bundle.js"></script>','https://example.org/')
        self.assertFalse(result['extraction_complete']);self.assertEqual(result['questions'],[])

    def test_generic_probe_is_used_outside_known_exact_hosts(self):
        with patch.object(form_intel,'fetch',return_value='<label for="x">Why us?</label><textarea id="x" required></textarea>') as fetched,patch.object(form_intel,'greenhouse_embed_intel',side_effect=AssertionError('wrong host')):
            result=form_intel.probe_url('https://boards.greenhouse.io.evil.example/fixture/jobs/123')
            self.assertEqual(result['source'],'passive_html_inspection');fetched.assert_called_once()

    def test_form_control_limit(self):
        with self.assertRaises(ValueError):html_form.inspect_html('<input>'*501,'https://example.org/')


class OutcomeValidationTests(unittest.TestCase):
    def test_impossible_dates_unknown_outcomes_and_nonfinite_scores_rejected(self):
        cases=[{'date':'2026-02-30'},{'outcome':'maybe'},{'fit_score':float('nan')},
               {'fit_score':float('inf')},{'fit_score':True},{'fit_score':-1},{'fit_score':101}]
        for case in cases:
            args={'ats':'greenhouse','technique':'manual-review','outcome':'blocked','note':'synthetic blocked attempt',**case}
            with self.subTest(case=case),self.assertRaises(ValueError),patch.object(log_event,'log',side_effect=AssertionError('must not log')):
                record_outcome.record(**args)

    def test_telemetry_disabled_does_not_claim_durable_write(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(log_event,'log',side_effect=AssertionError('must not log')):
            result=record_outcome.record('greenhouse','manual-review','blocked','synthetic blocked attempt',emit_telemetry=False)
        self.assertIsNone(result);self.assertIn('not recorded',output.getvalue())

    def test_ambiguous_company_response_stays_unlinked(self):
        rows=[{'role_id':'A','company':'Fixture','status':'SUBMITTED','date_submitted':'2026-09-15T00:00:00Z'},
              {'role_id':'B','company':'Fixture','status':'SUBMITTED','date_submitted':'2026-09-16T00:00:00Z'}]
        event={'company':'Fixture','company_key':'fixture','ts':'2026-09-17T00:00:00Z'}
        linked,unlinked,held=outcome_analytics.link_responses(rows,[event])
        # Ambiguous company match: not attributed, not unlinked — held.
        self.assertFalse(linked);self.assertEqual(unlinked,[])
        self.assertEqual(len(held),1);self.assertEqual(held[0]['linkage']['tier'],4)
        self.assertNotIn('linkage',event)  # original evidence unmutated
        linked,unlinked,held=outcome_analytics.link_responses(rows,[{**event,'role_id':'B'}])
        self.assertEqual(list(linked),[1]);self.assertEqual(unlinked,[]);self.assertEqual(held,[])

    def test_response_before_application_stays_unlinked(self):
        rows=[{'role_id':'A','company':'Fixture','status':'SUBMITTED','date_submitted':'2026-09-16T00:00:00Z'}]
        event={'company':'Fixture','company_key':'fixture','ts':'2026-09-15T00:00:00Z'}
        linked,unlinked,held=outcome_analytics.link_responses(rows,[event])
        self.assertFalse(linked);self.assertEqual(unlinked,[event])


class TransportTests(unittest.TestCase):
    def exchange(self,body,headers=None):
        class FakeSocket:
            def settimeout(self,value):pass
            def connect(self,address):self.address=address
            def close(self):pass
            def shutdown(self,how):pass
        class FakeResponse:
            status=200
            def __init__(self):self.body=io.BytesIO(body);self.headers=headers or {}
            def getheader(self,k,default=None):return self.headers.get(k,default)
            def read1(self,n):return self.body.read(n)
            def close(self):self.body.close()
        class Connection:
            sock=None
            def request(self,*args,**kwargs):pass
            def getresponse(self):return FakeResponse()
            def close(self):pass
        class Context:
            def set_alpn_protocols(self,*args):pass
            def wrap_socket(self,sock,server_hostname):return sock
        sock=FakeSocket()
        with patch.object(safe_http.socket,'socket',return_value=sock),patch.object(safe_http.ssl,'create_default_context',return_value=Context()),patch.object(safe_http.http.client,'HTTPSConnection',return_value=Connection()),patch.object(safe_http.socket,'getaddrinfo',side_effect=AssertionError('DNS must not be repeated')):
            response=safe_http._exchange('https://example.org/a','GET',{},[(2,1,6,'',('8.8.8.8',443))],1,10)
        self.assertEqual(sock.address,('8.8.8.8',443))
        return response

    def test_transport_connects_to_checked_address(self):
        self.assertEqual(self.exchange(b'123').read(),b'123')

    def test_body_limit_without_content_length(self):
        with self.assertRaises(safe_http.NetworkPolicyError):self.exchange(b'x'*11)

    def test_oversize_length_and_compression_refused(self):
        for headers in ({'Content-Length':'11'},{'Content-Encoding':'gzip'}):
            with self.subTest(headers=headers),self.assertRaises(safe_http.NetworkPolicyError):self.exchange(b'x',headers)


class PackagingTests(unittest.TestCase):
    def load_module(self):
        spec=importlib.util.spec_from_file_location('keel_package',ROOT/'tools/package.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def test_reproducible_archive_manifest_and_runtime_exclusion(self):
        module=self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'VERSION').write_text('fixture\n');(root/'source.py').write_text('print("fixture")\n')
            (root/'release-files.json').write_text(json.dumps(['VERSION','source.py','release-files.json']))
            (root/'data').mkdir();(root/'data/private.json').write_text('PRIVATE')
            first=module.build(root/'one.zip',root);second=module.build(root/'two.zip',root)
            self.assertEqual(first['sha256'],second['sha256']);self.assertTrue(module.verify(root/'one.zip')['verified_integrity'])
            (root/'release-files.json').write_text(json.dumps(['VERSION','../outside']))
            with self.assertRaises(ValueError):module.payload(root)

    def test_symlink_not_packaged(self):
        module=self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'VERSION').write_text('fixture');(root/'alias').symlink_to(root/'VERSION')
            (root/'release-files.json').write_text(json.dumps(['VERSION','alias']))
            with self.assertRaises(ValueError):module.payload(root)


class FinalBoundaryTests(unittest.TestCase):
    def test_failed_form_screen_remains_verification_work(self):
        import prescreen,verify_retry
        with patch.object(form_intel,'probe_url',side_effect=RuntimeError('network unavailable')):
            verdict=prescreen.screen_entry_prepromotion({'ats_url':'https://example.org/'})
            self.assertEqual(verdict['verdict'],'UNKNOWN')
            with self.assertRaises(verify_retry.VerificationUnavailable):
                verify_retry.screen_promotion_form({'role_id':'R'},'https://example.org/')

    def test_stale_queue_snapshot_refuses_all_changes(self):
        import queue_io
        with tempfile.TemporaryDirectory() as directory:
            first=str(Path(directory)/'one.json');second=str(Path(directory)/'two.json')
            safe_io.atomic_json(first,{'entries':[{'role_id':'A'}],'revision':2});safe_io.atomic_json(second,[])
            expected={p:safe_io.read_json(p) for p in (first,second)}
            safe_io.atomic_json(first,{'entries':[{'role_id':'A','status':'PARKED'}],'revision':3})
            with patch.object(queue_io,'_LOCK_PATH',str(Path(directory)/'lock')):
                with self.assertRaises(RuntimeError):queue_io.commit_snapshot({first:[],second:[{'role_id':'B'}]},expected)
            self.assertEqual(safe_io.read_json(second),[])
            self.assertEqual(safe_io.read_json(first)['revision'],3)

    def test_http429_prevents_following_host_request(self):
        safe_http._backoff.clear()
        response=safe_http.Response(b'',429,{'Retry-After':'120'},'https://rate.example.org/')
        try:
            with patch.object(safe_http,'resolve_public',return_value=[]),patch.object(safe_http,'_exchange',return_value=response) as call:
                with self.assertRaises(safe_http.HTTPError):safe_http.urlopen('https://rate.example.org/')
                with self.assertRaises(safe_http.NetworkPolicyError):safe_http.urlopen('https://rate.example.org/other')
                self.assertEqual(call.call_count,1)
        finally:safe_http._backoff.clear()

    def test_budget_counts_wrapped_claims_and_ignores_similar_employer_name(self):
        import rate_limits
        with tempfile.TemporaryDirectory() as directory:
            ledger=str(Path(directory)/'ledger.json')
            safe_io.atomic_json(ledger,{'rows':[{'role_id':'R','company':'Fixture','status':'SUBMISSION_CLAIMED'},
                                             {'role_id':'X','company':'Not Fixture','status':'SUBMITTED'}]})
            self.assertEqual(rate_limits.count_used('Fixture',ledger),1)

    def test_text_bundle_validates_before_extracting(self):
        spec=importlib.util.spec_from_file_location('text_bundle',ROOT/'tools/text_bundle.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'bundle.txt';destination=root/'restored';body=b'print("fixture")\n'
            info={'path':'source.py','bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
            source.write_bytes(module.MAGIC+b'{"files":1}\n'+b'FILE '+json.dumps(info).encode()+b'\n'+body+b'\nEND\n')
            self.assertEqual(module.extract(source,destination),1);self.assertEqual((destination/'source.py').read_bytes(),body)
            info['path']='../escape';source.write_bytes(module.MAGIC+b'{"files":1}\n'+b'FILE '+json.dumps(info).encode()+b'\n'+body+b'\nEND\n')
            with self.assertRaises(ValueError):module.extract(source,root/'refused')
            self.assertFalse((root/'refused').exists());self.assertFalse((root/'escape').exists())

if __name__=='__main__':unittest.main()
