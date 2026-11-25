"""Standalone unittest coverage for the isolated enterprise lane; synthetic fixtures only."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'experiments' / 'enterprise_insurance' / 'pipeline.py'
spec = importlib.util.spec_from_file_location('enterprise_lane_test_target', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)


class LaneTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home, self.keel = self.root/'enterprise', self.root/'applicant'
        self.lane = m.Lane(self.home, exclude_keel_home=self.keel, enabled=True, now=NOW)
        self.lane.init()
        self.packet = m.fixture('agency-a', NOW)

    def put(self):
        return self.lane.ingest(self.packet)['packet_hash']

    def ready(self):
        h = self.put()
        self.lane.run()
        return h

    def approved(self):
        h = self.ready()
        self.lane.review('agency-a', h, 'approve', 'synthetic-reviewer-a')
        return h

    def grant(self, h):
        return {'synthetic': True, 'account_id': 'agency-a', 'packet_hash': h,
                'scope': 'offline_pilot', 'reference': 'https://permission.example/agency-a',
                'expires_at': m.iso(NOW + timedelta(days=7))}

    def later(self, days):
        return m.Lane(self.home, exclude_keel_home=self.keel, enabled=True, now=NOW+timedelta(days=days))

    def state(self, lane=None):
        return (lane or self.lane).report()['accounts'][0]['state']

    def test_disabled_has_no_filesystem_effect(self):
        p = self.root/'disabled'
        with self.assertRaisesRegex(m.Refused, 'DISABLED'):
            m.Lane(p, exclude_keel_home=self.keel)
        self.assertFalse(p.exists())

    def test_exact_boolean_opt_in(self):
        for enabled in (1, 'true', 'yes', None):
            with self.subTest(enabled=enabled), self.assertRaises(m.Refused):
                m.Lane(self.root/'disabled', exclude_keel_home=self.keel, enabled=enabled)

    def test_import_has_no_side_effects(self):
        with patch('socket.socket', side_effect=AssertionError('network')):
            module = importlib.util.module_from_spec(spec)
            with patch.object(Path, 'mkdir', side_effect=AssertionError('filesystem')):
                spec.loader.exec_module(module)

    def test_no_job_or_network_imports(self):
        names = set()
        for node in ast.walk(ast.parse(SOURCE.read_text())):
            if isinstance(node, ast.Import): names.update(n.name.split('.')[0] for n in node.names)
            if isinstance(node, ast.ImportFrom): names.add((node.module or '').split('.')[0])
        self.assertTrue(names.isdisjoint({'engines','keel_next','keel_srf','requests','http','socket','smtplib','subprocess'}))

    def test_workspace_overlap_both_directions(self):
        for p in (self.keel, self.keel/'nested', self.root):
            with self.subTest(p=p), self.assertRaisesRegex(m.Refused, 'OVERLAP'):
                m.Lane(p, exclude_keel_home=self.keel, enabled=True, now=NOW)

    def test_source_tree_workspace_refused(self):
        with self.assertRaisesRegex(m.Refused, 'OVERLAP'):
            m.Lane(SOURCE.parent/'runtime', exclude_keel_home=self.keel, enabled=True, now=NOW)

    def test_keel_home_environment_excluded(self):
        with patch.dict(os.environ, {'KEEL_HOME':str(self.home)}), self.assertRaisesRegex(m.Refused, 'OVERLAP'):
            m.Lane(self.home, exclude_keel_home=self.keel, enabled=True, now=NOW)

    def test_relative_home_refused(self):
        with self.assertRaises(m.Refused):
            m.Lane('relative', exclude_keel_home=self.keel, enabled=True)

    def test_symlink_parent_refused(self):
        link = self.root/'linked'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(m.Refused, 'SYMLINK'):
            m.Lane(link/'new', exclude_keel_home=self.keel, enabled=True)

    def test_existing_directory_refused(self):
        with self.assertRaisesRegex(m.Refused, 'FRESH_HOME'):
            self.lane.init()

    def test_private_modes(self):
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.lane.dbfile.stat().st_mode & 0o777, 0o600)

    def test_insecure_file_mode_refused(self):
        self.lane.dbfile.chmod(0o644)
        with self.assertRaisesRegex(m.Refused, 'UNSAFE_WORKSPACE_FILE'):
            self.lane.report()

    def test_database_symlink_refused(self):
        saved = self.root/'saved.sqlite3'
        self.lane.dbfile.rename(saved)
        self.lane.dbfile.symlink_to(saved)
        with self.assertRaises(m.Refused): self.lane.report()

    def test_database_hardlink_refused(self):
        os.link(self.lane.dbfile, self.root/'hardlink')
        with self.assertRaises(m.Refused): self.lane.report()

    def test_unknown_sensitive_fields_refused(self):
        for field in ('email','phone','resume','applicant','health','policyholder','outreach_authorized','ssn','credentials'):
            p = dict(self.packet, **{field: 'sentinel'})
            with self.subTest(field=field), self.assertRaisesRegex(m.Refused, 'SCHEMA'):
                self.lane.ingest(p)
        self.assertEqual(self.lane.report()['unique_accounts'], 0)

    def test_source_type_and_synthetic_label_refused(self):
        for key,value in (('synthetic',False),('synthetic',1),('source_type','recruiter_email'),
                          ('source_type','authorized_company'),('lane','job_application')):
            with self.subTest(key=key,value=value), self.assertRaises(m.Refused):
                self.lane.ingest(dict(self.packet, **{key:value}))

    def test_real_and_ambiguous_domains_refused(self):
        for domain in ('agency.com','agency.example.com','localhost','127.0.0.1','foo..example','a.example.', 'A.example'):
            with self.subTest(domain=domain), self.assertRaises(m.Refused):
                self.lane.ingest(dict(self.packet,business_domain=domain))

    def test_name_and_identifier_injection_refused(self):
        for key,value in (('organization_name','<script>alert(1)</script>'),('organization_name','Real Agency'),
                          ('account_id','../../x'),('account_id','x;DROP TABLE accounts;')):
            with self.subTest(key=key), self.assertRaises(m.Refused):
                self.lane.ingest(dict(self.packet, **{key:value}))

    def test_malformed_nested_fields_refused(self):
        for key,value in (('criterion','unknown'),('value',True),('permission','approved'),('extra','text')):
            p = copy.deepcopy(self.packet); p['evidence'][0][key] = value
            with self.subTest(key=key), self.assertRaises(m.Refused): self.lane.ingest(p)

    def test_evidence_url_boundaries(self):
        for url in ('http://a.example/e','https://a.com/e','https://u@a.example/e',
                    'https://a.example:443/e','https://a.example/e?q=x','https://a.example/e#x',
                    'file:///etc/passwd','javascript:alert(1)','https://127.0.0.1/e',
                    'https://a.example/%2e%2e/x'):
            p=copy.deepcopy(self.packet);p['evidence'][0]['source_url']=url
            with self.subTest(url=url),self.assertRaises(m.Refused):self.lane.ingest(p)

    def test_timezone_required(self):
        p=copy.deepcopy(self.packet);p['evidence'][0]['observed_at']='2026-10-08T18:00:00'
        with self.assertRaises(m.Refused):self.lane.ingest(p)

    def test_time_window_bounds(self):
        p=copy.deepcopy(self.packet);p['evidence'][0]['expires_at']=m.iso(NOW+timedelta(days=40))
        with self.assertRaises(m.Refused):self.lane.ingest(p)
        for days in (0,91):
            p=dict(self.packet,retention_until=m.iso(NOW+timedelta(days=days)))
            with self.subTest(days=days),self.assertRaises(m.Refused):self.lane.ingest(p)

    def test_json_duplicates_and_nonfinite_refused(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{{', '['*2000):
            with self.subTest(text=text[:30]),self.assertRaises(m.Refused):m.load_json(text)

    def test_input_size_and_observation_bounds(self):
        with self.assertRaises(m.Refused):m.load_json(' '*65537)
        p=copy.deepcopy(self.packet);p['evidence']=p['evidence']*5
        with self.assertRaises(m.Refused):self.lane.ingest(p)

    def test_applicant_file_never_read(self):
        self.keel.mkdir();file=self.keel/'profile.json';file.write_text(json.dumps(self.packet))
        with patch('os.open',side_effect=AssertionError('must not open applicant input')):
            with self.assertRaisesRegex(m.Refused,'APPLICANT_SOURCE'):
                self.lane.read_packet(file)

    def test_input_symlink_and_hardlink_refused(self):
        file=self.root/'input.json';file.write_text(json.dumps(self.packet))
        link=self.root/'symlink';link.symlink_to(file)
        with self.assertRaises(m.Refused):self.lane.read_packet(link)
        hard=self.root/'hard';os.link(file,hard)
        with self.assertRaises(m.Refused):self.lane.read_packet(hard)

    def test_input_file_load(self):
        file=self.root/'input.json';file.write_text(json.dumps(self.packet))
        self.assertEqual(self.lane.read_packet(file),m.validate(self.packet))

    def test_exact_replay_no_new_events(self):
        self.put();before=self.lane.report()
        self.assertEqual(self.lane.ingest(self.packet)['result'],'duplicate')
        self.assertEqual(before,self.lane.report())

    def test_reordered_evidence_is_duplicate(self):
        self.put();self.packet['evidence'].reverse()
        self.assertEqual(self.lane.ingest(self.packet)['result'],'duplicate')

    def test_conflicting_domain_or_id_not_merged(self):
        self.put()
        for p in (dict(self.packet,account_id='other'),dict(self.packet,business_domain='other.example')):
            with self.assertRaisesRegex(m.Refused,'CONFLICT'):self.lane.ingest(p)
        self.assertEqual(self.lane.report()['unique_accounts'],1)

    def test_missing_evidence_does_not_inflate_score(self):
        self.packet['evidence']=[];self.put();self.lane.run()
        row=self.lane.report()['accounts'][0]
        self.assertEqual((row['state'],row['score']),('PARKED',0))

    def test_score_does_not_override_required_negative(self):
        self.packet=m.fixture('agency-a',NOW,no='need');self.put();self.lane.run()
        row=self.lane.report()['accounts'][0]
        self.assertEqual(row['score'],75);self.assertEqual(row['state'],'PARKED')
        self.assertIn('REQUIRED_NO:need',row['reasons'])

    def test_optional_negative_can_qualify(self):
        self.packet=m.fixture('agency-a',NOW,no='volume');self.ready()
        self.assertEqual(self.lane.report()['accounts'][0]['score'],85)
        self.assertEqual(self.state(),'QUALIFIED_FOR_DISCOVERY')

    def test_unknown_critical_or_optional_parks(self):
        for criterion in m.WEIGHTS:
            p=m.fixture('agency-a',NOW,missing=criterion)
            with self.subTest(criterion=criterion):self.assertEqual(m.assess(p,NOW)['state'],'PARKED')

    def test_conflict_parks(self):
        self.packet['evidence'].append(dict(self.packet['evidence'][0],value='no'))
        self.ready();self.assertEqual(self.state(),'PARKED')

    def test_future_evidence_parks_without_automatic_review(self):
        for e in self.packet['evidence']:e['observed_at']=m.iso(NOW+timedelta(days=1))
        h=self.ready();later=self.later(2)
        self.assertEqual(later.report()['accounts'][0]['state'],'PARKED')
        with self.assertRaises(m.Refused):later.review('agency-a',h,'approve','synthetic-reviewer-a')
        later.run();self.assertEqual(self.state(later),'QUALIFIED_FOR_DISCOVERY')

    def test_duplicate_observations_do_not_boost_score(self):
        p=m.fixture('agency-a',NOW,missing='need');p['evidence']*=3
        self.assertEqual(m.assess(m.validate(p),NOW)['score'],75)

    def test_no_review_before_evaluation(self):
        h=self.put()
        with self.assertRaises(m.Refused):self.lane.review('agency-a',h,'approve','synthetic-reviewer-a')

    def test_stale_review_hash_refused(self):
        self.ready()
        with self.assertRaisesRegex(m.Refused,'STALE'):self.lane.review('agency-a','0'*64,'approve','synthetic-reviewer-a')

    def test_bad_reviewer_refused(self):
        h=self.ready()
        with self.assertRaises(m.Refused):self.lane.review('agency-a',h,'approve','real-person')

    def test_approval_replay_idempotent(self):
        h=self.approved();before=self.lane.report()
        self.lane.review('agency-a',h,'approve','synthetic-reviewer-a')
        self.assertEqual(before,self.lane.report())

    def test_revision_clears_approval(self):
        h=self.approved();p=copy.deepcopy(self.packet);p['retention_until']=m.iso(NOW+timedelta(days=59))
        result=self.lane.revise(p,h)
        self.assertNotEqual(result['packet_hash'],h);self.assertEqual(self.state(),'DISCOVERED')
        with self.assertRaises(m.Refused):self.lane.pilot(self.grant(h))

    def test_same_revision_preserves_state(self):
        h=self.approved();before=self.lane.report();self.lane.revise(self.packet,h)
        self.assertEqual(before,self.lane.report())

    def test_expired_revision_cannot_resurrect(self):
        h=self.ready();p=m.fixture('agency-a',NOW+timedelta(days=61))
        with self.assertRaises(m.Refused):self.later(61).revise(p,h)

    def test_pilot_requires_separate_review(self):
        h=self.ready()
        with self.assertRaises(m.Refused):self.lane.pilot(self.grant(h))

    def test_complete_pilot_still_has_no_effect_authority(self):
        h=self.approved();result=self.lane.pilot(self.grant(h))
        self.assertEqual(result['state'],'PILOT_CANDIDATE_SIMULATED')
        self.assertFalse(result['outreach_authorized'])
        for action in ('email','call','sms','scrape','crm_write','quote','submit','unknown'):
            self.assertFalse(self.lane.external_action(action)['allowed'])

    def test_real_or_unscoped_grant_refused(self):
        h=self.approved()
        for key,value in (('synthetic',False),('scope','live'),('account_id','other'),('packet_hash','0'*64)):
            with self.subTest(key=key),self.assertRaises(m.Refused):self.lane.pilot(dict(self.grant(h),**{key:value}))

    def test_expired_and_excessive_grant_refused(self):
        h=self.approved()
        for days in (0,15):
            with self.subTest(days=days),self.assertRaises(m.Refused):
                self.lane.pilot(dict(self.grant(h),expires_at=m.iso(NOW+timedelta(days=days))))

    def test_expiry_checked_on_read_not_only_sweep(self):
        h=self.approved();self.lane.pilot(self.grant(h))
        self.assertEqual(self.state(self.later(7)),'PARKED')
        self.assertEqual(self.state(self.later(20)),'PARKED')
        self.assertEqual(self.state(self.later(60)),'EXPIRED')

    def test_pause_blocks_progress_but_not_suppression(self):
        h=self.ready();self.lane.pause(True)
        with self.assertRaises(m.Refused):self.lane.run()
        with self.assertRaises(m.Refused):self.lane.ingest(m.fixture('other',NOW))
        with self.assertRaises(m.Refused):self.lane.review('agency-a',h,'approve','synthetic-reviewer-a')
        self.lane.suppress('agency-a');self.assertEqual(self.state(),'SUPPRESSED')
        self.lane.pause(False);self.lane.run();self.assertEqual(self.state(),'SUPPRESSED')

    def test_suppression_survives_duplicate_and_revision(self):
        h=self.ready();self.lane.suppress('agency-a');self.lane.ingest(self.packet)
        self.assertEqual(self.state(),'SUPPRESSED')
        with self.assertRaises(m.Refused):self.lane.revise(self.packet,h)

    def test_rejection_terminal(self):
        h=self.ready();self.lane.review('agency-a',h,'reject','synthetic-reviewer-a')
        self.lane.run();self.assertEqual(self.state(),'REJECTED')
        with self.assertRaises(m.Refused):self.lane.review('agency-a',h,'approve','synthetic-reviewer-a')

    def test_purge_is_logical_and_idempotent(self):
        self.ready();lane=self.later(60)
        self.assertEqual(lane.purge(),{'logically_purged':1,'secure_erasure':False})
        self.assertEqual(lane.purge()['logically_purged'],0)
        self.assertEqual(self.state(lane),'EXPIRED')
        with closing(sqlite3.connect(lane.dbfile)) as db, db:
            self.assertEqual(db.execute('SELECT packet,domain FROM accounts').fetchone(),(None,None))
        self.assertIsNone(lane.report()['accounts'][0]['domain'])

    def test_readonly_report_does_not_rewrite_database(self):
        self.ready();before=self.lane.dbfile.read_bytes()
        self.lane.report();self.later(60).report()
        self.assertEqual(before,self.lane.dbfile.read_bytes())

    def test_packet_tamper_fails_closed(self):
        self.ready()
        with closing(sqlite3.connect(self.lane.dbfile)) as db, db:db.execute("UPDATE accounts SET packet='{}'")
        with self.assertRaises(m.Refused):self.lane.report()

    def test_state_tamper_fails_closed(self):
        self.ready()
        with closing(sqlite3.connect(self.lane.dbfile)) as db, db:db.execute("UPDATE accounts SET state='PILOT_CANDIDATE_SIMULATED'")
        with self.assertRaises(m.Refused):self.lane.report()

    def test_audit_tamper_and_account_deletion_fail(self):
        self.ready()
        with closing(sqlite3.connect(self.lane.dbfile)) as db, db:db.execute('DELETE FROM accounts')
        with self.assertRaises(m.Refused):self.lane.report()

    def test_audit_tail_deletion_fails(self):
        self.ready()
        with closing(sqlite3.connect(self.lane.dbfile)) as db, db:db.execute('DELETE FROM events WHERE seq=(SELECT max(seq) FROM events)')
        with self.assertRaises(m.Refused):self.lane.report()

    def test_pause_tamper_fails(self):
        self.lane.pause(True)
        with closing(sqlite3.connect(self.lane.dbfile)) as db, db:db.execute("UPDATE meta SET value='false' WHERE key='paused'")
        with self.assertRaises(m.Refused):self.lane.report()

    def test_clock_rollback_refused(self):
        with self.assertRaisesRegex(m.Refused,'CLOCK'):self.later(-1).report()

    def test_unexpected_journal_requires_review(self):
        p=Path(str(self.lane.dbfile)+'-journal');p.write_text('sentinel')
        with self.assertRaisesRegex(m.Refused,'SIDECAR'):self.lane.report()

    def test_transaction_failure_rolls_back_entire_batch(self):
        self.put();before=self.lane.report();original=self.lane._record
        def fail(db,account,action):
            if action=='EVALUATE':raise RuntimeError('injected interruption')
            return original(db,account,action)
        with patch.object(self.lane,'_record',side_effect=fail),self.assertRaises(RuntimeError):self.lane.run()
        self.assertEqual(before,self.lane.report())
        self.lane.run();self.assertEqual(self.state(),'QUALIFIED_FOR_DISCOVERY')

    def test_bounded_run_skips_unchanged_parked(self):
        p=m.fixture('hold',NOW,missing='need');self.lane.ingest(p);self.lane.run()
        for i in range(5):self.lane.ingest(m.fixture(f'account-{i}',NOW))
        self.assertEqual(self.lane.run(2)['count'],2)
        self.assertEqual(self.lane.run(2)['count'],2)
        self.assertEqual(self.lane.run(2)['count'],1)
        self.assertEqual(self.lane.run(2)['count'],0)

    def test_batch_bounds_and_caps(self):
        for limit in (0,101,True,-1):
            with self.subTest(limit=limit),self.assertRaises(m.Refused):self.lane.run(limit)
        self.put()
        with patch.object(m,'MAX_ACCOUNTS',1),self.assertRaises(m.Refused):self.lane.ingest(m.fixture('other',NOW))
        before=self.lane.report()
        with patch.object(m,'MAX_EVENTS',3),self.assertRaises(m.Refused):self.lane.run()
        self.assertEqual(before,self.lane.report())

    def test_simultaneous_duplicate_ingest_one_account(self):
        def ingest(_):
            return m.Lane(self.home,exclude_keel_home=self.keel,enabled=True,now=NOW).ingest(self.packet)['result']
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(ingest,range(8)))
        self.assertEqual(results.count('created'),1)
        self.assertEqual(results.count('duplicate'),7)
        self.assertEqual(self.lane.report()['unique_accounts'],1)

    def test_restart_preserves_review_and_audit(self):
        h=self.approved();self.lane.pilot(self.grant(h));report=self.lane.report()
        self.assertEqual(report,self.later(0).report())

    def test_html_escapes_untrusted_values(self):
        self.ready();report=self.lane.report();report['accounts'][0]['account_id']='<script>alert(1)</script>'
        page=m.render_html(report)
        self.assertNotIn('<script>',page);self.assertIn('&lt;script&gt;',page)
        self.assertIn("default-src 'none'",page)

    def test_demo_has_exact_reconciled_counts_and_no_network(self):
        lane=m.Lane(self.root/'demo',exclude_keel_home=self.keel,enabled=True,now=NOW)
        with patch('socket.socket',side_effect=AssertionError('network')),patch('socket.create_connection',side_effect=AssertionError('network')):
            result=m.demo(lane)
        r=result['report']
        self.assertEqual(r['state_counts'],{'PARKED':5,'PILOT_CANDIDATE_SIMULATED':1,'QUALIFIED_FOR_DISCOVERY':1,'REJECTED':1,'SUPPRESSED':1})
        self.assertEqual(sum(r['state_counts'].values()),r['unique_accounts'])
        self.assertEqual(result['duplicate'],'duplicate');self.assertEqual(result['invalid_record'],'SCHEMA_FIELDS_REFUSED')
        self.assertEqual(r['real_accounts_evaluated'],0);self.assertIsNone(r['revenue'])

    def test_cli_disabled_returns_nonzero_and_no_root(self):
        p=self.root/'cli'
        result=subprocess.run([sys.executable,'-S',str(SOURCE),'--home',str(p),'--exclude-keel-home',str(self.keel),'init'],
                              capture_output=True,text=True,env=os.environ.copy(),timeout=10)
        self.assertEqual(result.returncode,2);self.assertIn('LANE_DISABLED',result.stderr);self.assertFalse(p.exists())

    def test_cli_demo_then_html_report(self):
        env=dict(os.environ,KEEL_ENTERPRISE_INSURANCE_ENABLED='true');p=self.root/'cli'
        command=[sys.executable,'-S',str(SOURCE),'--home',str(p),'--exclude-keel-home',str(self.keel)]
        result=subprocess.run(command+['demo'],capture_output=True,text=True,env=env,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['report']['unique_accounts'],9)
        result=subprocess.run(command+['report','--format','html'],capture_output=True,text=True,env=env,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('SYNTHETIC ONLY',result.stdout)
        result=subprocess.run(command+['request-external','--action','email'],capture_output=True,text=True,env=env,timeout=10)
        self.assertEqual(result.returncode,2);self.assertFalse(json.loads(result.stdout)['allowed'])


    def test_bounded_batch_intake_duplicates_and_rollback(self):
        result=self.lane.ingest_batch([self.packet,self.packet,m.fixture('other',NOW)])
        self.assertEqual([r['result'] for r in result],['created','duplicate','created'])
        before=self.lane.report()
        conflict=dict(self.packet,business_domain='conflict.example')
        with self.assertRaises(m.Refused):self.lane.ingest_batch([m.fixture('new',NOW),conflict])
        self.assertEqual(before,self.lane.report())

    def test_batch_invalid_record_writes_nothing(self):
        invalid=dict(self.packet,email='synthetic@example.com')
        before=self.lane.report()
        with self.assertRaises(m.Refused):self.lane.ingest_batch([self.packet,invalid])
        self.assertEqual(before,self.lane.report())
        for packets in ([],[self.packet]*101,{},None):
            with self.assertRaises(m.Refused):self.lane.ingest_batch(packets)

    def test_fifo_refused_without_waiting_for_writer(self):
        fifo=self.root/'input.fifo';os.mkfifo(fifo)
        with self.assertRaisesRegex(m.Refused,'INPUT_FILE'):self.lane.read_packet(fifo)

    def test_exhaustive_score_policy_729_combinations(self):
        import itertools
        for values in itertools.product(('yes','no','unknown'),repeat=6):
            p=copy.deepcopy(self.packet)
            for e,value in zip(p['evidence'],values):e['value']=value
            score=sum(weight for weight,value in zip(m.WEIGHTS.values(),values) if value=='yes')
            expected=('unknown' not in values and all(values[i]=='yes' for i in (0,1,2)) and score>=75)
            result=m.assess(m.validate(p),NOW)
            with self.subTest(values=values):
                self.assertEqual(result['score'],score)
                self.assertEqual(result['state']=='QUALIFIED_FOR_DISCOVERY',expected)

    def test_concurrent_revisions_use_exact_hash_lease(self):
        h=self.ready()
        def revise(i):
            p=dict(self.packet,retention_until=m.iso(NOW+timedelta(days=40+i)))
            lane=m.Lane(self.home,exclude_keel_home=self.keel,enabled=True,now=NOW)
            try:lane.revise(p,h);return 'changed'
            except m.Refused as error:return str(error)
        with ThreadPoolExecutor(max_workers=4) as pool:outcomes=list(pool.map(revise,range(8)))
        self.assertEqual(outcomes.count('changed'),1)
        self.assertEqual(outcomes.count('STALE_REVIEW_REFUSED'),7)
        self.assertEqual(self.state(),'DISCOVERED')

    def test_process_crash_fails_closed_pending_journal_review(self):
        self.ready()
        code=("import os, sqlite3; "
              "db=sqlite3.connect("+repr(str(self.lane.dbfile))+"); "
              "db.execute('BEGIN IMMEDIATE'); "
              "db.execute(\"UPDATE accounts SET state='SUPPRESSED'\"); os._exit(23)")
        result=subprocess.run([sys.executable,'-S','-c',code],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,23,result.stderr)
        with self.assertRaisesRegex(m.Refused,'SIDECAR_REQUIRES_OPERATOR_REVIEW'):self.lane.report()

    def test_target_python_syntax_versions(self):
        for version in (11,12):ast.parse(SOURCE.read_text(),feature_version=(3,version))


if __name__=='__main__':
    unittest.main()
