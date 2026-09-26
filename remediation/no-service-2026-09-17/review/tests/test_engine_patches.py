"""Regression cases against patched source, not a duplicate implementation."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch, Mock

EXECUTOR = Path(os.environ["KEEL_TEST_EXECUTOR"])
sys.path.insert(0,str(EXECUTOR))
import queue_io as q
import parking_schema as parking
import launch_lock as locks
import api_direct_loop as direct


class EnginePatches(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.qpath=self.root/"queue.json"; self.lockpath=self.root/"q.lock"
        self.patch=patch.object(locks,"LOCK_DIR",str(self.root/"launch"));self.patch.start();self.addCleanup(self.patch.stop)

    def test_list_notes_valid(self):
        entry={"unresolved":["Need actual answer"],"status_reason":"Missing consent","queue_notes":["Question quoted"]}
        self.assertEqual(parking.validate_park_entry(entry),[])

    def test_empty_or_malformed_notes_refused(self):
        for note in ([],[""],[1],None):
            entry={"unresolved":["x"],"status_reason":"x","queue_notes":note}
            self.assertTrue(parking.validate_park_entry(entry))

    def test_wrapper_preserved(self):
        for key in ("leads","entries","items"):
            self.qpath.write_text(json.dumps({key:[{"role_id":"a","status":"READY"}],"meta":"keep"}))
            q.patch_entry(self.qpath,"a",{"value":1},lock_path=self.lockpath)
            result=json.loads(self.qpath.read_text())
            self.assertEqual(result["meta"],"keep");self.assertEqual(result[key][0]["value"],1)

    def test_duplicate_queue_role_refused(self):
        self.qpath.write_text('[{"role_id":"a"},{"role_id":"a"}]')
        before=self.qpath.read_bytes()
        with self.assertRaises(ValueError):q.patch_entry(self.qpath,"a",{"x":1},lock_path=self.lockpath)
        self.assertEqual(self.qpath.read_bytes(),before)

    def test_no_orphan_parking(self):
        self.qpath.write_text('[]')
        with self.assertRaises(KeyError):q.park_entry(self.qpath,"absent",["question"],"Needs consent",lock_path=self.lockpath)

    def test_parking_persists_reason(self):
        self.qpath.write_text('[{"role_id":"a","status":"READY"}]')
        q.park_entry(self.qpath,"a",["exact question"],"Needs consent",lock_path=self.lockpath)
        row=json.loads(self.qpath.read_text())[0]
        self.assertEqual(row["status"],"NEEDS_INPUT");self.assertEqual(row["unresolved"],["exact question"])

    def test_explicit_lock_reentrant_and_path_local(self):
        original=q.get_lock_path()
        with q.queue_lock(lock_path=self.lockpath):
            with q.queue_lock(lock_path=self.lockpath):
                with q.queue_lock(lock_path=self.root/"other.lock"):pass
        self.assertEqual(original,q.get_lock_path())

    def test_durable_write_preserves_original_on_serialization_failure(self):
        self.qpath.write_text('[1]')
        with self.assertRaises(ValueError):q.atomic_write_json(self.qpath,[float('nan')])
        self.assertEqual(self.qpath.read_text(),'[1]')
        self.assertFalse(list(self.root.glob('.queue-*')))

    def test_twenty_writers_preserve_updates(self):
        self.qpath.write_text('[{"role_id":"a"}]')
        with ThreadPoolExecutor(max_workers=20) as pool:
            results=list(pool.map(lambda i:q.patch_entry(self.qpath,"a",{f"field{i}":i},lock_path=self.lockpath),range(20)))
        row=json.loads(self.qpath.read_text())[0]
        self.assertTrue(all(results));self.assertEqual(len(row),21)

    def test_twenty_launch_claimers_one_winner(self):
        with ThreadPoolExecutor(max_workers=20) as pool:
            results=list(pool.map(lambda i:locks.acquire("a",f"worker{i}"),range(20)))
        self.assertEqual(sum(r[0] for r in results),1)

    def test_release_does_not_remove_other_owner(self):
        locks.acquire("a","worker1")
        self.assertFalse(locks.release("a","worker2")[0])
        self.assertEqual(locks.check("a")["task_id"],"worker1")

    def test_corrupt_lock_is_not_free(self):
        (self.root/"launch").mkdir();(self.root/"launch"/"a.json").write_text('{broken')
        self.assertFalse(locks.acquire("a","worker")[0])

    def test_guard_conflicting_and_free_text_go_refused(self):
        for value in ('GO','traceback GO','{"status":"HELD","verdict":"GO"}'):
            self.assertFalse(direct._parse_guard_verdict(value)[0])

    def entry(self):
        return {"role_id":"a","_board":"acme","_origin":"standard","company":"Acme","title":"Operations"}

    def process(self, *, packet=True, screen="api_fastlane", dry_run=False, grant=True, submit_error=None):
        mocks={name:Mock() for name in ("guard","release_pre_dispatch_lock","request_preparation","record_board_stat",
                                      "note_edge_case","telemetry")}
        mocks["guard"].return_value=(True,"GO")
        with patch.multiple(direct,**mocks), patch.object(direct,"blocklisted",return_value=False), \
             patch.object(direct,"find_packet",return_value=({"role_id":"a"},"a.json") if packet else (None,None)), \
             patch.object(direct.fl,"fastlane_screen",return_value={"route":screen,"reason":"fixture","fingerprint":"fixture-hash"}), \
             patch.object(direct.asu,"stage_for_human_review",return_value={"status":"staged"}), \
             patch.object(direct.asu,"grant_human_approval",return_value=(grant,"fixture")), \
             patch.object(direct.asu,"try_api_submit",side_effect=submit_error,return_value={"submitted":False,"reason":"ambiguous"}) as submit:
            result=direct.process_lead(self.entry(),{},dry_run=dry_run)
            return result,mocks,submit

    def test_missing_packet_requests_build_without_lock(self):
        result,mocks,_=self.process(packet=False)
        self.assertEqual(result["outcome"],"needs_preparation")
        mocks["request_preparation"].assert_called_once_with(self.entry(),"packet_build")
        mocks["guard"].assert_not_called()

    def test_browser_refusal_does_not_acquire_lock(self):
        _,mocks,_=self.process(screen="browser")
        mocks["guard"].assert_not_called()
        mocks["request_preparation"].assert_called_once()

    def test_dry_run_no_lock_no_grant_or_handoff(self):
        for packet in (True,False):
            _,mocks,submit=self.process(dry_run=True,packet=packet)
            mocks["guard"].assert_not_called();mocks["request_preparation"].assert_not_called();submit.assert_not_called()

    def test_grant_refusal_releases_only_acquired_token(self):
        _,mocks,_=self.process(grant=False)
        token=mocks["guard"].call_args.kwargs["task_id"]
        mocks["release_pre_dispatch_lock"].assert_called_once_with("a",token)

    def test_dispatch_exception_retains_lock(self):
        _,mocks,_=self.process(submit_error=TimeoutError("uncertain POST"))
        mocks["release_pre_dispatch_lock"].assert_not_called()

    def test_ambiguous_result_retains_lock(self):
        _,mocks,_=self.process()
        mocks["release_pre_dispatch_lock"].assert_not_called()

    def test_claim_tokens_unique_per_invocation(self):
        _,first,_=self.process(grant=False);_,second,_=self.process(grant=False)
        self.assertNotEqual(first["guard"].call_args.kwargs["task_id"],second["guard"].call_args.kwargs["task_id"])

    def test_preparation_request_durable_in_existing_queue(self):
        self.qpath.write_text('{"entries":[{"role_id":"a","status":"READY"}]}')
        old=q.get_lock_path();q.set_lock_path(str(self.lockpath));self.addCleanup(q.set_lock_path,old)
        with patch.object(direct,"STD_Q",str(self.qpath)):
            direct.request_preparation(self.entry(),"packet_build")
        row=json.loads(self.qpath.read_text())["entries"][0]
        self.assertEqual(row["status"],"READY")
        self.assertEqual(row["preparation_request"]["kind"],"packet_build")

    def test_fastlane_preflight_does_not_stage_approval(self):
        with patch.object(direct.fl,"resolve_lead",return_value=({"role_id":"a"},"standard")), \
             patch.object(direct.asu,"try_api_submit",return_value={"reason":"dry_run_ok", "payload_fingerprint":"hash"}), \
             patch.object(direct.asu,"stage_for_human_review") as stage:
            result=direct.fl.fastlane_screen({"role_id":"a","ats":"greenhouse"},{},stage=False)
        stage.assert_not_called();self.assertIs(result["staged"],False)

    def test_grant_exception_releases_owned_lock(self):
        with patch.object(direct,"blocklisted",return_value=False), \
             patch.object(direct,"find_packet",return_value=({"role_id":"a"},"a.json")), \
             patch.object(direct.fl,"fastlane_screen",return_value={"route":"api_fastlane","fingerprint":"f","reason":"fixture"}), \
             patch.object(direct,"guard",return_value=(True,"GO")), \
             patch.object(direct.asu,"stage_for_human_review",side_effect=ValueError("failed stage")), \
             patch.object(direct,"release_pre_dispatch_lock") as release:
            with self.assertRaises(ValueError):direct.process_lead(self.entry(),{})
        release.assert_called_once()

    def test_main_dry_run_writes_no_http_meter(self):
        for rows in ([],[self.entry()]):
            with patch.object(direct,"clean_boards",return_value={"acme"}), \
                 patch.object(direct.fl,"_load_json",return_value={}), \
                 patch.object(direct,"eligible_leads",return_value=rows), \
                 patch.object(direct,"process_lead",return_value={"role_id":"a","outcome":"dry_run_ok"}), \
                 patch.object(direct,"HTTP_COUNT",str(self.root/"must-not-exist.jsonl")), \
                 patch.object(direct,"telemetry") as telemetry:
                self.assertEqual(direct.main(["--dry-run"]),0)
            self.assertFalse((self.root/"must-not-exist.jsonl").exists())
            telemetry.assert_not_called()
