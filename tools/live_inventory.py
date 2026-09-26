"""Explicit source/evidence allowlist for the additive 0.9 integration."""
import hashlib
import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def payload(root=ROOT, *, for_inventory=False):
    root = Path(root).absolute()
    baseline = json.loads((root / "live-base-manifest.json").read_text())["files"]
    # The original ZIP's MANIFEST.json is packaging-only metadata absent from
    # the original TXT and may describe a different host package. It remains
    # preserved and verified for a full reference build, but is not executable
    # source and is neither installed nor required for host regression checks.
    if for_inventory:
        baseline.pop('MANIFEST.json', None)
    extensions = json.loads((root / "live-release-files.json").read_text())
    if type(extensions) is not list or len(extensions) != len(set(extensions)):
        raise ValueError("unique explicit release file list required")
    if set(extensions) & set(baseline):
        raise ValueError("integration must not replace baseline files")
    result = {}
    for name in sorted(set(baseline) | set(extensions)):
        part = PurePosixPath(name)
        source = root / name
        if (str(part) != name or part.is_absolute() or '..' in part.parts or '\\' in name
                or any(p in {'.git', '__pycache__', '.pytest_cache', 'credentials', 'data', 'dist'} for p in part.parts)
                or source.is_symlink() or not source.resolve().is_relative_to(root)
                or not source.is_file() or source.stat().st_size > 8 * 1024 * 1024):
            raise ValueError("unsafe or missing release file: " + name)
        body = source.read_bytes()
        if name in baseline and hashlib.sha256(body).hexdigest() != baseline[name]:
            raise ValueError("preserved baseline changed: " + name)
        result[name] = body
    return result


def inventory_from_payload(files):
    """Bind one already-read immutable byte snapshot, avoiding a second read."""
    records = {n: hashlib.sha256(b).hexdigest() for n, b in files.items() if n != 'MANIFEST.json'}
    raw = json.dumps(records, sort_keys=True, separators=(',', ':')).encode()
    return {"schema": "keel.live.inventory.v1", "sha256": hashlib.sha256(raw).hexdigest(),
            "files": records, "file_count": len(records)}


def inventory(root=ROOT):
    return inventory_from_payload(payload(root, for_inventory=True))
