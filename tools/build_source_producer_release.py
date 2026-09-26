#!/usr/bin/env python3
"""Build a verified additive-only source-producer transfer for a Keel0.7 base."""
import argparse
import ast
import base64
import hashlib
import io
import json
import lzma
from pathlib import Path
import sys
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.source_producer_inventory import files as source_files, inventory
from tools.transfer_reader import decode_transfer

VERSION = "0.8.0-review.1"


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+"\n").encode()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _load(path):
    return json.loads(Path(path).read_text())


def _new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("base-archive", "checks", "demo", "host", "out"):
        parser.add_argument("--"+name,required=True)
    args=parser.parse_args(argv)
    checks, demo, out = map(lambda value:Path(value).absolute(), (args.checks,args.demo,args.out))
    summary=_load(checks/"summary.json")
    rehearsal=_load(demo/"source-producer-rehearsal.json")
    if summary["status"]!="PASS" or rehearsal["status"]!="PASS" or not all(row["passed"] for row in rehearsal["checks"]):
        raise ValueError("passing guarded suites and source producer rehearsal required")
    observed=inventory(ROOT)
    if observed["source_sha256"]!=rehearsal["source_inventory"]["source_sha256"]:
        raise ValueError("source changed since rehearsal")
    test_binding=_load(checks/'source-inventory.json')
    if (test_binding['status']!='PASS' or test_binding['before_sha256']!=observed['source_sha256']
            or test_binding['after_sha256']!=observed['source_sha256']):
        raise ValueError('guarded test results do not bind the current source')
    expected_outputs={'summary.json','main/junit.xml','main/pytest.log','maintenance/tests.json','maintenance/tests.log'}
    if set(test_binding['output_sha256'])!=expected_outputs or any(
            _sha((checks/name).read_bytes())!=digest for name,digest in test_binding['output_sha256'].items()):
        raise ValueError('guarded test evidence changed')
    with zipfile.ZipFile(args.base_archive) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)):
            raise ValueError("duplicate base archive members")
        for name in names:
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts or not (ROOT/name).is_file():
                raise ValueError("invalid or missing baseline file")
            if (ROOT/name).read_bytes()!=archive.read(name):
                raise ValueError("existing base changed: "+name)
        base_names=set(names)
        dependencies={name:_sha(archive.read(name)) for name in names if name.endswith('.py') and (
            name.split('/')[0] in {'keel_agent','keel_flow','keel_assurance','keel_trust','keel_workflow','keel_local'}
            or name.startswith('maintenance_workbench/keel_maint/')
            or name in {'tools/make_flow_demo.py','tools/transfer_reader.py','tools/source_inventory.py',
                        'tools/run_release_checks.py','tools/run_tests.py','tools/test_guard/sitecustomize.py',
                        'maintenance_workbench/tools/run_tests.py'})}
        for name in ('roles.json','keel.config.json'):
            dependencies[name]=_sha(archive.read(name))
    files={name:(ROOT/name).read_bytes() for name in source_files(ROOT)}
    if set(files)&base_names:
        raise ValueError("additive payload contains existing base files")
    for name, body in files.items():
        body.decode('utf-8')
        if name.endswith('.py'):
            ast.parse(body,filename=name,feature_version=(3,10))
    evidence_files={
        'audit/source-producers/validation.json':_json(summary),
        'audit/source-producers/test-source-binding.json':_json(test_binding),
        'audit/source-producers/rehearsal.json':_json(rehearsal),
        'audit/source-producers/host-readiness.json':Path(args.host).read_bytes(),
        'audit/source-producers/synthetic-observations.json':(demo/'synthetic-observations.json').read_bytes(),
        'audit/source-producers/main-junit.xml':(checks/'main/junit.xml').read_bytes(),
        'audit/source-producers/main-pytest.log':(checks/'main/pytest.log').read_bytes(),
        'audit/source-producers/maintenance-tests.json':(checks/'maintenance/tests.json').read_bytes(),
        'audit/source-producers/maintenance-tests.log':(checks/'maintenance/tests.log').read_bytes(),
    }
    files.update(evidence_files)
    manifest={"schema":"keel.additive_patch.v1","version":VERSION,"base_version":"0.7.0-review.1",
              "base_archive_sha256":_sha(Path(args.base_archive).read_bytes()),"base_files":dependencies,
              "files":{name:_sha(body) for name,body in sorted(files.items())},
              "execution_authorized":False,"production_deployed":False}
    files['keel_sources/PATCH_MANIFEST.json']=_json(manifest)
    # Same bounded text format/decoder as the already verified0.7 transfer.
    stream=io.BytesIO();stream.write(b'KEEL_TEXT_BUNDLE_V1\n')
    stream.write(json.dumps({'files':len(files),'scope':'additive source producers; requires verified0.7'}).encode()+b'\n')
    for name,body in sorted(files.items()):
        info={'path':name,'bytes':len(body),'sha256':_sha(body),'executable':False}
        stream.write(b'FILE '+json.dumps(info,sort_keys=True).encode()+b'\n'+body+b'\n')
    stream.write(b'END\n');raw=stream.getvalue()
    encoded=base64.b64encode(lzma.compress(raw,preset=6)).decode()
    if {name:entry[0] for name,entry in decode_transfer(encoded,len(raw),_sha(raw),len(files)).items()}!=files:
        raise ValueError("transfer round-trip failed")
    reader=(ROOT/'tools/transfer_reader.py').read_text().replace(
        'Read docs/SELF_HOSTED_HANDOFF.md and docs/SELF_HOSTED.md',
        'Read docs/SOURCE_PRODUCERS.md; verify additive install against the existing0.7 tree')
    note='''#!/usr/bin/env python3
# Keel0.8 SOURCE PRODUCERS — ADDITIVE PATCH, REQUIRES THE EXISTING0.7 TREE
# Verify: python3 -B Keel_0.8.0_Transfer.txt --verify-only
# Extract: python3 -B Keel_0.8.0_Transfer.txt --out /new/isolated/keel-source-producers
# Read docs/SOURCE_PRODUCERS.md, then use tools/install_source_producers.py.
# No existing source file is replaced; no record, approval or deployment is authorized.
# Synthetic source and approval examples are test fixtures, never live evidence.
'''
    footer=(f'\nSOURCE_BYTES = {len(raw)}\nSOURCE_SHA256 = {_sha(raw)!r}\nSOURCE_FILES = {len(files)}\n'
            f'RELEASE_VERSION = {VERSION!r}\nPAYLOAD_B64 = """\n'+'\n'.join(textwrap.wrap(encoded,100))+
            '\n"""\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    transfer=(note+reader+footer).encode()
    compile(transfer,'Keel_0.8.0_Transfer.txt','exec')
    out.mkdir(parents=True,exist_ok=False)
    transfer_path=out/'Keel_0.8.0_Transfer.txt';_new(transfer_path,transfer)
    archive_path=out/'Keel_0.8.0_Source_Producers.zip'
    with zipfile.ZipFile(archive_path,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,body in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0));info.external_attr=(0o100644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,body,compresslevel=9)
    with zipfile.ZipFile(archive_path) as archive:
        if set(archive.namelist())!=set(files) or any(archive.read(name)!=body for name,body in files.items()):
            raise ValueError("archive round-trip failed")
    handoff=(ROOT/'docs/SOURCE_PRODUCER_HANDOFF.md').read_bytes()+b'\n\n'+(ROOT/'docs/SOURCE_PRODUCERS.md').read_bytes()
    handoff_path=out/'Keel_0.8.0_Handoff.txt';_new(handoff_path,handoff)
    evidence={"schema":"keel.source_producer_release.v1","version":VERSION,"release_status":"INTEGRATION_CANDIDATE",
              "additive_only":True,"original_archive_files_verified_unchanged":len(base_names),
              "patch_files":len(files),"base_dependency_files":len(dependencies),
              "source_sha256":observed['source_sha256'],"tests":summary,
              "rehearsal":{key:rehearsal[key] for key in ('status','checks_passed','checks_total','synthetic','model_calls','http_calls','browser_actions','canonical_writes','execution_authorized')},
              "transfer":{"file":str(transfer_path),'bytes':len(transfer),'sha256':_sha(transfer),'payload_sha256':_sha(raw)},
              "archive":{"file":str(archive_path),'bytes':archive_path.stat().st_size,'sha256':_sha(archive_path.read_bytes())},
              "handoff_sha256":_sha(handoff),"paid_api_required":False,"live_source_records_populated":False,
              "real_model_inference":"NOT_RUN","rendered_browser":"NOT_RUN",
              "production_deployed":False,"execution_authorized":False}
    _new(out/'Keel_0.8.0_Release_Evidence.json',_json(evidence))
    print(json.dumps({'status':'PACKAGED','patch_files':len(files),'base_files_unchanged':len(base_names),'out':str(out)},indent=2))


if __name__=='__main__':
    main()
