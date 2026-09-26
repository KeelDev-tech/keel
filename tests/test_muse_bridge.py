from copy import deepcopy
import pytest
from keel_muse.bridge import CommandBridge, ReadOperation
from keel_muse.common import MuseError, digest


def setup(handler=None):
    state={'workspace_id':'w','revision':1}
    source=[digest('code')]
    clock=[100]
    calls=[]
    def default(args):
        calls.append(args)
        return {'status':'BLOCKED','reason':'human_decision_missing'}
    bridge=CommandBridge('w',{'inspect':ReadOperation(handler or default,True)},
                         source_provider=lambda:source[0],state_provider=lambda:state,clock=lambda:clock[0])
    request={'schema':'keel.muse.bridge-request.v1','request_id':'r','workspace_id':'w',
             'operation':'inspect','expected_source_sha256':source[0],
             'expected_state_sha256':digest(state),'expires_at':110,'arguments':{}}
    return bridge,request,state,source,clock,calls


def test_recording_blocked_result_never_manufactures_task_success():
    bridge,request,_,_,_,calls=setup()
    result=bridge.invoke(request)
    assert len(calls)==1 and result['status']=='RECORDED'
    assert result['result']['status']=='BLOCKED'
    assert result['task_success_inferred'] is False
    assert result['execution_authorized'] is False


@pytest.mark.parametrize('field,value',[
    ('operation','submit'),('workspace_id','other'),('expected_source_sha256','0'*64),
    ('expected_state_sha256','0'*64),('expires_at',100),('expires_at',3701),
    ('expires_at',True),('arguments',[]),('schema','mcp')])
def test_malformed_or_unbound_request_never_calls_handler(field,value):
    bridge,request,_,_,_,calls=setup()
    request[field]=value
    with pytest.raises(ValueError):bridge.invoke(request)
    assert calls==[]


def test_extra_transport_or_authority_cannot_enter_request():
    bridge,request,_,_,_,calls=setup()
    request['execution_authorized']=True
    with pytest.raises(ValueError):bridge.invoke(request)
    assert not calls


def test_state_reloaded_for_every_read_even_with_same_request_id():
    bridge,request,state,_,_,calls=setup()
    bridge.invoke(request)
    state['revision']=2
    with pytest.raises(MuseError):bridge.invoke(request)
    assert len(calls)==1


@pytest.mark.parametrize('changed',['source','state','late'])
def test_mid_operation_change_quarantines_result(changed):
    bridge,request,state,source,clock,_=setup()
    def mutate(_):
        if changed=='source':source[0]=digest('new-code')
        if changed=='state':state['revision']=2
        if changed=='late':clock[0]=110
        return {'status':'PASS'}
    bridge.operations['inspect']=ReadOperation(mutate,True)
    result=bridge.invoke(request)
    assert result['status']=='QUARANTINED'
    assert result['quarantine_reasons']
    assert result['execution_authorized'] is False


def test_handler_input_mutation_does_not_rewrite_request_pin():
    def mutate(args):args['x']=2;return {'x':2}
    bridge,request,*_=setup(mutate)
    request['arguments']={'x':1}
    before=deepcopy(request)
    result=bridge.invoke(request)
    assert request==before
    assert result['request_sha256']==digest(before)


def test_executable_result_is_rejected():
    bridge,request,*_=setup(lambda _: {'callable':lambda:None})
    with pytest.raises(ValueError):bridge.invoke(request)


@pytest.mark.parametrize('next_time',[99,110])
def test_slow_or_regressing_state_read_never_admits_handler(next_time):
    bridge,request,state,_,clock,calls=setup()
    def delayed():
        clock[0]=next_time
        return state
    bridge.state_provider=delayed
    with pytest.raises(ValueError):bridge.invoke(request)
    assert calls==[]
