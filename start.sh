#!/usr/bin/env bash
# Keel — status overview launcher.
set -euo pipefail
cd "$(dirname "$0")"
export KEEL_HOME="${KEEL_HOME:-$PWD}"
VER="$(cat VERSION 2>/dev/null || echo dev)"

echo "=============================================="
echo " Keel v$VER — status overview"
echo " workspace: $KEEL_HOME"
echo "=============================================="

python3 - "$KEEL_HOME" << 'PYEOF'
import json, os, sys
home = sys.argv[1]
def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None
def items(d):
    return d if isinstance(d, list) else (d or {}).get("entries", d.get("items", [])) if d else []

bank = load(os.path.join(home, "data", "answer_bank.json")) or load(os.path.join(home, "engines", "answer_bank.json"))
bank_ok = bool(bank) and "YOUR_FIRST_NAME" not in json.dumps(bank.get("answers", {}))
print(f"  answer bank personalized: {'yes' if bank_ok else 'NO — run ./setup.sh'}")

qdir = os.path.join(home, "data", "queues")
total = 0
if os.path.isdir(qdir):
    for f in sorted(os.listdir(qdir)):
        if f.endswith(".json"):
            n = len(items(load(os.path.join(qdir, f))))
            total += n
            print(f"  queue {f}: {n} leads")
print(f"  total queued leads: {total}")

ledger = load(os.path.join(home, "data", "application-ledger.json"))
rows = ledger if isinstance(ledger, list) else (ledger or {}).get("rows", [])
sub = sum(1 for r in rows if r.get("status") == "SUBMITTED")
print(f"  submitted applications: {sub}")

dash = os.path.join(home, "dashboard", "dashboard.html")
print(f"  dashboard: {'present — open ' + dash if os.path.exists(dash) else 'not built yet (python3 engines/build_dashboard.py)'}")
PYEOF

echo
echo "Engines:"
echo "  apply_loop.py        build launch packets for READY leads"
echo "  verify_retry.py      re-verify parked leads (dry-run; --live to apply)"
echo "  feeder_watchdog.py   watch queue health"
echo "  outcome_analytics.py outcome analytics from ledger + telemetry"
echo "  edge_probe.py        ATS capability radar"
echo "  inbox_listener.py    employer-response intake (Maildir source)"
echo "  build_dashboard.py   regenerate the dashboard"
