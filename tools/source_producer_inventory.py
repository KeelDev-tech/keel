"""Content inventory for the additive0.8 source producers and their0.7 base."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def files(root=ROOT):
    root = Path(root)
    selected = set()
    for pattern in ("keel_sources/*.py", "tests/test_sources*.py"):
        selected.update(str(path.relative_to(root)) for path in root.glob(pattern) if path.is_file())
    for name in ("tools/source_producer_inventory.py", "tools/install_source_producers.py",
                 "tools/build_source_producer_release.py", "tools/make_source_producer_demo.py", "tools/check_source_host.py",
                 "tools/run_source_producer_checks.py",
                 "docs/SOURCE_PRODUCERS.md", "docs/SOURCE_CAPTURE.md", "docs/SOURCE_APPROVALS.md",
                 "docs/SOURCE_PRODUCER_HANDOFF.md", "docs/HOST_READINESS.md"):
        if (root/name).is_file():
            selected.add(name)
    return sorted(selected)


def inventory(root=ROOT):
    root = Path(root)
    # The unchanged baseline inventory covers the full tested source/config,
    # including engines, monitors, tests and both guard runners. Add the new
    # package and its documentation explicitly, since0.7 does not enumerate it.
    from tools.source_inventory import inventory as baseline_inventory
    names = {row['file'] for row in baseline_inventory(root)['files']} | set(files(root))
    records = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(names)}
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return {"schema":"keel.source_producer_inventory.v1", "source_sha256":hashlib.sha256(encoded).hexdigest(),
            "files":records, "new_source_files":len(files(root)), "total_bound_files":len(records)}
