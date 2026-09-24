"""Stdlib-only reviewed tests with network/process execution denied.

This in-process guard is not a security sandbox for arbitrary malicious code.
New tests are isolated with temporary fixtures. No broad legacy discovery.
"""
import argparse
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent


def guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind",
                 "socket.sendto", "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        raise PermissionError("OFFLINE TEST GUARD: network and process execution disabled")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor",type=Path,help="isolated executor copy with these four patches applied")
    parser.add_argument("--results",type=Path,default=HERE/"evidence"/"current")
    args=parser.parse_args()
    args.results.mkdir(parents=True,exist_ok=True)
    if args.executor:
        os.environ["KEEL_TEST_EXECUTOR"]=str(args.executor.resolve())
    for path in HERE.rglob("*.py"):
        ast.parse(path.read_text(),filename=str(path))
    sys.addaudithook(guard)
    suite=unittest.TestSuite();loader=unittest.TestLoader()
    for path in sorted((HERE/"tests").glob("test_*.py")):
        if path.name=="test_engine_patches.py" and not args.executor:
            continue
        spec=importlib.util.spec_from_file_location(path.stem,path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    # Meaningful neighboring regressions supplied in the recovered source.
    neighboring=0
    if args.executor:
        path=args.executor/"tests"/"test_queue_io.py"
        if path.is_file():
            spec=importlib.util.spec_from_file_location("prior_queue_io",path)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            old=loader.loadTestsFromModule(module);neighboring=old.countTestCases();suite.addTests(old)
    stream=io.StringIO();started=time.monotonic()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    (args.results/"tests.log").write_text(stream.getvalue())
    summary={"tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
             "skipped":len(result.skipped),"passed":result.wasSuccessful(),"neighboring_queue_tests":neighboring,
             "seconds":round(time.monotonic()-started,3),"python":sys.version.split()[0],
             "scope":"LOCAL_REVIEW_ONLY", "production_deployed":False,
             "engine_patch_tests_executed":bool(args.executor),"live_provider_tests":"NOT_RUN",
             "network_and_subprocess_guard":"enabled", "third_party_dependencies":[]}
    (args.results/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))
    if not result.wasSuccessful():
        print(stream.getvalue())
    return not result.wasSuccessful()


if __name__=="__main__":
    raise SystemExit(main())
