"""Run with python3 -B -m keel_workbench. No paid service or package install."""
import argparse
import json
import sys
from pathlib import Path
from keel_agent.io import read_json, write_private
from .model import adapt_body
from .service import Workbench


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo-export", help="write a synthetic frozen-time snapshot")
    demo.add_argument("--out", required=True)
    adapt = commands.add_parser("adapt-body", help="wrap a Keel 0.7 flow/assurance/trust JSON body")
    adapt.add_argument("--body", required=True); adapt.add_argument("--workspace", required=True)
    adapt.add_argument("--labels"); adapt.add_argument("--revision-sources"); adapt.add_argument("--out", required=True)
    for name in ("serve", "run", "bridge"):
        sub = commands.add_parser(name)
        mode = sub.add_mutually_exclusive_group(required=True)
        mode.add_argument("--demo", action="store_true"); mode.add_argument("--snapshot")
        sub.add_argument("--workspace", help="required for an operational snapshot")
        sub.add_argument("--attachment-root", help="host-owned root for verifying supplied attachment bytes")
        if name == "serve":
            sub.add_argument("--port", type=int, default=8765)
        if name == "run":
            sub.add_argument("--request", required=True); sub.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo-export":
            from .demo import make_demo
            write_private(args.out, make_demo()); return 0
        if args.command == "adapt-body":
            from .model import validate
            document = adapt_body(read_json(args.body), args.workspace,
                                  labels=read_json(args.labels) if args.labels else None,
                                  revision_sources=read_json(args.revision_sources) if args.revision_sources else None)
            validate(document, workspace_id=args.workspace, synthetic=False)
            write_private(args.out, document); return 0
        clock = None
        if args.demo:
            from .demo import make_demo, NOW
            document = make_demo(); workspace = document["workspace_id"]; clock = lambda: NOW
            if args.workspace and args.workspace != workspace: raise ValueError("demo workspace is fixed")
        else:
            if not args.workspace: parser.error("--workspace is required with --snapshot")
            document = read_json(args.snapshot); workspace = args.workspace
        root = Path(args.attachment_root).absolute() if args.attachment_root else None
        if root is not None and (not root.is_dir() or root.resolve() != root):
            raise ValueError("attachment root must be an existing non-symlink directory")
        app = Workbench(document, workspace_id=workspace, synthetic=args.demo, host_clock=clock, attachment_root=root)
        if args.command == "run":
            write_private(args.out, app.run(read_json(args.request))); return 0
        if args.command == "bridge":
            from .integrations import bridge_message, read_message, EOF
            while True:
                value = read_message(sys.stdin.buffer)
                if value is EOF: return 0
                print(json.dumps(bridge_message(app, value), allow_nan=False), flush=True)
        from .server import LocalServer
        with LocalServer(app, args.port) as server:
            print("Keel Workbench · "+("synthetic demo, frozen clock" if args.demo else "operational snapshot, host clock"), flush=True)
            print("Private session URL: http://"+server.authority+"/#token="+server.token, flush=True)
            print("Local review only. Ctrl-C stops the server and clears session history.", flush=True)
            server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt: return 0
    except (ValueError, OSError, TypeError, KeyError) as error:
        print("keel-workbench: "+str(error), file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
