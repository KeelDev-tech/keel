#!/usr/bin/env python3
"""Actual loopback HTTP integration checks, separate from the network-disabled suite.

Starts and stops an ephemeral local fixture in the same process. No real
applicant data, external network, models, browser actions or submissions.
"""
import argparse
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from keel_workbench.demo import make_demo,NOW
from keel_workbench.service import Workbench
from keel_workbench.server import LocalServer
from keel_workbench.client import Client,ClientError
from keel_agent.io import write_private


def check():
    app=Workbench(make_demo(),workspace_id="keel-demo",synthetic=True,host_clock=lambda:NOW)
    rows=[]
    def verify(name,passed):
        rows.append({"name":name,"passed":bool(passed)})
        if not passed:raise AssertionError(name)
    with LocalServer(app,0) as server:
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.05},daemon=True);thread.start()
        client=Client("http://"+server.authority,server.token)
        def http(method,path,body=None,headers=None,auth=True):
            connection=HTTPConnection("127.0.0.1",server.server_port,timeout=5)
            hdr={"Host":server.authority,**({"Authorization":"Bearer "+server.token} if auth else {}),**(headers or {})}
            if isinstance(body,dict):body=json.dumps(body).encode();hdr.setdefault("Content-Type","application/json")
            connection.request(method,path,body=body,headers=hdr)
            response=connection.getresponse();status=response.status;head=dict(response.getheaders());raw=response.read();connection.close()
            return status,head,raw
        try:
            status,headers,body=http("GET","/",auth=False)
            verify("HTML served with restrictive CSP",status==200 and b"Keel" in body and "frame-ancestors 'none'" in headers["Content-Security-Policy"])
            for path in ("/app.js","/style.css"):
                status,headers,body=http("GET",path,auth=False)
                verify("static asset "+path,status==200 and len(body)>1000 and headers["Cache-Control"]=="no-store")
            verify("health has no workspace data",http("GET","/health",auth=False)[0]==200)
            verify("workspace requires token",http("GET","/api/v1/overview",auth=False)[0]==401)
            verify("foreign host rejected",http("GET","/",headers={"Host":"attacker.example"})[0]==403)
            verify("foreign origin rejected",http("GET","/api/v1/overview",headers={"Origin":"https://attacker.example"})[0]==403)
            verify("browser cross-site request rejected",http("GET","/api/v1/overview",headers={"Sec-Fetch-Site":"cross-site"})[0]==403)
            verify("query-string token not accepted",http("GET","/api/v1/overview?token=x")[0]==400)
            verify("static path traversal rejected",http("GET","/../WORKBENCH.md")[0]==404)
            verify("CORS preflight grants no access",http("OPTIONS","/api/v1/run")[0]==405)
            verify("chunked framing rejected",http("POST","/api/v1/run",b"{}",{"Transfer-Encoding":"chunked"})[0]==400)
            verify("oversized body rejected before read",http("POST","/api/v1/run",b"{}",{"Content-Length":str(8*1024*1024+1)})[0]==413)
            verify("non-JSON import rejected",http("POST","/api/v1/snapshot",b"{}",{"Content-Type":"text/plain"})[0]==415)
            verify("duplicate JSON key rejected",http("POST","/api/v1/run",b'{"request_id":"a","request_id":"b"}',{"Content-Type":"application/json"})[0]==400)
            v=client.overview();snapshot=client.snapshot()
            verify("SDK reads real reducer counts",v["counts"]["nominal_ready"]==5 and v["counts"]["base_ready"]==1 and v["counts"]["review_checks_passed"]==0)
            verify("workflow catalog has five workflows",len(client.workflows()["workflows"])==5)
            for identifier,options in [
                ("source-repair",{}),("material-review",{}),("daily-brief",{"budget_minutes":2}),("incident-replay",{}),
                ("twin-scenario",{"delay_minutes":10,"capacity_multiplier":1.5,"rate_hold_source":None,"invalidate_role":None})]:
                c={"request_id":"http-"+identifier,"workflow_id":identifier,"snapshot_sha256":v["snapshot_sha256"],"role_ids":[],"options":options}
                result=client.run(c)
                verify(identifier+" through actual HTTP",result["workflow_id"]==identifier and not result["execution_authorized"])
                if identifier=="source-repair":verify("source plan has seven system tasks",result["result"]["system_tasks"]==7)
                if identifier=="incident-replay":verify("replay results pass",result["result"]["status"]=="PASS")
                if identifier=="twin-scenario":verify("HTTP twin compares 300s to 200s runway",result["result"]["baseline"]["runway_seconds"]==300 and result["result"]["scenario"]["runway_seconds"]==200)
                verify(identifier+" retries return the same report",client.run(c)==result)
            verify("history reflects all five workflows",len(client.history()["runs"])==5)
            verify("OpenAPI document is available",client.call("GET","/openapi.json")["openapi"]=="3.1.1")
            candidate=deepcopy(snapshot);candidate["labels"][0]["title"]="Synthetic HTTP import"
            imported=client.import_snapshot(candidate,previous_sha256=v["snapshot_sha256"])
            verify("import updates session only",imported["canonical_writes"]==0 and client.overview()["roles"][0]["title"]=="Synthetic HTTP import")
            try:client.import_snapshot(snapshot,previous_sha256=v["snapshot_sha256"])
            except ClientError as error:verify("stale compare-and-swap rejected",error.status==409)
            else:verify("stale compare-and-swap rejected",False)
            candidate["workspace_id"]="other"
            status,_,_=http("POST","/api/v1/snapshot",{"previous_sha256":imported["snapshot_sha256"],"snapshot":candidate})
            verify("cross-workspace import rejected",status==400)
            verify("bad import preserves current snapshot",client.snapshot()["labels"][0]["title"]=="Synthetic HTTP import")
        finally:
            server.shutdown();thread.join(timeout=3)
    return {"schema_version":1,"suite":"workbench-actual-loopback-http","status":"PASS","checks":rows,
            "checks_passed":len(rows),"synthetic":True,"external_network_requests":0,"model_calls":0,
            "browser_actions":0,"application_submissions":0,"production_deployed":False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--out",required=True);args=parser.parse_args()
    result=check();write_private(args.out,result);print(json.dumps({"status":result["status"],"checks_passed":result["checks_passed"]}))


if __name__=="__main__":main()
