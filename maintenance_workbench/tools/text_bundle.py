#!/usr/bin/env python3
"""Strict plaintext source transfer. Verify all bytes before extracting; never eval.

Hashes detect byte mismatch, not publisher identity. Destination must be NEW.
The optional build command reads a declared PACKAGE_FILES.json allowlist.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys

MAGIC=b"KEEL-MAINTENANCE-TEXT/1\n"
MAX_TOTAL=32*1024*1024
MAX_FILE=1024*1024

class BundleError(ValueError):pass

def need(test,msg):
    if not test:raise BundleError(msg)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
def parse(raw):
    def pairs(items):
        result={}
        for k,v in items:
            need(k not in result,"duplicate JSON key");result[k]=v
        return result
    def reject(_):raise BundleError("non-finite number")
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=reject)
    except (ValueError,UnicodeError,RecursionError) as exc:raise BundleError("invalid header JSON") from exc

def safe_path(name):
    need(type(name)is str and 0<len(name)<=240,"invalid path")
    need(not name.startswith("/") and "\\" not in name and ":" not in name,"unsafe path")
    need(all(p not in ("", ".", "..") and re.fullmatch(r"[A-Za-z0-9_.-]+",p) and not p.endswith(".") for p in name.split("/")),"unsafe component")

def secure_read(path,limit):
    need(os.name=="posix" and hasattr(os,"O_NOFOLLOW"),"POSIX required")
    path=Path(os.path.abspath(path));fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nextfd
        filefd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            before=os.fstat(filefd)
            need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=limit,"unsafe/oversized file")
            with os.fdopen(filefd,"rb",closefd=False) as stream:raw=stream.read(limit+1)
            after=os.fstat(filefd)
            need(len(raw)<=limit and (before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns),"file changed or oversized")
            return raw
        finally:os.close(filefd)
    finally:os.close(fd)

def encode(files):
    need(type(files)is dict and 0<len(files)<=1000,"invalid count")
    rows=[];seen=set();total=0
    for name,raw in sorted(files.items()):
        safe_path(name);need(name.casefold() not in seen,"duplicate/case collision");seen.add(name.casefold())
        need(type(raw)is bytes and len(raw)<=MAX_FILE,"invalid file bytes");raw.decode("utf-8")
        total+=len(raw);need(total<=MAX_TOTAL,"total exceeded")
        rows.append({"path":name,"bytes":len(raw),"sha256":sha(raw)})
    header={"schema_version":1,"files":rows,"tree_sha256":sha(canonical(rows))}
    out=io.BytesIO();out.write(MAGIC);out.write(canonical(header)+b"\n")
    for row in rows:
        out.write(b"FILE "+canonical(row)+b"\n");out.write(files[row["path"]]);out.write(b"\n")
    out.write(b"END\n");raw=out.getvalue();decode(raw);return raw

def decode(raw):
    need(type(raw)is bytes and len(raw)<=MAX_TOTAL+2*1024*1024,"bundle exceeds limit")
    stream=io.BytesIO(raw);need(stream.readline()==MAGIC,"unsupported bundle format")
    header=parse(stream.readline(1024*1024))
    need(type(header)is dict and set(header)=={"schema_version","files","tree_sha256"},"invalid bundle header")
    need(type(header["schema_version"])is int and header["schema_version"]==1,"unsupported version")
    rows=header["files"];need(type(rows)is list and 0<len(rows)<=1000,"invalid file count")
    need(sha(canonical(rows))==header["tree_sha256"],"manifest hash mismatch")
    files={};seen=set();total=0
    for row in rows:
        need(type(row)is dict and set(row)=={"path","bytes","sha256"},"invalid entry")
        name=row["path"];safe_path(name)
        need(name.casefold() not in seen,"duplicate or case collision");seen.add(name.casefold())
        need(type(row["bytes"])is int and 0<=row["bytes"]<=MAX_FILE,"invalid byte count")
        need(type(row["sha256"])is str and re.fullmatch(r"[0-9a-f]{64}",row["sha256"]),"invalid SHA-256")
        total+=row["bytes"];need(total<=MAX_TOTAL,"total limit")
        need(stream.readline(4096)==b"FILE "+canonical(row)+b"\n","entry framing mismatch")
        content=stream.read(row["bytes"])
        need(len(content)==row["bytes"] and sha(content)==row["sha256"],"file hash mismatch")
        content.decode("utf-8");need(stream.read(1)==b"\n","missing separator");files[name]=content
    need(stream.read()==b"END\n","trailing, missing, or duplicated bundle data")
    folded={p.casefold() for p in files}
    for name in files:
        parts=name.casefold().split("/")
        need(not any("/".join(parts[:i]) in folded for i in range(1,len(parts))),"file/directory collision")
    return files,header

def extract(raw,destination):
    files,header=decode(raw) # All validation happens before directory creation.
    need(os.name=="posix" and hasattr(os,"O_NOFOLLOW"),"POSIX required")
    out=Path(os.path.abspath(destination));safe_path(out.name)
    fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in out.parts[1:-1]:
            newfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=newfd
        os.mkdir(out.name,0o700,dir_fd=fd)
    finally:os.close(fd)
    # New private tree; concurrent mutation by the same OS principal is excluded.
    for name,content in sorted(files.items()):
        dest=out/name;dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        filefd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(filefd,"wb") as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
    return header

def build(root):
    root=Path(root)
    paths=parse(secure_read(root/"PACKAGE_FILES.json",MAX_FILE))
    need(type(paths)is list and paths and all(type(p)is str for p in paths),"package allowlist required")
    need(len(paths)==len(set(paths)),"duplicate package path")
    for p in paths:safe_path(p)
    return encode({p:secure_read(root/p,MAX_FILE) for p in paths})

def verify_tree(raw,destination):
    files,header=decode(raw)
    root=Path(destination)
    observed=list(root.rglob("*"))
    need(not any(p.is_symlink() for p in observed),"symlink in restored tree")
    actual={str(p.relative_to(root)) for p in observed if not p.is_dir()}
    need(actual==set(files),"restored tree has missing or unexpected files")
    for name,content in files.items():
        need(secure_read(root/name,MAX_FILE)==content,"restored file mismatch: "+name)
    return header

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("extract");p.add_argument("bundle");p.add_argument("destination")
    p=sub.add_parser("verify");p.add_argument("bundle")
    p=sub.add_parser("verify-tree");p.add_argument("bundle");p.add_argument("destination")
    p=sub.add_parser("build");p.add_argument("source");p.add_argument("output")
    a=parser.parse_args()
    try:
        if a.command=="build":
            raw=build(a.source)
            with open(a.output,"xb") as stream:stream.write(raw)
            print(json.dumps({"status":"BUILT","bytes":len(raw),"sha256":sha(raw)}));return 0
        raw=secure_read(a.bundle,MAX_TOTAL+2*1024*1024)
        if a.command=="extract":header=extract(raw,a.destination)
        elif a.command=="verify-tree":header=verify_tree(raw,a.destination)
        else:_,header=decode(raw)
        print(json.dumps({"status":"HASHES_MATCH","files":len(header["files"]),"tree_sha256":header["tree_sha256"],"publisher_identity":"NOT_VERIFIED"}));return 0
    except (ValueError,OSError,UnicodeError,TypeError,KeyError) as exc:
        print(json.dumps({"status":"ERROR","error":type(exc).__name__,"message":str(exc)[:300]}));return 2

if __name__=="__main__":raise SystemExit(main())
