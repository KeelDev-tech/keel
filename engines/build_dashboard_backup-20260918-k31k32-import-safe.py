#!/usr/bin/env python3
"""Keel — dashboard generator (public edition).

Reads the application ledger + queues + backlog and renders a self-contained
mobile-friendly HTML dashboard. All paths resolve under KEEL_HOME
(or ~/keel). Interview cards come from INTERVIEW_INVITED ledger rows;
parked items come from data/parked.json (user-maintained) — nothing is
hardcoded, so no personal data can leak into the template.
"""
import json, html, os, re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(os.environ.get("KEEL_HOME", str(Path.home() / "keel")))
LEDGER = HOME / "data" / "application-ledger.json"
QUEUE_DIR = HOME / "data" / "queues"
BACKLOG = HOME / "ORCHESTRATOR_BACKLOG.md"
PARKED_FILE = HOME / "data" / "parked.json"
OUT = HOME / "dashboard" / "dashboard.html"
TZ = ZoneInfo(os.environ.get("KEEL_TZ", "America/Los_Angeles"))


def load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return []


def as_items(d):
    if isinstance(d, list):
        return d
    return d.get("entries", d.get("items", []))


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


ledger = as_items(load_json(LEDGER))
submitted = [e for e in ledger if e.get("status") == "SUBMITTED"]
interviews = [e for e in ledger if e.get("status") == "INTERVIEW_INVITED"]
recent = sorted(
    (e for e in submitted if e.get("submitted_at") or e.get("date_submitted")),
    key=lambda e: str(e.get("submitted_at") or e.get("date_submitted")),
    reverse=True,
)[:8]

queues = {}
if QUEUE_DIR.exists():
    for qf in QUEUE_DIR.glob("*.json"):
        queues[qf.stem] = len(as_items(load_json(qf)))

# Backlog: extract ranked QUEUED items (optional file)
backlog_items = []
try:
    text = BACKLOG.read_text()
    m = re.search(r"### QUEUED \(ranked\)(.*?)(?=^## |\Z)", text, re.S | re.M)
    if m:
        for line in m.group(1).splitlines():
            lm = re.match(r"\s*\d+\.\s+\*\*(.+?)\*\*\s*[—-]\s*(.+)", line)
            if lm:
                backlog_items.append((lm.group(1).strip(), lm.group(2).strip()))
except Exception:
    pass

# Parked items: user-maintained data file, never hardcoded.
# Format: [{"title": "...", "detail": "...", "action": "..."}]
parked = as_items(load_json(PARKED_FILE))

now = datetime.now(TZ).strftime("%A, %b %d — %I:%M %p")

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
    for e in recent
) or "<tr><td colspan='2' class='dim'>No submissions yet.</td></tr>"

queue_rows = "\n".join(
    f"<div class='qrow'><span>{esc(k)}</span><strong>{v}</strong></div>"
    for k, v in sorted(queues.items())
) or "<p class='dim'>No queues yet.</p>"

backlog_rows = "\n".join(
    f"<div class='brow'><span class='rank'>{i+1}</span>"
    f"<div><strong>{esc(t)}</strong><br><span class='dim'>{esc(d)}</span></div></div>"
    for i, (t, d) in enumerate(backlog_items[:7])
) or "<p class='dim'>Backlog empty.</p>"

parked_html = "\n".join(
    f"<div class='prow'><strong>{esc(p.get('title'))}</strong><br><span class='dim'>{esc(p.get('detail'))}</span></div>"
    for p in parked
) or "<p class='dim'>Nothing parked.</p>"

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
<div class="live">● LIVE — AUTO-REFRESHED</div>
<h1>Keel</h1>
<div class="sub">Updated {esc(now)} · evidence-only counts · ledger-verified</div>

<div class="score">
<div class="stat"><div class="n">{len(submitted)}</div><div class="l">Applications<br>submitted</div></div>
<div class="stat"><div class="n">{len(interviews)}</div><div class="l">Interview<br>invites</div></div>
<div class="stat"><div class="n">{sum(queues.values())}</div><div class="l">Roles in<br>queues</div></div>
</div>

<h2>Interview pipeline — needs you</h2>
{interview_html}

<h2>Recent submissions</h2>
<div class="card"><table>{recent_rows}</table></div>

<h2>Queues</h2>
{queue_rows}

<h2>Objectives — orchestrator backlog</h2>
{backlog_rows}

<h2>Parked / blocked</h2>
{parked_html}

<footer>
Standing rules: clean leads needing nothing from you are auto-submitted. No outreach, no payments, no fabricated credentials. Items needing your input are parked, never prompted repeatedly. Counts increment only on explicit confirmation pages.
</footer>
</body></html>"""

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(html_doc)
print(f"Wrote {OUT} ({len(html_doc)} bytes)")
