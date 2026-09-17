#!/usr/bin/env python3
"""Watermark repair utility for the input tray.

A tray watermark records which tray items have already been presented, so a
digest run does not re-post the same item. When the watermark and the tray
drift (e.g. a dry-run advanced the watermark without delivery, or items were
retired), this tool repairs it: it keeps only keys that correspond to
genuinely-presented current tray items and drops orphans.

The "currently presented" key list is supplied by the caller
(--valid-keys JSON): this module never reads live queues or the tray digest
itself — it only performs the repair.

Dry-run by default; --live writes the watermark (with a backup first).

Usage:
    python3 recover_watermark.py --watermark PATH --valid-keys KEYS.json
    python3 recover_watermark.py --watermark PATH --valid-keys KEYS.json --live

KEYS.json: a JSON list of tray keys that should remain in the watermark.
"""
import json
import os
import shutil
import sys

USAGE = """usage: recover_watermark.py --watermark PATH --valid-keys KEYS.json [--live]

Repair a tray watermark: keep only keys present in KEYS.json, drop orphans.
  --watermark PATH    the watermark JSON file (list of key strings)
  --valid-keys F      JSON file holding the list of currently-valid keys
  --live              write the repaired watermark (default: dry-run, review only)
  --help, -h          this help
"""


def repair_watermark(watermark_path, valid_keys, dry_run=True, backup=True):
    """Return (report dict, target list). Writes only when dry_run is False.

    report: current/valid/kept/dropped key counts + the backup path used.
    """
    with open(watermark_path, encoding="utf-8") as f:
        current = json.load(f)
    if not isinstance(current, list):
        raise ValueError(f"watermark at {watermark_path} is not a JSON list")
    valid = set(valid_keys)
    kept = [k for k in current if k in valid]
    dropped = [k for k in current if k not in valid]
    report = {"watermark": watermark_path,
              "current_keys": len(current),
              "valid_keys": len(valid),
              "kept": len(kept),
              "dropped": len(dropped)}
    backup_path = None
    if not dry_run:
        if backup:
            backup_path = watermark_path + ".bak-repair"
            shutil.copy2(watermark_path, backup_path)
            report["backup"] = backup_path
        tmp = watermark_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kept, f)
        os.replace(tmp, watermark_path)
    return report, kept


def main(argv=None):
    argv = argv or sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if "--watermark" not in argv or "--valid-keys" not in argv:
        print(USAGE, file=sys.stderr)
        return 2
    wm_path = argv[argv.index("--watermark") + 1]
    keys_path = argv[argv.index("--valid-keys") + 1]
    live = "--live" in argv
    with open(keys_path, encoding="utf-8") as f:
        valid_keys = json.load(f)
    if not isinstance(valid_keys, list):
        print(f"{keys_path} is not a JSON list", file=sys.stderr)
        return 2
    report, _ = repair_watermark(wm_path, valid_keys, dry_run=not live)
    print(f"watermark keys: {report['current_keys']} -> keep "
          f"{report['kept']} (drop {report['dropped']} orphans)")
    if not live:
        print("--dry-run: no writes performed")
    else:
        print(f"repaired (backup: {report.get('backup', 'none')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
