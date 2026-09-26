"""Bounded local import/inspect/export. CLI imports can never certify identity."""
import argparse
import json
import sys
from pathlib import Path

from .store import MAX_EVENT_BYTES, ObservationStore


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--store-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    import_command = commands.add_parser("import")
    import_command.add_argument("event_file")
    inspect_command = commands.add_parser("inspect")
    inspect_command.add_argument("--account", required=True)
    inspect_command.add_argument("--event-id", required=True)
    export_command = commands.add_parser("export")
    export_command.add_argument("--account", required=True)
    export_command.add_argument("--after", type=int, default=0)
    export_command.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            store = ObservationStore.create(args.db, store_id=args.store_id)
            result = {"created": True, "store_id": store.store_id, "authentication_configured": False}
        else:
            store = ObservationStore(args.db, store_id=args.store_id)
            if args.command == "import":
                with Path(args.event_file).open("rb") as stream:
                    raw = stream.read(MAX_EVENT_BYTES + 1)
                if len(raw) > MAX_EVENT_BYTES:
                    raise ValueError("event_too_large")
                event = json.loads(raw, object_pairs_hook=_pairs,
                                   parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite_json")))
                result = store.ingest(event)
            elif args.command == "inspect":
                result = store.inspect(args.account, args.event_id)
            else:
                result = store.export(args.account, after_sequence=args.after, limit=args.limit)
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
