"""Aggregate executable private fixtures; native/account capability stays unknown."""
from pathlib import Path

from .common import atomic_json,new_home,outside_source


def run_demo(home):
    from . import (backup,bridge,browser,capabilities,context,coordinator,dashboard,
                   evaluation,integration,reconciliation,repair,review,session,sources)
    root=Path(__file__).resolve().parents[1]
    home=new_home(outside_source(home,root))
    components={}
    components['capabilities']=capabilities.probe_host()
    components['sources']=sources.demo(home/'sources')
    components['context']=context.demo(home/'context')
    components['bridge']=bridge.demo()
    components['browser']=browser.demo(home/'browser')
    components['coordinator']=coordinator.demo(home/'coordinator')
    snapshot=review.example_snapshot()
    components['review']=review.project_review(snapshot,now=snapshot['captured_at'])
    components['dashboard']=dashboard.demo(new_home(home/'dashboard'))
    components['evaluation']=evaluation.demo()
    components['repair']=repair.demo()
    components['backup']=backup.demo(home/'backup')
    components['integration']=integration.demo(home/'integration')
    components['session']=session.demo(home/'session')
    components['reconciliation']=reconciliation.demo(home/'reconciliation')
    report={'schema':'keel.muse.demo.v1','status':'REHEARSED','synthetic':True,'components':components,
        'rendered_browser':'NOT_RUN','external_tlc_model_check':'NOT_RUN',
        'boundary':'Private synthetic fixtures, actual local files and SQLite stores, and injected native observations. No live account, model-quality or browser execution claim.',
        'execution_authorized':False,'production_deployed':False,'real_local_model_inference':'NOT_RUN',
        'actual_native_browser':'NOT_RUN','account_muse_integration':'NOT_VERIFIED',
        'live_repository_integration':'NOT_VERIFIED','real_model_calls':0,'canonical_writes':0,
        'real_browser_actions':0,'external_network_calls':0,'competitive_superiority_established':False}
    atomic_json(home/'demo.json',report)
    return report
