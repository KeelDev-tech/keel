#!/usr/bin/env python3
"""LLM wording-polish hook for triage briefs (GPT + Gemini).

``polish(line)`` / ``polish_gemini(line)`` take a deterministic triage brief
line (as rendered by ``tray_triage.brief_line``) and return a reworded version
with identical meaning. The classifier's verdict -- class, cited rule, reply
shape -- is never delegated; only wording is.

``polish_best(line)`` runs every enabled provider, appends one comparison row
to the JSONL log (input, each provider's output + latency, chosen winner),
and returns the preferred provider's valid output -- falling back to the
other provider, then to the original deterministic line.

Fail-safe contract (the digest must never break or stall on this):
- disabled via config -> original line
- CLI error / timeout / bad output -> original line
- post-checks: the bracketed label must survive, output must be non-empty
  and under the length cap; otherwise -> original line
- per-process, per-provider circuit breaker: the first failure disables that
  provider for the rest of the process, so one dead backend can't add N
  timeouts to a run.

Config: <keel-home>/hidden_files/tray_polish.json
  {"enabled": true, "model": "gpt-4o-mini", "max_tokens": 120,
   "timeout_s": 20,
   "gemini": {"enabled": true, "model": "gemini-3.1-flash-lite",
              "max_tokens": 120, "timeout_s": 20},
   "compare": {"enabled": true, "log": "tray_polish_compare.jsonl",
               "prefer": "gpt"}}
Missing config == GPT enabled with defaults; a missing "gemini" section ==
Gemini disabled. Set a provider's "enabled" to false to kill-switch it.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time

from keel_paths import HOME  # noqa: E402 — repo path convention
HIDDEN = os.path.join(HOME, "hidden_files")
CONFIG = os.path.join(HIDDEN, "tray_polish.json")
# Reword-provider backends. Override with env vars; a backend whose binary
# is absent is skipped gracefully (provider reports disabled).
SKILL_BIN = os.environ.get(
    "KEEL_OPENAI_POLISH_BIN",
    os.path.expanduser("~/workspace/skills/openai/bin/openai_polish.py"))
GEMINI_BIN = os.environ.get(
    "KEEL_GEMINI_ASK_BIN",
    os.path.expanduser("~/workspace/skills/gemini/bin/gemini_ask.py"))

POLISH_SYSTEM = (
    "You reword one-line triage briefs for a job-application input tray. "
    "Rules: keep the word 'triage' and the bracketed label exactly as-is "
    "(e.g. 'triage [D1 travel]'); keep "
    "the meaning identical -- rewording only, never new content; do not add "
    "advice, recommendations, or answers; do not answer the underlying "
    "application question; keep it under 200 characters with a mobile-chat "
    "tone. Output only the reworded line, nothing else."
)
MAX_LEN = 280

_dead = {"gpt": False, "gemini": False}  # per-process circuit breakers


def _config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def enabled() -> bool:
    """GPT master switch. Missing config == enabled (legacy default)."""
    return bool(_config().get("enabled", True))


def gemini_enabled() -> bool:
    """Gemini switch. Missing section == disabled (explicit opt-in)."""
    return bool(_config().get("gemini", {}).get("enabled", False))


def _reset() -> None:
    """Test hook: clear both circuit breakers."""
    for k in _dead:
        _dead[k] = False


def _validate(line: str, out: str) -> str | None:
    """Post-checks shared by both providers. Returns the final line or None."""
    out = (out or "").strip()
    if not out:
        return None
    if "triage" not in out.lower():
        return None
    label = re.search(r"\[[^\]]+\]", line)
    if label and label.group(0) not in out:
        return None
    # Content is the model's; presentation (leading indent) stays the digest's.
    indent = line[:len(line) - len(line.lstrip())]
    out = indent + out
    if len(out) > MAX_LEN:
        return None
    return out


def _attempt(provider: str, line: str) -> tuple[bool, str, int]:
    """Run one provider. Returns (ok, output, latency_ms). Never raises.

    A disabled provider returns (False, "", 0) WITHOUT tripping its breaker;
    any real failure trips the provider's breaker for the rest of the process.
    """
    cfg = _config()
    if provider == "gpt":
        if _dead["gpt"] or not enabled() or not os.path.isfile(SKILL_BIN or ""):
            return (False, "", 0)
        cmd = [
            sys.executable, SKILL_BIN, line,
            "--model", str(cfg.get("model", "gpt-4o-mini")),
            "--max-tokens", str(cfg.get("max_tokens", 120)),
        ]
        timeout = float(cfg.get("timeout_s", 20))
    elif provider == "gemini":
        g = cfg.get("gemini", {})
        if _dead["gemini"] or not gemini_enabled() or not os.path.isfile(GEMINI_BIN or ""):
            return (False, "", 0)
        cmd = [
            sys.executable, GEMINI_BIN,
            "--model", str(g.get("model", "gemini-3.1-flash-lite")),
            "--temperature", "0.2",
            "--max-tokens", str(g.get("max_tokens", 120)),
            "--system", POLISH_SYSTEM,
            line,
        ]
        timeout = float(g.get("timeout_s", 20))
    else:
        return (False, "", 0)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        out = _validate(line, proc.stdout or "")
        if proc.returncode != 0 or out is None:
            raise RuntimeError("provider failed")
        return (True, out, latency_ms)
    except Exception:
        _dead[provider] = True
        return (False, "", int((time.perf_counter() - t0) * 1000))


def polish(line: str) -> str:
    """Return the GPT-reworded line, or the original on any failure."""
    if not line:
        return line
    ok, out, _ = _attempt("gpt", line)
    return out if ok else line


def polish_gemini(line: str) -> str:
    """Return the Gemini-reworded line, or the original on any failure."""
    if not line:
        return line
    ok, out, _ = _attempt("gemini", line)
    return out if ok else line


def _compare_log_path() -> str | None:
    cmp_cfg = _config().get("compare", {})
    if not cmp_cfg.get("enabled", False):
        return None
    name = str(cmp_cfg.get("log", "tray_polish_compare.jsonl"))
    return name if os.path.isabs(name) else os.path.join(HIDDEN, name)


def _log_compare(row: dict) -> None:
    path = _compare_log_path()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # comparison logging must never break the digest


def _result_cell(ok: bool, out: str, latency_ms: int) -> dict:
    cell = {"ok": ok, "latency_ms": latency_ms}
    if ok:
        cell["output"] = out
    return cell


def polish_best(line: str) -> str:
    """Run every enabled provider, log the comparison, return the winner.

    Preference order comes from config compare.prefer ("gpt" default); a
    failed preferred provider falls back to the other provider's valid
    output, then to the original line.
    """
    if not line:
        return line
    g_ok, g_out, g_ms = _attempt("gpt", line)
    m_ok, m_out, m_ms = _attempt("gemini", line)
    prefer = str(_config().get("compare", {}).get("prefer", "gpt"))
    order = [("gpt", g_ok, g_out), ("gemini", m_ok, m_out)]
    if prefer == "gemini":
        order.reverse()
    chosen, winner = line, "deterministic"
    for name, ok, out in order:
        if ok:
            chosen, winner = out, name
            break
    _log_compare({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input": line,
        "gpt": _result_cell(g_ok, g_out, g_ms),
        "gemini": _result_cell(m_ok, m_out, m_ms),
        "chosen": winner,
    })
    return chosen
