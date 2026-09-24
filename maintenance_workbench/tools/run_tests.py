#!/usr/bin/env python3
"""Run only this delivered package's tests; zero tests and skips are not green."""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import platform
import socket
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from keel_maint.safeio import write_new_file

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--out",required=True);args=p.parse_args()
    out=Path(args.out);out.mkdir(mode=0o700,parents=False,exist_ok=False)
    suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"),top_level_dir=str(ROOT))
    stream=io.StringIO()
    def no_network(*args,**kwargs):raise AssertionError("unexpected network call in local test process")
    with patch.object(socket,"socket",no_network),patch.object(socket,"create_connection",no_network),patch.object(socket,"getaddrinfo",no_network):
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    passed=(result.testsRun>0 and result.wasSuccessful() and not result.skipped and not result.expectedFailures)
    report={"schema_version":1,"status":"PASS" if passed else "FAIL","tests_run":result.testsRun,
            "failures":len(result.failures),"errors":len(result.errors),
            "failed_test_ids":[t.id() for t,_ in result.failures],"error_test_ids":[t.id() for t,_ in result.errors],"skipped":len(result.skipped),
            "expected_failures":len(result.expectedFailures),"unexpected_successes":len(result.unexpectedSuccesses),
            "python":platform.python_version(),"sqlite":sqlite3.sqlite_version,"platform":platform.system(),
            "network_guard":"host_socket_APIs_blocked; trusted_static_parser_subprocess_only",
            "scope":"maintenance_workbench_own_tests_not_KEEL_private_executor",
            "os_sandbox_validation":"NOT_RUN","production_deployed":False}
    write_new_file(out/"tests.log",stream.getvalue().encode())
    write_new_file(out/"tests.json",json.dumps(report,sort_keys=True,indent=2).encode()+b"\n")
    print(stream.getvalue());print(json.dumps(report,sort_keys=True));return 0 if passed else 1

if __name__=="__main__":raise SystemExit(main())
