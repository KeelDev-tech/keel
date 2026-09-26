"""Faults exercised against Keel's actual board, outbox and crash adapters."""
from contextlib import ExitStack
import json
from pathlib import Path
import runpy
import tempfile
import time
from unittest.mock import patch
from urllib.error import HTTPError

from keel_machine.common import require


def run():
    runpy.run_path(str(Path(__file__).resolve().parents[1]/'keel.py'),run_name='faultlab_loader')
    import pipeline_service as service
    import log_event
    import queue_io
    from safe_io import atomic_json,read_json
    checks={}
    with tempfile.TemporaryDirectory(prefix='keel-faultlab-') as directory:
        root=Path(directory)
        with ExitStack() as stack:
            stack.enter_context(patch.object(queue_io,'_LOCK_PATH',str(root/'queue.lock')))
            stack.enter_context(patch.object(log_event,'EVENTS',str(root/'events.jsonl')))
            for name in service.QUEUES:atomic_json(root/'data/queues'/f'{name}-queue.json',[])
            atomic_json(root/'data/application-ledger.json',[])
            service.add_source(root,'greenhouse:fixture','Fixture')
            def expired(*_):raise HTTPError('https://boards-api.greenhouse.io',401,'fixture',{},None)
            def limited(*_):raise HTTPError('https://boards-api.greenhouse.io',429,'fixture',{},None)
            def delayed(*_):raise TimeoutError('fixture')
            for name, fetch in [('expired_session',expired),('rate_limit',limited),('delayed_response',delayed)]:
                result=service.discover(root,reader=service.PublicBoardReader(fetcher=fetch))
                checks[name+'_adds_nothing']=read_json(root/'data/queues/standard-queue.json')==[]
                checks[name+'_reported']=result.get('status')!='COMPLETED' or result.get('added',0)==0
            def late_response(*_):
                reader.deadline=time.monotonic()-1
                return {'jobs':[]}
            reader=service.PublicBoardReader(fetcher=late_response)
            late=service.discover(root,reader=reader)
            checks['deadline_discards_response']=read_json(root/'data/queues/standard-queue.json')==[] and late.get('added',0)==0
            pending=[{'role_id':'fault-'+str(i),'verification_event_pending':{
                'event_id':'fault-event-'+str(i),'event_type':'lead_verified',
                'source':'fixture','details':{'synthetic':True}}} for i in range(4)]
            queue=root/'data/queues/standard-queue.json';atomic_json(queue,pending)
            with patch.object(log_event,'_sync_event_directory',side_effect=OSError('fixture')):
                failed=service.flush_outbox(root)
            checks['sync_failure_keeps_all_pending']=failed['pending']==4 and failed['emitted']==0
            recovered=service.flush_outbox(root);again=service.flush_outbox(root)
            records=[json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()]
            checks['duplicate_receipts_deduplicated']=recovered['emitted']==4 and again['emitted']==0 and len(records)==4
        from keel_next.qualification import qualify
        crash=qualify(root/'crashes')
        checks['actual_process_crashes_recover']=crash['status']=='QUALIFICATION_PASSED'
    return {'status':'FAULTLAB_PASSED' if all(checks.values()) else 'FAULTLAB_FAILED',
            'checks':checks,'synthetic':True,'external_actions':0}
