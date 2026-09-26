from datetime import timedelta
from email.message import Message
from io import BytesIO
import json
import pytest
from keel_agent.state import LocalState
from keel_workbench.demo import make_demo,NOW
from keel_workbench.service import Workbench
from keel_workbench.integrations import snapshot_from_agent,bridge_message,read_message,EOF
from keel_workbench.api import APIError,dispatch
from keel_workbench.server import check_request
from keel_workbench.client import Client
from keel_workbench.openapi import specification
from keel_workbench.__main__ import main
from keel_trust.common import digest


def app(): return Workbench(make_demo(),workspace_id="keel-demo",synthetic=True,host_clock=lambda:NOW)


def test_real_localstate_adapter_verifies_signature_and_changes_no_events(tmp_path):
    state=LocalState(tmp_path/"private"/"agent.db","keel-demo")
    d=make_demo();body={k:d[k] for k in ("flow","assurance","trust")}
    env=state.sign_snapshot(body,body["flow"]["source_revision"],1,NOW,NOW+timedelta(seconds=90))
    state.import_snapshot(env,now=NOW);before=state.events()
    wrapped=snapshot_from_agent(state,now=NOW)
    assert not wrapped["synthetic"] and wrapped["flow"]==body["flow"]
    assert state.events()==before and wrapped["revision_sources"] is None
    # Treating fixture records as operational here tests only the envelope route.
    # This fixture never leaves the isolated test directory.
    with pytest.raises(ValueError,match="stale"):snapshot_from_agent(state,now=NOW+timedelta(seconds=91))


def test_json_bridge_and_python_service_share_results():
    a=app();v=bridge_message(a,{"id":"1","method":"GET","path":"/api/v1/overview","body":None})
    assert v["status"]==200 and v["data"]==a.overview()
    c={"request_id":"bridge-1","workflow_id":"source-repair","snapshot_sha256":v["data"]["snapshot_sha256"],"role_ids":[],"options":{}}
    r=bridge_message(a,{"id":"2","method":"POST","path":"/api/v1/run","body":c})
    assert r["data"]==a.run(c)
    c["snapshot_sha256"]="a"*64
    assert bridge_message(a,{"id":"3","method":"POST","path":"/api/v1/run","body":c})["status"]==409


@pytest.mark.parametrize("value",[None,[],{}, {"id":"x","method":"DELETE","path":"/api/v1/snapshot","body":None},
    {"id":"x","method":"GET","path":"/api/v1/overview","body":{}},
    {"id":"x","method":"POST","path":"/api/v1/snapshot","body":{"snapshot":{}}}])
def test_bridge_returns_structured_input_errors(value):
    assert bridge_message(app(),value)["status"]==400


def test_json_line_eof_is_distinct_from_null():
    assert read_message(BytesIO(b"")) is EOF
    assert read_message(BytesIO(b"null\n")) is None
    with pytest.raises(ValueError):read_message(BytesIO(b'{"a":1,"a":2}\n'))
    with pytest.raises(ValueError,match="8 MiB"):read_message(BytesIO(b"x"*(8*1024*1024+1)))


def headers(**override):
    result=Message()
    for k,v in {"Host":"127.0.0.1:8765","Authorization":"Bearer "+"x"*32,**override}.items():result[k]=v
    return result


@pytest.mark.parametrize("changes,status",[
    ({"Host":"attacker.example:8765"},403),({"Host":"localhost:8765"},403),
    ({"Origin":"https://attacker.example"},403),({"Origin":"null"},403),
    ({"Sec-Fetch-Site":"cross-site"},403),({"Authorization":"Bearer wrong"},401),
    ({"Transfer-Encoding":"chunked"},400),
])
def test_local_auth_host_and_origin_boundary(changes,status):
    with pytest.raises(APIError) as error:check_request(headers(**changes),authority="127.0.0.1:8765",token="x"*32)
    assert error.value.status==status


@pytest.mark.parametrize("header,value",[("Host","127.0.0.1:8765"),("Origin","http://127.0.0.1:8765"),("Authorization","Bearer "+"x"*32)])
def test_ambiguous_auth_headers_rejected(header,value):
    h=headers();h[header]=value;h[header]=value
    with pytest.raises(APIError):check_request(h,authority="127.0.0.1:8765",token="x"*32)


def test_same_origin_with_valid_token_is_accepted():
    check_request(headers(Origin="http://127.0.0.1:8765"),authority="127.0.0.1:8765",token="x"*32)


@pytest.mark.parametrize("endpoint",["https://127.0.0.1:8765","http://example.com:8765","http://localhost:8765",
    "http://127.0.0.1:8765/private","http://127.0.0.1:8765/?token=x","http://user@127.0.0.1:8765","http://127.0.0.1"])
def test_sdk_cannot_send_local_token_to_other_destinations(endpoint):
    with pytest.raises(ValueError):Client(endpoint,"x"*32)


def test_openapi_exposes_versioned_routes_and_strict_workflow_options():
    spec=specification();assert spec["openapi"]=="3.1.1"
    assert spec["security"]==[{"localSession":[]}]
    assert set(spec["paths"]["/api/v1/snapshot"])=={"get","post"}
    assert len(spec["components"]["schemas"]["RunRequest"]["oneOf"])==5
    assert all(v["additionalProperties"] is False for v in spec["components"]["schemas"]["RunRequest"]["oneOf"])


def test_cli_single_file_export_and_run(tmp_path):
    out=tmp_path/"demo.json"
    assert main(["demo-export","--out",str(out)])==0
    assert json.loads(out.read_text())["synthetic"]
    assert out.stat().st_mode & 0o777 == 0o600
    assert main(["demo-export","--out",str(out)])==2
    request=tmp_path/"request.json"
    request.write_text(json.dumps({"request_id":"cli-1","workflow_id":"incident-replay","snapshot_sha256":digest(make_demo()),"role_ids":[],"options":{}}))
    result=tmp_path/"result.json"
    assert main(["run","--demo","--request",str(request),"--out",str(result)])==0
    assert json.loads(result.read_text())["result"]["status"]=="PASS"
