"""Explicit-allowlist snapshots, never imports or mutates the source repository."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .contracts import keys, version, identifier, integer, digest, sha, canonical, strict_json, require, hexdigest
from .safeio import relative, read_under, write_tree, MAX_FILE, MAX_TOTAL, MAX_FILES

KINDS={"python", "json", "text"}

def validate_config(config: dict) -> dict:
    keys(config, {"schema_version", "workspace", "files", "protected_paths", "dependencies"})
    version(config); identifier(config["workspace"])
    require(type(config["files"]) is list and 0 < len(config["files"]) <= MAX_FILES, "invalid file list")
    seen=set()
    for row in config["files"]:
        keys(row, {"path", "kind"}); p=relative(row["path"])
        require(p.casefold() not in seen, "duplicate/case-colliding source path"); seen.add(p.casefold())
        require(type(row["kind"]) is str and row["kind"] in KINDS, "invalid source kind")
        require(row["kind"] != "python" or p.endswith(".py"), "python path must end with .py")
    paths={x["path"] for x in config["files"]}
    require(type(config["protected_paths"]) is list, "protected_paths must be a list")
    for p in config["protected_paths"]:
        require(type(p) is str, "protected path must be text")
        relative(p[:-1] if p.endswith("/") else p)
    require(type(config["dependencies"]) is list and len(config["dependencies"]) <= MAX_FILES*10,
            "invalid declared dependencies")
    for pair in config["dependencies"]:
        require(type(pair) is list and len(pair)==2 and all(type(x) is str for x in pair), "dependency pair required")
        require(set(pair) <= paths and pair[0] != pair[1], "dependency outside snapshot or self-edge")
    canonical(config)
    return config

@dataclass(frozen=True)
class Snapshot:
    manifest: dict
    contents: dict[str, bytes]

    @property
    def id(self) -> str: return self.manifest["snapshot_id"]
    @property
    def workspace(self) -> str: return self.manifest["config"]["workspace"]
    @property
    def config(self) -> dict: return self.manifest["config"]

    def validate(self) -> None:
        m=self.manifest
        keys(m, {"schema_version", "kind", "config", "files", "snapshot_id", "consistency"})
        version(m); require(m["kind"]=="keel-maint.snapshot", "wrong snapshot kind")
        require(m["consistency"]=="two_pass_observation_not_atomic", "unsupported consistency claim")
        validate_config(m["config"]); hexdigest(m["snapshot_id"])
        require(digest({k:v for k,v in m.items() if k!="snapshot_id"})==m["snapshot_id"], "manifest digest mismatch")
        require(type(m["files"]) is list and len(m["files"])==len(m["config"]["files"]), "manifest count mismatch")
        expected={r["path"]:r["kind"] for r in m["config"]["files"]}
        require(set(self.contents)==set(expected), "snapshot content set mismatch")
        seen=set(); total=0
        for row in m["files"]:
            keys(row,{"path","kind","sha256","bytes"}); p=relative(row["path"])
            require(p in expected and p not in seen and row["kind"]==expected[p], "invalid manifest row")
            seen.add(p); hexdigest(row["sha256"]); integer(row["bytes"], maximum=MAX_FILE)
            raw=self.contents[p]; require(type(raw) is bytes, "snapshot content must be bytes")
            require(sha(raw)==row["sha256"] and len(raw)==row["bytes"], "snapshot content hash mismatch")
            try: raw.decode("utf-8")
            except UnicodeError as exc: raise ValueError("snapshot must contain UTF-8 text") from exc
            total+=len(raw)
        require(total<=MAX_TOTAL, "snapshot total exceeded")

    def save(self, destination: Path | str) -> None:
        self.validate()
        output={"snapshot.json": canonical(self.manifest)+b"\n"}
        for row in self.manifest["files"]:
            output["objects/"+row["sha256"]]=self.contents[row["path"]]
        write_tree(destination, output)


def make_snapshot(config: dict, contents: dict[str, bytes]) -> Snapshot:
    validate_config(config)
    config=strict_json(canonical(config))
    require(type(contents) is dict and set(contents)=={r["path"] for r in config["files"]}, "snapshot content set mismatch")
    require(all(type(v) is bytes for v in contents.values()), "bytes required")
    contents=dict(contents)
    files=[{"path":r["path"],"kind":r["kind"],"sha256":sha(contents[r["path"]]),
            "bytes":len(contents[r["path"]])} for r in sorted(config["files"],key=lambda r:r["path"])]
    m={"schema_version":1,"kind":"keel-maint.snapshot","config":config,"files":files,
       "consistency":"two_pass_observation_not_atomic"}
    m["snapshot_id"]=digest(m); result=Snapshot(m,contents); result.validate(); return result


def capture(root: Path | str, config: dict) -> Snapshot:
    validate_config(config)
    contents={}; total=0
    for row in config["files"]:
        raw=read_under(root,row["path"])
        total+=len(raw); require(total<=MAX_TOTAL,"snapshot total exceeded")
        contents[row["path"]]=raw
    for path, raw in contents.items():
        require(sha(read_under(root,path))==sha(raw), "source changed during capture: "+path)
    return make_snapshot(config,contents)


def load(root: Path | str) -> Snapshot:
    m=strict_json(read_under(root,"snapshot.json",4*1024*1024))
    keys(m,{"schema_version","kind","config","files","snapshot_id","consistency"}); version(m)
    validate_config(m["config"])
    require(type(m["files"]) is list and len(m["files"])<=MAX_FILES,"invalid manifest file list")
    data={}; total=0
    for row in m["files"]:
        keys(row,{"path","kind","sha256","bytes"}); relative(row["path"]); hexdigest(row["sha256"])
        require(row["path"] not in data,"duplicate manifest path")
        raw=read_under(root,"objects/"+row["sha256"])
        total+=len(raw); require(total<=MAX_TOTAL,"snapshot total exceeded")
        data[row["path"]]=raw
    result=Snapshot(m,data); result.validate(); return result


def changes(base: Snapshot, candidate: Snapshot) -> dict:
    base.validate(); candidate.validate()
    require(base.workspace==candidate.workspace,"workspace mismatch")
    # This version does not permit silently expanding scope during a comparison.
    require(base.config==candidate.config,"configuration changed; separate review required")
    changed=[p for p in sorted(base.contents) if base.contents[p]!=candidate.contents[p]]
    return {"base_snapshot":base.id,"candidate_snapshot":candidate.id,"changed_paths":changed,
            "status":"CHANGED" if changed else "UNCHANGED","authorization":"NONE"}
