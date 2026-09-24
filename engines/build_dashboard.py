#!/usr/bin/env python3
"""Keel — dashboard generator (public edition).

Reads the application ledger + queues + backlog and renders a self-contained
mobile-friendly HTML dashboard. All paths resolve under KEEL_HOME
(or ~/keel). Interview cards come from INTERVIEW_INVITED ledger rows;
parked items come from data/parked.json (user-maintained) — nothing is
hardcoded, so no personal data can leak into the template.

K31/K32 (2026-09-18): import-safe — importing this module performs no
dashboard I/O; call build()/main() to regenerate. Missing or corrupt source
data renders as "Unknown" with a warning banner, never as a healthy zero.
K33 (2026-09-18): untrusted date fallback is HTML-escaped; the page carries a
restrictive Content-Security-Policy.
"""
import argparse
import json, html, os, re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from safe_io import rows, read_json, atomic_bytes

HOME = Path(os.environ.get("KEEL_HOME", str(Path.home() / "keel")))
LEDGER = HOME / "data" / "application-ledger.json"
QUEUE_DIR = HOME / "data" / "queues"
BACKLOG = HOME / "ORCHESTRATOR_BACKLOG.md"
PARKED_FILE = HOME / "data" / "parked.json"
OUT = HOME / "dashboard" / "dashboard.html"
TZ = ZoneInfo(os.environ.get("KEEL_TZ", "America/Los_Angeles"))


def load_json(path):
    """Read a JSON document.

    Returns (items, warning). items is None when the file is missing,
    unreadable, or malformed — callers must render "Unknown" for it, never a
    healthy zero (K31). warning is None on success.
    """
    path = Path(path)
    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        return as_items(read_json(path)), None
    except FileNotFoundError:
        return None, f"{path.name} is missing; its counts are unknown"
    except OSError as exc:
        return None, f"{path.name} could not be read ({exc}); its counts are unknown"
    except (ValueError, AttributeError, TypeError):
        return None, f"{path.name} is malformed; its counts are unknown"


def as_items(d):
    # 2026-09-19 (Keel 0.11.0 validation): honor live's own canonical
    # row-container contract (safe_io.ROW_KEYS) instead of only
    # entries/items. A ledger shaped {"rows": [...]} is a shape live's own
    # safe_io.rows() supports — silently flattening it to [] would report a
    # healthy zero for data that is present, the exact gap K31 exists to
    # stop. Non-dict/non-list top-level values keep failing closed through
    # load_json's (None, warning) path.
    return rows(d)


def esc(s):
    return html.escape(str(s or ""))


def fmt_ts(ts):
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        dt = dt.astimezone(TZ)
        return dt.strftime("%b %d, %I:%M %p")
    except Exception:
        # K33: the raw fallback is untrusted ledger/queue data — escape it so a
        # hostile date string (e.g. date_submitted="<img src=x onerror=...>") can
        # never inject markup into this user-facing page.
        return html.escape(str(ts)[:16].replace("T", " "))


def fmt_count(value):
    """Render a count, or "Unknown" when the underlying data was missing or
    corrupt (K31). On clean data this is str(int) — byte-identical to before."""
    return "Unknown" if value is None else str(value)


def collect(home=HOME):
    """Gather dashboard data. Pure collection — no rendering, no writes (K32)."""
    home = Path(home)
    data = {"warnings": []}

    ledger_items, warning = load_json(home / "data" / "application-ledger.json")
    if warning:
        data["warnings"].append(warning)
    data["ledger_known"] = ledger_items is not None
    ledger = ledger_items if ledger_items is not None else []
    submitted = [e for e in ledger if e.get("status") == "SUBMITTED"]
    interviews = [e for e in ledger if e.get("status") == "INTERVIEW_INVITED"]
    data["submitted"] = submitted
    data["interviews"] = interviews
    data["recent"] = sorted(
        (e for e in submitted if e.get("submitted_at") or e.get("date_submitted")),
        key=lambda e: str(e.get("submitted_at") or e.get("date_submitted")),
        reverse=True,
    )[:8]

    queues = {}
    queue_dir = home / "data" / "queues"
    queues_known = True
    if queue_dir.is_dir():
        required = {queue_dir / (name + '-queue.json')
                    for name in ('standard', 'strategic', 'needs_input')}
        for qf in sorted(required | set(queue_dir.glob("*.json"))):
            items, w = load_json(qf)
            if w:
                data["warnings"].append(w)
            label = qf.stem.removesuffix('-queue') if qf in required else qf.name
            queues[label] = None if items is None else len(items)
            if items is None:
                queues_known = False
    else:
        queues_known = False
        data["warnings"].append("queues directory is missing; queue counts are unknown")
    data["queues"] = queues
    data["queues_known"] = queues_known
    data["queue_total"] = sum(queues.values()) if queues_known else None

    # Backlog: extract ranked QUEUED items (optional file)
    backlog_items = []
    try:
        text = (home / "ORCHESTRATOR_BACKLOG.md").read_text()
        m = re.search(r"### QUEUED \(ranked\)(.*?)(?=^## |\Z)", text, re.S | re.M)
        if m:
            for line in m.group(1).splitlines():
                lm = re.match(r"\s*\d+\.\s+\*\*(.+?)\*\*\s*[—-]\s*(.+)", line)
                if lm:
                    backlog_items.append((lm.group(1).strip(), lm.group(2).strip()))
    except Exception:
        pass
    data["backlog_items"] = backlog_items

    # Parked items: user-maintained data file, never hardcoded.
    # Format: [{"title": "...", "detail": "...", "action": "..."}]
    parked_items, warning = load_json(home / "data" / "parked.json")
    if warning:
        data["warnings"].append(warning)
    data["parked"] = parked_items if parked_items is not None else []

    # Gate-blocked breakdown: telemetry events with a gate field.
    # Mirrors live's data/telemetry/events.jsonl layout under this HOME.
    # Reads the existing schema only (event_type/type, details.gate) — no
    # schema changes. Missing or unreadable -> None ("Unknown", never a
    # healthy zero — K31). Malformed lines are skipped, not fatal.
    gate_blocks = None
    tel_path = home / "data" / "telemetry" / "events.jsonl"
    try:
        with tel_path.open('rb') as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('telemetry size limit exceeded')
        tel_text = raw.decode('utf-8')
    except (OSError, ValueError):
        data["warnings"].append(
            "telemetry events.jsonl is missing; gate-block counts are unknown")
    else:
        counts = {}
        malformed = False
        for line in tel_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                from safe_io import loads
                ev = loads(line)
                if not isinstance(ev, dict) or not isinstance(ev.get('details', {}), dict):
                    raise ValueError('invalid event shape')
            except (ValueError, TypeError):
                malformed = True
                continue
            if (ev.get("event_type") or ev.get("type")) != "gate_blocked":
                continue
            gate = (ev.get("details") or {}).get("gate") or "unknown"
            if not isinstance(gate, str):
                malformed = True
                continue
            counts[gate] = counts.get(gate, 0) + 1
        if malformed:
            data['warnings'].append('telemetry contains malformed events; gate-block counts are unknown')
        else:
            gate_blocks = counts
    data["gate_blocks"] = gate_blocks

    data["now"] = datetime.now(TZ).strftime("%A, %b %d — %I:%M %p")
    return data


def render(data):
    """Render the dashboard HTML from collected data. Pure function of data."""
    submitted = data["submitted"]
    interviews = data["interviews"]
    queues = data["queues"]

    submitted_n = fmt_count(len(submitted) if data["ledger_known"] else None)
    interviews_n = fmt_count(len(interviews) if data["ledger_known"] else None)
    queue_total = fmt_count(
        None
        if not data["queues_known"] or any(v is None for v in queues.values())
        else sum(queues.values())
    )

    interview_html = "\n".join(
        f"<div class='card interview'><div class='co'>{esc(c.get('company'))}</div>"
        f"<div class='role'>{esc(c.get('title'))}</div>"
        f"<div class='dim'>{esc(c.get('contact') or '')} — {esc(c.get('detail') or '')}</div>"
        f"<div class='action'>⚠ {esc(c.get('action') or 'Needs your input')}</div></div>"
        for c in interviews
    ) or "<p class='dim'>No active interview threads.</p>"

    recent_rows = "\n".join(
        f"<tr><td>{fmt_ts(e.get('submitted_at') or e.get('date_submitted'))}</td>"
        f"<td><strong>{esc(e.get('company'))}</strong><br><span class='dim'>{esc(e.get('title') or e.get('role_id') or '')}</span></td></tr>"
        for e in data["recent"]
    ) or "<tr><td colspan='2' class='dim'>No submissions yet.</td></tr>"

    queue_rows = "\n".join(
        f"<div class='qrow'><span>{esc(k)}</span><strong>{fmt_count(v)}</strong></div>"
        for k, v in sorted(queues.items())
    ) or "<p class='dim'>No queues yet.</p>"

    backlog_rows = "\n".join(
        f"<div class='brow'><span class='rank'>{i+1}</span>"
        f"<div><strong>{esc(t)}</strong><br><span class='dim'>{esc(d)}</span></div></div>"
        for i, (t, d) in enumerate(data["backlog_items"][:7])
    ) or "<p class='dim'>Backlog empty.</p>"

    parked_html = "\n".join(
        f"<div class='prow'><strong>{esc(p.get('title'))}</strong><br><span class='dim'>{esc(p.get('detail'))}</span></div>"
        for p in data["parked"]
    ) or "<p class='dim'>Nothing parked.</p>"

    gate_blocks = data.get("gate_blocks")
    if gate_blocks is None:
        gate_html = "<p class='dim'>Unknown — telemetry is missing or incomplete.</p>"
    elif not gate_blocks:
        gate_html = "<p class='dim'>No gate blocks recorded.</p>"
    else:
        gate_html = "\n".join(
            f"<div class='qrow'><span>{esc(g)}</span><strong>{n}</strong></div>"
            for g, n in sorted(gate_blocks.items(), key=lambda kv: (-kv[1], kv[0]))
        )

    warning_html = ""
    if data["warnings"]:
        items = "".join(f"<li>{esc(w)}</li>" for w in data["warnings"])
        warning_html = (
            "<div class='card'><div class='co'>⚠ Data needs attention</div>"
            f"<div class='dim'><ul style='margin:8px 0 0 18px;font-size:13px'>{items}</ul></div></div>\n"
        )

    now = data["now"]

    html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Keel — Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1419;color:#e8eef4;padding:16px;max-width:720px;margin:0 auto}}
h1{{font-size:22px;margin-bottom:2px}}
.sub{{color:#8b9bab;font-size:13px;margin-bottom:16px}}
.live{{display:inline-block;background:#123f2a;color:#4ade80;font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;margin-bottom:10px;letter-spacing:.5px}}
.score{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:14px 0}}
.stat{{background:#18222c;border-radius:12px;padding:14px 10px;text-align:center}}
.stat .n{{font-size:26px;font-weight:800;color:#fff}}
.stat .l{{font-size:11px;color:#8b9bab;margin-top:4px}}
h2{{font-size:15px;text-transform:uppercase;letter-spacing:1px;color:#8b9bab;margin:22px 0 10px}}
.card{{background:#18222c;border-radius:12px;padding:14px;margin-bottom:10px}}
.interview{{border-left:4px solid #f59e0b}}
.co{{font-weight:700;font-size:16px}}
.role{{color:#c9d6e2;margin:2px 0 6px}}
.dim{{color:#8b9bab;font-size:13px}}
.action{{margin-top:8px;color:#fbbf24;font-size:13px;font-weight:600}}
table{{width:100%;border-collapse:collapse}}
td{{padding:9px 4px;border-bottom:1px solid #243040;font-size:13px;vertical-align:top}}
td:first-child{{color:#8b9bab;white-space:nowrap;width:110px}}
.qrow,.brow{{display:flex;justify-content:space-between;align-items:center;background:#18222c;border-radius:10px;padding:11px 14px;margin-bottom:8px;font-size:14px}}
.brow{{justify-content:flex-start;gap:12px;align-items:flex-start}}
.rank{{background:#243b55;color:#7db8f0;font-weight:800;border-radius:8px;min-width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px}}
.prow{{background:#1c1618;border:1px solid #3a2a2e;border-radius:10px;padding:11px 14px;margin-bottom:8px;font-size:13px}}
footer{{margin:26px 0 10px;color:#5b6b7d;font-size:12px;line-height:1.6}}
</style></head><body>
<div class="live">LOCAL SNAPSHOT</div>
<h1>Keel</h1>
<div class="sub">Updated {esc(now)} · Provider verification is not connected</div>
{warning_html}
<div class="score">
<div class="stat"><div class="n">{submitted_n}</div><div class="l">Reported submission claims</div></div>
<div class="stat"><div class="n">{interviews_n}</div><div class="l">Interview<br>invites</div></div>
<div class="stat"><div class="n">{queue_total}</div><div class="l">Roles in<br>queues</div></div>
</div>

<h2>Interview pipeline — needs you</h2>
{interview_html}

<h2>Recent submission claims</h2>
<div class="card"><table>{recent_rows}</table></div>

<h2>Queues</h2>
{queue_rows}

<h2>Objectives — orchestrator backlog</h2>
{backlog_rows}

<h2>Parked / blocked</h2>
{parked_html}

<h2>Gate blocks</h2>
{gate_html}

<footer>
This is an offline view of local records. Status labels are claims, not provider confirmations. Preparing a packet does not submit an application or authorize an executor.
</footer>
</body></html>"""
    return html_doc


def build(home=HOME, out=None):
    """Collect, render, and write the dashboard. The sanctioned write path (K32)."""
    home = Path(home)
    out = Path(out) if out is not None else home / "dashboard" / "dashboard.html"
    html_doc = render(collect(home))
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(out, html_doc.encode('utf-8'))
    return out, html_doc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None,
                        help="Write the dashboard HTML here instead of the default path.")
    args = parser.parse_args(argv)
    out, html_doc = build(out=args.out)
    print(f"Wrote {out} ({len(html_doc)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
