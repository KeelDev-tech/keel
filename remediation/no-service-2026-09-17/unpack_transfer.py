"""Reconstruct a complete Keel plain-text transfer into a NEW directory.

Validates every length/digest before writing; does not execute or deploy code.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def unpack(source, destination):
    raw=Path(source).read_bytes()
    marker=b"\nKEEL_LOCAL_TRANSFER_PAYLOAD_V1\n"
    cursor=raw.index(marker)+len(marker)
    files=[];seen=set()
    while True:
        end=raw.index(b"\n",cursor);line=raw[cursor:end];cursor=end+1
        if line==b"KEEL_LOCAL_TRANSFER_END":
            if raw[cursor:]:raise ValueError("unexpected trailing bytes")
            break
        record=json.loads(line)
        name=record["path"];path=PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or "\\" in name or
                name != str(path) or name in seen):raise ValueError("unsafe path")
        size=record["size_bytes"]
        if type(size) is not int or not 0<=size<=50_000_000:raise ValueError("invalid size")
        content=raw[cursor:cursor+size];cursor+=size
        if raw[cursor:cursor+1]!=b"\n":raise ValueError("missing separator")
        cursor+=1
        if len(content)!=size or hashlib.sha256(content).hexdigest()!=record["sha256"]:
            raise ValueError("incomplete or changed file: "+name)
        files.append((path,content));seen.add(name)
    destination=Path(destination)
    destination.mkdir(parents=True,exist_ok=False)
    for path,content in files:
        target=destination.joinpath(*path.parts);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content)
    return len(files)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path);parser.add_argument("destination",type=Path)
    args=parser.parse_args()
    print("Reconstructed",unpack(args.source,args.destination),"verified files")
