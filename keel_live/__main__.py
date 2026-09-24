"""Host-controlled capture, source export, proof reports and local review server."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from keel_agent.io import read_json, write_private
from keel_trust.common import keys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="isolated synthetic fixture; never real applicant data")
    demo.add_argument("--home", required=True, help="NEW fixture directory")
    demo.add_argument("--port", type=int, default=8765)
    commands = {}
    for name in ("init", "register", "capture", "export", "proof", "serve"):
        item = commands[name] = sub.add_parser(name)
        item.add_argument("--config", required=True, help="private host configuration")
        if name != "init":
            item.add_argument("--body", required=True, help="fresh host export: flow, assurance, trust")
        if name != "serve":
            item.add_argument("--out", help="new private JSON output; existing files refused")
    commands["capture"].add_argument("--event", required=True)
    commands["serve"].add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from tools.make_live_demo import create_demo
            from .surface import LiveSurface
            fixture = create_demo(args.home)
            app = LiveSurface(fixture.store, fixture.body, action="PREPARE",
                              principal=fixture.principal, synthetic=True)
            return _serve(app, args.port)
        from .config import load_config, principal_from_config
        from .connector import HostSourceConnector
        from keel_sources.store import SourceStore
        config = load_config(args.config)
        if args.command != "init" and not (Path(config["home"])/"store.identity").is_file():
            raise ValueError("initialize the host source store explicitly before use")
        store = SourceStore(config["home"], config["workspace_id"])
        connector = HostSourceConnector(store, producer_components=config["producer_components"], action=config["action"])
        if args.command == "init":
            result = {"state": "INITIALIZED", "source_records_created": 0,
                      "workspace_id": config["workspace_id"], "execution_authorized": False}
        else:
            def body_provider():
                body = read_json(args.body)
                keys(body, {"flow", "assurance", "trust"})
                return body
            body = body_provider()
            if args.command == "register":
                result = connector.register_flow(body["flow"])
            elif args.command == "capture":
                result = connector.capture_event(read_json(args.event), flow=body["flow"],
                                                  attachment_source_root=config["attachment_source_root"])
            elif args.command == "serve":
                from .surface import LiveSurface
                app = LiveSurface(store, body, action=config["action"],
                    principal=principal_from_config(config), body_provider=body_provider)
                return _serve(app, args.port)
            else:
                result = connector.export_workbench(flow=body["flow"], assurance=body["assurance"],
                                                     trust=body["trust"], synthetic=False)
                if args.command == "proof":
                    from .proof import build_proof
                    result = build_proof(result["workbench_snapshot"], workspace_id=config["workspace_id"],
                        synthetic=False, action=config["action"], attachment_root=store.attachment_root, now=store.clock())
        if args.out:
            write_private(args.out, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError, RecursionError) as error:
        print("keel-live: " + str(error)[:300], file=sys.stderr)
        return 2


def _serve(app, port):
    from .server import LiveServer
    with LiveServer(app, port=port) as server:
        print("Private local review: http://" + server.authority + "/review#token=" + server.token, flush=True)
        print("Keep this session URL private. Ctrl-C stops the server.", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
