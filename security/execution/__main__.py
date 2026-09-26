"""Bounded IPC utilities. The default daemon has no production execution adapter."""
import argparse
import json
import os
import stat
import sys
from .boundary import Boundary
from .envelope import ActionEnvelope, MAX_WIRE, canonical, strict_json
from .ipc import UnixServer, UnixClient


def _request_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_WIRE:
            raise ValueError("invalid_request_file")
        return strict_json(stream.read(MAX_WIRE + 1))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Keel protected boundary. Default daemon refuses all execution.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run an UNBOUND daemon; no production handler or authority installed")
    serve.add_argument("--socket", required=True)
    serve.add_argument("--worker-uid", required=True, type=int)
    serve.add_argument("--actor", required=True)
    serve.add_argument("--socket-gid", type=int)
    client = commands.add_parser("client", help="Send one request with no automatic retry")
    client.add_argument("--socket", required=True)
    client.add_argument("--server-uid", required=True, type=int)
    client.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            with UnixServer(args.socket, Boundary(), {args.worker_uid: args.actor},
                            socket_gid=args.socket_gid) as server:
                print(json.dumps({"status": "UNBOUND", "execution_enabled": False}), flush=True)
                server.serve_forever()
            return 0
        result = UnixClient(args.socket, expected_server_uid=args.server_uid).execute(_request_file(args.request))
        print(canonical(result).decode("ascii"))
        return 0 if result["status"] in {"submitted", "not_submitted"} else (3 if result["status"] == "unknown" else 2)
    except KeyboardInterrupt:
        return 130
    except Exception:
        # A transport error may follow dispatch. No details/payload leak and no retry.
        print(json.dumps({"status": "unknown" if args.command == "client" else "held",
                          "code": "client_transport_or_validation_error" if args.command == "client" else "daemon_start_or_runtime_error"}), file=sys.stderr)
        return 3 if args.command == "client" else 2


if __name__ == "__main__":
    raise SystemExit(main())
