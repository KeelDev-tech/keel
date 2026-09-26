"""Read-only verification CLI; reports cannot authorize any external action."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from keel_agent.io import read_json, write_private
from keel_trust.common import digest
from .claims import verify_grounding
from .answers import resolve_grounded_answer
from .packet import verify_packet
from .demo import make_fixture
from .evidence import GroundingError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="create and verify a labelled synthetic fixture")
    demo.add_argument("--home", required=True)
    for name in ("evidence", "packet", "answer"):
        p = sub.add_parser(name)
        p.add_argument("--document", required=True); p.add_argument("--bindings", required=True)
        p.add_argument("--evidence-root", required=True); p.add_argument("--out", required=True)
        if name == "packet":
            p.add_argument("--packet", required=True); p.add_argument("--packet-root", required=True)
            p.add_argument("--expected-packet-sha256", required=True,
                           help="packet manifest digest obtained from the trusted host review context")
        if name == "answer":
            for arg in ("records", "context", "answer-bindings"): p.add_argument("--" + arg, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            home = Path(args.home).absolute()
            if home.exists(): raise GroundingError("demo_requires_new_home")
            home.mkdir(mode=0o700, parents=False)
            fixture = make_fixture(home)
            for name in ("document", "bindings", "packet", "records", "context", "answer_bindings"):
                write_private(home / (name + ".json"), fixture[name])
            report = {"synthetic": True, "execution_authorized": False,
                "evidence": verify_grounding(fixture["document"], fixture["bindings"], root=home/"evidence", now=fixture["now"]),
                "packet": verify_packet(fixture["document"], fixture["bindings"], fixture["packet"],
                    expected_packet_sha256=digest(fixture["packet"]),
                    evidence_root=home/"evidence", packet_root=home/"packet", now=fixture["now"]),
                "answer": resolve_grounded_answer(fixture["records"], fixture["context"], fixture["answer_bindings"],
                    fixture["document"], fixture["bindings"], root=home/"evidence", now=fixture["now"])}
            write_private(home / "synthetic-report.json", report)
            print(json.dumps({"synthetic": True, "report": str(home/"synthetic-report.json"), "execution_authorized": False}))
            return 0 if report["packet"]["status"] == "VERIFIED" and report["answer"]["status"] == "RESOLVED" else 1
        document, bindings = read_json(args.document), read_json(args.bindings)
        now = datetime.now(timezone.utc)
        if args.command == "evidence":
            report = verify_grounding(document, bindings, root=args.evidence_root, now=now)
        elif args.command == "packet":
            report = verify_packet(document, bindings, read_json(args.packet), evidence_root=args.evidence_root,
                                   expected_packet_sha256=args.expected_packet_sha256,
                                   packet_root=args.packet_root, now=now)
        else:
            report = resolve_grounded_answer(read_json(args.records), read_json(args.context), read_json(args.answer_bindings),
                                              document, bindings, root=args.evidence_root, now=now)
        write_private(args.out, report)
        print(json.dumps({"status": report["status"], "execution_authorized": False}))
        return 0 if report["status"] in {"VERIFIED", "RESOLVED"} else 1
    except (ValueError, OSError, TypeError, KeyError) as exc:
        code = exc.code if isinstance(exc, GroundingError) else "input_or_output_invalid"
        print(json.dumps({"status": "BLOCKED", "reason": code, "execution_authorized": False}), file=sys.stderr)
        return 2


if __name__ == "__main__": raise SystemExit(main())
