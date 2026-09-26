"""Private static-parser subprocess. Does not import or execute target source."""
import ast
import json
import sys

LIMIT=512*1024

def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(3,3))
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    except (ImportError,ValueError,OSError):
        print(json.dumps({"status":"UNAVAILABLE","reason":"parser_resource_limits_unavailable"}))
        return 2
    raw=sys.stdin.buffer.read(LIMIT+1)
    if len(raw)>LIMIT:
        print(json.dumps({"status":"INVALID","error":"size_limit"})); return 1
    try:
        tree=ast.parse(raw.decode("utf-8"))
        imports=[]; symbols=[]; dynamic=False
        for n in ast.walk(tree):
            if isinstance(n,ast.Import):
                imports.extend({"style":"import","module":a.name,"level":0,"names":[]} for a in n.names)
            elif isinstance(n,ast.ImportFrom):
                imports.append({"style":"from","module":n.module or "","level":n.level,"names":[a.name for a in n.names]})
            elif isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                symbols.append({"name":n.name,"line":n.lineno,"kind":type(n).__name__})
            elif isinstance(n,ast.Call):
                f=n.func
                if (isinstance(f,ast.Name) and f.id in ("__import__","eval","exec")) or \
                   (isinstance(f,ast.Attribute) and f.attr in ("import_module","exec_module")):
                    dynamic=True
        if len(imports)>2000 or len(symbols)>5000:
            print(json.dumps({"status":"INVALID","error":"structure_limit"}));return 1
        print(json.dumps({"status":"PARSED","imports":imports,"symbols":symbols,"dynamic_code":dynamic}))
        return 0
    except (SyntaxError,UnicodeError,ValueError,RecursionError,MemoryError) as exc:
        print(json.dumps({"status":"INVALID","error":type(exc).__name__,"line":getattr(exc,"lineno",None)}))
        return 1

if __name__=="__main__": raise SystemExit(main())
