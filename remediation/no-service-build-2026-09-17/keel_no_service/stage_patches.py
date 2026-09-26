"""Apply only this delivery's five patches into a NEW, isolated source copy.

Exact base hashes are mandatory. A newer live tree must be manually rebased;
this tool never edits the input tree, starts workers, or imports engine code.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def apply_hunks(original, patch_text):
    source = original.splitlines(keepends=True)
    lines = patch_text.splitlines(keepends=True)
    if len(lines) < 3 or not lines[0].startswith("--- a/") or not lines[1].startswith("+++ b/"):
        raise ValueError("unsupported patch header")
    output, cursor, index = [], 0, 2
    while index < len(lines):
        match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n?", lines[index])
        if not match:
            raise ValueError("unsupported hunk header")
        start = int(match[1]) - 1
        if start < cursor:
            raise ValueError("overlapping hunks")
        output.extend(source[cursor:start]); cursor=start; index+=1
        removed=added=0
        while index < len(lines) and not lines[index].startswith("@@ "):
            line=lines[index];marker,content=line[0],line[1:]
            if marker in (" ","-"):
                if cursor >= len(source) or source[cursor] != content:
                    raise ValueError("patch context mismatch")
                cursor+=1;removed+=1
            if marker in (" ","+"):
                output.append(content);added+=1
            if marker not in (" ","+","-"):
                raise ValueError("unsupported patch line")
            index+=1
        if removed != int(match[2] or 1) or added != int(match[4] or 1):
            raise ValueError("hunk counts differ")
    output.extend(source[cursor:])
    return "".join(output).encode("utf-8")


def stage(source, destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("destination must be new and outside the source tree")
    manifest=json.loads((ROOT/"patches"/"manifest.json").read_text())
    changed={}
    for item in manifest["files"]:
        name=item["file"]
        if Path(name).name != name or not name.endswith(".py"):
            raise ValueError("unsafe manifest path")
        path=source/name
        if path.is_symlink() or path.resolve().parent != source:
            raise ValueError("symlink source not allowed")
        data=path.read_bytes()
        if sha(data) != item["base_sha256"]:
            raise ValueError(f"{name}: current source differs; rebase the patch, do not overwrite newer work")
        new=apply_hunks(data.decode(),(ROOT/"patches"/item["patch"]).read_text())
        if sha(new) != item["patched_sha256"]:
            raise ValueError("patched digest differs: "+name)
        changed[name]=new
    shutil.copytree(source,destination,ignore=shutil.ignore_patterns("__pycache__","*.pyc"),symlinks=True)
    for name,data in changed.items():
        (destination/name).write_bytes(data)
    return {"stage":str(destination),"patched_files":sorted(changed),"input_modified":False,
            "production_deployed":False}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-executor",required=True,type=Path)
    parser.add_argument("--destination",required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(stage(args.source_executor,args.destination),indent=2))
