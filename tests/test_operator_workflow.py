"""Public CLI, browser-facing output, batch and recovery checks."""
import asyncio
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engines'))
from safe_io import read_json, atomic_json, atomic_bytes, utc_now
import build_dashboard
import safe_http
import verify_retry_async as async_verify


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)/'workspace with spaces'
        self.addCleanup(self.temp.cleanup)

    def cli(self,*args,role='operator'):
        return subprocess.run([sys.executable,str(ROOT/'keel.py'),'--home',str(self.home),'--role',role,*args],
                              capture_output=True,text=True,timeout=10)

    def test_init_confirm_doctor_dashboard_roundtrip(self):
        self.assertEqual(self.cli('init').returncode,0)
        before=read_json(self.home/'data/answer_bank.json')
        self.assertTrue(all(v is None for v in before['answers'].values()))
        self.assertEqual(before['attestation_scope']['preauthorized_attestation_keys'],[])
        self.assertEqual(self.cli('doctor').returncode,1)
        for key,value in [('first_name','Alex'),('last_name','Tester'),('email','alex@fixture.invalid')]:
            result=self.cli('confirm-answer','--key',key,'--value',value,'--source','synthetic applicant assertion')
            self.assertEqual(result.returncode,0,result.stderr)
        doctor=self.cli('doctor');self.assertEqual(doctor.returncode,0,doctor.stdout+doctor.stderr)
        self.assertTrue(json.loads(doctor.stdout)['ready_for_local_preparation'])
        self.assertFalse(json.loads(doctor.stdout)['provider_verification_available'])
        self.assertEqual(self.cli('dashboard').returncode,0)
        document=(self.home/'dashboard/dashboard.html').read_text()
        self.assertIn('Provider verification is not connected',document)
        self.assertNotIn('LIVE — AUTO-REFRESHED',document)

    def test_reinitialize_preserves_answers_and_rejects_corruption(self):
        self.cli('init')
        self.cli('confirm-answer','--key','first_name','--value','Alex','--source','test')
        before=(self.home/'data/answer_bank.json').read_bytes()
        self.assertEqual(self.cli('init').returncode,0)
        self.assertEqual((self.home/'data/answer_bank.json').read_bytes(),before)
        (self.home/'data/queues/standard-queue.json').write_text('{corrupt')
        self.assertEqual(self.cli('init').returncode,2)
        self.assertEqual((self.home/'data/queues/standard-queue.json').read_text(),'{corrupt')

    def test_cli_role_cannot_confirm_on_behalf_of_applicant(self):
        result=self.cli('confirm-answer','--key','us_work_auth','--value','Yes','--source','guessed',role='discovery')
        self.assertEqual(result.returncode,2)
        self.assertFalse((self.home/'data/answer_bank.json').exists())

    def test_duplicate_identity_reported(self):
        self.cli('init')
        atomic_json(self.home/'data/queues/standard-queue.json',[{'role_id':'R'},{'role_id':'R'}])
        report=json.loads(self.cli('doctor').stdout)
        self.assertTrue(any(c['check']=='Queue identity' and c['status']=='ACTION_REQUIRED' for c in report['checks']))

    def test_missing_and_corrupt_data_are_unknown_not_zero(self):
        data=build_dashboard.collect(self.home)
        self.assertIsNone(data['queue_total']);self.assertTrue(data['warnings'])
        self.cli('init');(self.home/'data/queues/strategic-queue.json').write_text('{bad')
        data=build_dashboard.collect(self.home)
        self.assertIsNone(data['queues']['strategic']);self.assertIsNone(data['queue_total'])
        self.assertIn('Data needs attention',build_dashboard.render(data))

    def test_dashboard_escapes_all_untrusted_labels_and_dates(self):
        self.cli('init');attack='<img src=x onerror="window.injected=true">'
        atomic_json(self.home/'data/application-ledger.json',{'rows':[{'role_id':'R','company':attack,'title':attack,'status':'SUBMITTED','date_submitted':attack}]})
        html=build_dashboard.render(build_dashboard.collect(self.home))
        self.assertNotIn('<img',html);self.assertNotIn('<script',html)
        self.assertIn('&lt;img',html)
        self.assertIn("default-src 'none'",html)
        self.assertIn('Reported submission claims',html)

    def test_import_does_not_write_dashboard(self):
        path=ROOT/'engines/build_dashboard.py'
        spec=importlib.util.spec_from_file_location('dashboard_import_check',path)
        module=importlib.util.module_from_spec(spec)
        with patch('safe_io.atomic_bytes',side_effect=AssertionError('import wrote a file')):
            spec.loader.exec_module(module)


class AsyncTests(unittest.TestCase):
    def test_async_reads_use_the_checked_transport(self):
        response=safe_http.Response(b'{}',200,{},'https://example.org/final')
        with patch.object(async_verify,'safe_urlopen',return_value=response) as opened:
            async_verify._global_sem=None;async_verify._semaphores.clear()
            status,body,url=asyncio.run(async_verify._get(None,'https://example.org/start',1))
            self.assertEqual((status,body,url),(200,b'{}','https://example.org/final'))
            opened.assert_called_once()

    def test_batch_cannot_silently_overwrite_duplicate_roles(self):
        entries=[('standard',{'role_id':'R'}),('strategic',{'role_id':'R'})]
        with self.assertRaises(ValueError):asyncio.run(async_verify.scan_window_async(entries))

    def test_async_batch_is_bounded(self):
        with self.assertRaises(ValueError):asyncio.run(async_verify.scan_window_async([('q',{'role_id':str(i)}) for i in range(1001)]))


class ProcessRecoveryTests(unittest.TestCase):
    def test_process_death_releases_storage_lock(self):
        import multiprocessing
        from safe_io import file_lock
        with tempfile.TemporaryDirectory() as directory:
            context=multiprocessing.get_context('fork');ready=context.Event();finish=context.Event()
            path=str(Path(directory)/'lock')
            def holder():
                with file_lock(path):
                    ready.set();finish.wait(5)
            process=context.Process(target=holder);process.start()
            try:
                self.assertTrue(ready.wait(3))
                process.terminate();process.join(3)
                self.assertFalse(process.is_alive())
                with file_lock(path,timeout=.2):pass
            finally:
                if process.is_alive():process.terminate();process.join(1)

    def test_twenty_processes_have_one_lease_winner(self):
        import multiprocessing
        import launch_lock
        with tempfile.TemporaryDirectory() as directory,patch.object(launch_lock,'LOCK_DIR',directory):
            context=multiprocessing.get_context('fork');start=context.Event();results=context.Queue()
            def contender(i):
                start.wait(3)
                try:results.put(launch_lock.acquire('R','t-'+str(i))[0])
                except Exception as exc:results.put(str(exc))
            processes=[context.Process(target=contender,args=(i,)) for i in range(20)]
            try:
                for process in processes:process.start()
                start.set();outcomes=[results.get(timeout=5) for _ in processes]
                self.assertEqual(outcomes.count(True),1);self.assertEqual(outcomes.count(False),19)
            finally:
                for process in processes:
                    process.join(2)
                    if process.is_alive():process.terminate();process.join(1)
                results.close()


if __name__=='__main__':unittest.main()
