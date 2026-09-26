"""Content-bound candidate staging. No apply-to-live operation exists."""
from __future__ import annotations
import difflib
from .contracts import keys, version, require, text, hexdigest, digest, sha
from .safeio import relative, MAX_FILE
from .snapshot import Snapshot, make_snapshot


def stage(base: Snapshot, proposal: dict) -> tuple[Snapshot,dict,str]:
    base.validate()
    keys(proposal,{"schema_version","workspace","base_snapshot","reason","changes"});version(proposal)
    require(proposal["workspace"]==base.workspace,"workspace mismatch")
    require(hexdigest(proposal["base_snapshot"])==base.id,"stale base snapshot")
    text(proposal["reason"],"reason",2000)
    require(type(proposal["changes"]) is list and 0<len(proposal["changes"])<=30,"bounded nonempty changes required")
    contents=dict(base.contents); seen=set();diff=[];summary=[]
    for item in proposal["changes"]:
        keys(item,{"path","expected_sha256","replacement"});p=relative(item["path"])
        require(p in contents and p not in seen,"unknown or repeated change path");seen.add(p)
        for protected in base.config["protected_paths"]:
            require(not (p==protected or (protected.endswith("/") and p.startswith(protected))),"protected path cannot be changed")
        require(hexdigest(item["expected_sha256"])==sha(contents[p]),"base file hash mismatch")
        require(type(item["replacement"]) is str,"replacement text required")
        raw=item["replacement"].encode("utf-8")
        require(len(raw)<=MAX_FILE,"replacement exceeds limit")
        require(raw!=contents[p],"no-op replacement")
        diff.extend(difflib.unified_diff(contents[p].decode().splitlines(True),raw.decode().splitlines(True),
                                         fromfile="a/"+p,tofile="b/"+p))
        summary.append({"path":p,"before_sha256":sha(contents[p]),"after_sha256":sha(raw)})
        contents[p]=raw
    candidate=make_snapshot(base.config,contents)
    return candidate,{"schema_version":1,"kind":"keel-maint.staging-result","workspace":base.workspace,
                      "base_snapshot":base.id,"candidate_snapshot":candidate.id,
                      "proposal_sha256":digest(proposal),"changes":summary,
                      "status":"CANDIDATE_ONLY","authorization":"NONE","live_files_written":0},"".join(diff)
