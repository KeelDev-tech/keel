"""Scoped literal full-text retrieval. Source content never becomes instructions."""
from __future__ import annotations
import re
import sqlite3
from .contracts import require, text, integer, digest, canonical, strict_json
from .snapshot import Snapshot

class AnalysisCache:
    """Process-local exact-result cache for INFORMATIONAL context/impact only.

    Never caches authorization, user facts, runtime checks, or provider outcomes.
    No persistent cache database and no independent authority are introduced.
    """
    def __init__(self, maximum: int = 64):
        integer(maximum,maximum=256);require(maximum>0,"positive cache limit required")
        self.maximum=maximum;self._items={}
    def get(self, key: str):
        value=self._items.get(key)
        return strict_json(value) if value is not None else None
    def put(self, key: str, value: dict):
        if key not in self._items and len(self._items)>=self.maximum:
            del self._items[next(iter(self._items))]
        self._items[key]=canonical(value)


def search(snapshot: Snapshot, *, workspace: str, paths: list[str], query: str,
           limit: int = 5, max_chars: int = 8000, mode: str = "auto") -> dict:
    snapshot.validate()
    require(workspace==snapshot.workspace,"workspace mismatch")
    require(type(paths) is list and paths and len(paths)<=1000 and all(type(p) is str for p in paths),
            "explicit path scope required")
    require(set(paths)<=set(snapshot.contents),"scope outside snapshot")
    text(query,"query",512);integer(limit,maximum=20);integer(max_chars,maximum=30000)
    require(limit>0 and max_chars>=100,"positive retrieval budgets required")
    require(mode in ("auto","tokens","fts5"),"unsupported retrieval mode")
    tokens=sorted(set(re.findall(r"[^\W_]+",query.casefold(),flags=re.UNICODE)))[:32]
    require(bool(tokens),"query has no searchable tokens")
    chunks=[]
    for path in sorted(set(paths)):
        lines=snapshot.contents[path].decode("utf-8").splitlines()
        for start in range(0,len(lines),40):
            content="\n".join(lines[start:start+40])
            # Long lines are not silently cut and mislabeled as complete evidence.
            if len(content)>12000: content=content[:12000]; truncated=True
            else: truncated=False
            chunks.append({"path":path,"start_line":start+1,"end_line":min(start+40,len(lines)),
                           "content":content,"excerpt_truncated":truncated})
    backend="tokens"; ranked=[]
    if mode!="tokens":
        con=sqlite3.connect(":memory:")
        try:
            try: con.execute("CREATE VIRTUAL TABLE snippets USING fts5(path, content)")
            except sqlite3.OperationalError:
                require(mode!="fts5","SQLite FTS5 unavailable")
            else:
                backend="sqlite_fts5_bm25"
                con.executemany("INSERT INTO snippets(rowid,path,content) VALUES(?,?,?)",
                                [(i+1,c["path"],c["content"]) for i,c in enumerate(chunks)])
                # No raw user FTS expression; every token is a quoted literal.
                match=" OR ".join('"'+t.replace('"','""')+'"' for t in tokens)
                ranked=[(row[0]-1,row[1]) for row in con.execute(
                    "SELECT rowid,bm25(snippets,2.0,1.0) AS s FROM snippets WHERE snippets MATCH ? ORDER BY s,rowid",
                    (match,))]
        finally: con.close()
    if backend=="tokens":
        for i,c in enumerate(chunks):
            words=set(re.findall(r"[^\W_]+",(c["path"]+" "+c["content"]).casefold()))
            score=len(set(tokens)&words)
            if score: ranked.append((i,-score))
        ranked.sort(key=lambda r:(r[1],chunks[r[0]]["path"],chunks[r[0]]["start_line"]))
    results=[];used=0
    rows={r["path"]:r for r in snapshot.manifest["files"]}
    for i,_ in ranked:
        if len(results)>=limit:break
        c=dict(chunks[i]);remaining=max_chars-used
        if remaining<=0:break
        if len(c["content"])>remaining:
            c["content"]=c["content"][:remaining];c["excerpt_truncated"]=True
        used+=len(c["content"])
        c.update(source_sha256=rows[c["path"]]["sha256"],source_status="UNTRUSTED_SOURCE_COPY",
                 authority="NONE",snapshot_id=snapshot.id)
        results.append(c)
    return {"schema_version":1,"workspace":workspace,"snapshot_id":snapshot.id,
            "scope":sorted(set(paths)),"backend":backend,"results":results,"characters":used,
            "authorization":"NONE","scope_enforcement":"host_must_authenticate_caller"}
