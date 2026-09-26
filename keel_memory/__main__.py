"""Local evidence index CLI. Authentication is supplied by the observation host."""
import argparse
import json
import sys

from keel_loki.common import atomic_json, load_json
from keel_observability import ObservationStore
from .index import EvidenceIndex


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--observations', required=True)
    parser.add_argument('--store-id', required=True)
    parser.add_argument('--out')
    commands = parser.add_subparsers(dest='command', required=True)
    ingest = commands.add_parser('ingest')
    ingest.add_argument('document')
    query = commands.add_parser('search')
    query.add_argument('query')
    query.add_argument('--account', required=True)
    query.add_argument('--scope', required=True)
    query.add_argument('--purpose', choices=('planning','review','application_fact'), required=True)
    query.add_argument('--limit', type=int, default=5)
    refresh = commands.add_parser('refresh')
    refresh.add_argument('--account', required=True)
    artifact = commands.add_parser('register-artifact')
    artifact.add_argument('document')
    status = commands.add_parser('artifact-status')
    status.add_argument('--account', required=True)
    status.add_argument('--artifact', required=True)
    args = parser.parse_args(argv)
    try:
        observations = ObservationStore(args.observations, store_id=args.store_id)
        index = EvidenceIndex(args.home, observations)
        if args.command == 'ingest':
            result = index.ingest(load_json(args.document))
        elif args.command == 'search':
            result = index.search(args.query, account_id=args.account, scope=args.scope,
                                  purpose=args.purpose, limit=args.limit)
        elif args.command == 'refresh':
            result = index.refresh(args.account)
        elif args.command == 'register-artifact':
            result = index.register_artifact(load_json(args.document))
        else:
            result = index.artifact_status(args.account, args.artifact)
        if args.out:
            atomic_json(args.out, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 3 if result.get('status') in {'HELD','CONFLICT','STALE','QUERY_TOO_BROAD'} else 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print('keel-memory: invalid input, unavailable provenance, or blocked storage', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
