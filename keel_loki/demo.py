"""Offline integrated rehearsal of all eight capability extensions."""
from pathlib import Path
import os
from .common import atomic_json


def run_demo(home):
    from . import browser,temporal,questions,lab,skills,research,routing,calibration,modelcheck,recovery,retrieval,integration
    home=Path(home).absolute()
    from tools.bench_inventory import directory_fd
    parent=directory_fd(home.parent)
    try:os.mkdir(Path('/proc/self/fd')/str(parent)/home.name,0o700)
    finally:os.close(parent)
    reports={
        'forms':browser.run_fixture(home/'forms'),
        'memory':temporal.demo(home/'memory'),
        'questions':questions.demo(),
        'lab':lab.demo(),
        'skills':skills.demo(home/'skills'),
        'research':research.demo(),
        'routing':routing.demo(),
        'calibration':calibration.demo(),
        'formal':modelcheck.demo(),
        'recovery':recovery.demo(home/'recovery'),
        'retrieval':retrieval.demo(),
        'integration':integration.demo(home/'integration')}
    result={'schema':'keel.loki.demo.v1','status':'REHEARSED','synthetic':True,
        'components':reports,'real_model_calls':0,'rendered_browser':'NOT_RUN','canonical_writes':0,
        'external_tlc_model_check':'NOT_RUN','execution_authorized':False,'production_deployed':False,
        'boundary':'Fixture data, injected model responses and Python finite-model exploration. No live host integration or quality claim.'}
    atomic_json(home/'demo.json',result)
    return result
