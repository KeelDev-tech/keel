#!/usr/bin/env python3
"""Adversarial review loop for the application pipeline (stdlib only).

One change per invocation -- batch sweeps are never run (batch jobs need
the applicant's explicit word per the skill operating rules). Two reviews per change:

  GEMINI (gemini_ask.py --gap-test + verdict-forcing system prompt):
      red-team the change -- loopholes, timestamp/logic flaws, fail-open paths.
  GPT (openai_ask.py):
      use-case review. Use cases wired (operator-authorized):
        fixtures    - generate adversarial regression fixtures for test suites
        code-review - review engine code for logic bugs the red-team missed
        packet-qa   - packet-copy QA on SYNTHETIC fixtures only
        wording     - triage brief wording (delegates to openai_polish.py)

Model selection: gpt-4o-mini default (cheap, fast). --escalate switches to a
stronger model for hard reasoning reviews; every call logs model + token
counts to the cost ledger so spend stays visible.

SAFETY (non-negotiable, enforced in code):
  - PII pre-filter on every prompt: email, phone, SSN, the user's name.
    A hit -> review refused BEFORE any API call ("review unavailable").
  - Fail closed: any review failure (API error, 429, timeout, empty output,
    unparseable verdict) -> "review unavailable". It never blocks a ship and
    never invents a verdict.
  - 429 = hard stop: no retry, mark unavailable.

Ship gate (used by blackboard.py ship --adversarial):
  - verdict FAIL (red-team found a real loophole) -> BLOCK the ship, findings
    attached. Fix, don't bypass.
  - verdict unavailable -> ship proceeds with a note (never blocks on an
    unavailable review).

Cost ledger: <keel-home>/hidden_files/adversarial-cost-ledger.jsonl
(one JSON line per API call).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------- paths

_HERE = os.path.dirname(os.path.abspath(__file__))
_JP = os.path.abspath(os.path.join(_HERE, "..", ".."))
_WS = os.path.abspath(os.path.join(_JP, ".."))

GEMINI_CLI = os.path.join(_WS, "skills", "gemini", "bin", "gemini_ask.py")
OPENAI_ASK_CLI = os.path.join(_WS, "skills", "openai", "bin", "openai_ask.py")
OPENAI_POLISH_CLI = os.path.join(_WS, "skills", "openai", "bin", "openai_polish.py")

from keel_paths import HOME  # noqa: E402 — repo path convention
_COST_LEDGER = os.path.join(HOME, "hidden_files", "adversarial-cost-ledger.jsonl")

GPT_DEFAULT_MODEL = "gpt-4o-mini"
GPT_ESCALATED_MODEL = "gpt-4o"  # stronger model for hard reasoning reviews

USE_CASES = ("fixtures", "code-review", "packet-qa", "wording")

VERDICT_SYSTEM = (
    "You are a skeptical senior systems reviewer auditing a change to an "
    "automated job-application pipeline. Find loopholes, timestamp/logic "
    "flaws, and fail-open paths. Be concrete and terse. End your response "
    "with exactly one line: either 'VERDICT: FAIL' (you found a real "
    "loophole or defect) or 'VERDICT: PASS' (the change looks sound)."
)

_USECASE_SYSTEMS = {
    "fixtures": (
        "You write adversarial regression fixtures for a Python test suite. "
        "Given the change described, produce concrete edge-case fixtures "
        "(inputs + expected behavior) that would catch regressions of this "
        "change. Terse, code-ready. End with exactly one line: "
        "'VERDICT: PASS' or 'VERDICT: FAIL'."
    ),
    "code-review": (
        "You review engine code for logic bugs: off-by-one, timestamp "
        "semantics, race windows, fail-open branches, guard bypasses. Be "
        "concrete, cite the code. End with exactly one line: 'VERDICT: FAIL' "
        "if you found a real bug, else 'VERDICT: PASS'."
    ),
    "packet-qa": (
        "You QA launch-packet copy against SYNTHETIC fixture data only. "
        "Flag invented qualifications, unverifiable claims, or wording that "
        "overstates evidence. Never invent facts. End with exactly one "
        "line: 'VERDICT: FAIL' if you found a real problem, else "
        "'VERDICT: PASS'."
    ),
    "wording": (
        "You reword one-line triage briefs. Keep the bracketed label exactly "
        "as-is, keep meaning identical, rewording only. Output the reworded "
        "line, then a final line 'VERDICT: PASS'."
    ),
}

# ---------------------------------------------------------------- PII filter

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
# The operator's name must never ride along in a prompt. Configured via
# KEEL_OPERATOR_NAME ("First Last"); when unset the pattern matches nothing.
def _operator_name_re():
    name = os.environ.get("KEEL_OPERATOR_NAME", "").strip()
    if not name:
        return re.compile(r"(?!x)x")  # never matches
    parts = [re.escape(p) for p in name.split()]
    # first token required, remaining tokens optional (mirrors the
    # original fixed pattern's shape)
    rest = "".join(r"(\s+%s)?" % p for p in parts[1:])
    return re.compile(r"\b" + parts[0] + rest + r"\b", re.IGNORECASE)


_NAME_RE = _operator_name_re()


class PIIBlocked(Exception):
    """Raised when a prompt fails the PII pre-filter."""


def assert_no_pii(text: str) -> None:
    """Fail closed: any PII-shaped content blocks the review before any call."""
    for label, rx in (("email", _EMAIL_RE), ("phone", _PHONE_RE),
                      ("ssn", _SSN_RE), ("user-name", _operator_name_re())):
        if rx.search(text):
            raise PIIBlocked(f"prompt blocked: contains {label}")


# ---------------------------------------------------------------- runners

def _default_run_cli(cli: str, args: list[str], stdin_text: str,
                     timeout: int = 180) -> tuple[int, str, str]:
    """Run a skill CLI. Returns (rc, stdout, stderr). No retries, ever."""
    try:
        proc = subprocess.run(
            [sys.executable, cli, *args],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    except OSError as exc:
        return (127, "", f"os error: {exc}")


def _is_rate_limited(stderr: str) -> bool:
    return "429" in stderr or "rate" in stderr.lower() and "limit" in stderr.lower()


def parse_verdict(text: str) -> str:
    """Extract PASS/FAIL from a trailing 'VERDICT: X' line. Unparseable -> unavailable."""
    m = re.search(r"^\s*VERDICT:\s*(PASS|FAIL)\s*$", text,
                  re.IGNORECASE | re.MULTILINE)
    if not m:
        return "unavailable"
    return "pass" if m.group(1).upper() == "PASS" else "fail"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_cost(provider: str, model: str, usecase: str, role: str,
             prompt_chars: int, response_chars: int, usage: dict,
             status: str, ledger_path: str | None = None) -> None:
    """Append one cost-ledger line per API call. Never logs credentials."""
    path = ledger_path or _COST_LEDGER
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "ts": _utcnow(),
        "provider": provider,
        "model": model,
        "usecase": usecase,
        "role": role,  # 'red-team' | 'usecase-review'
        "prompt_chars": prompt_chars,
        "response_chars": response_chars,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "total_tokens": (usage or {}).get("total_tokens"),
        "status": status,  # ok | pii-blocked | unavailable | rate-limited
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- review

def _review_one(cli: str, cli_args: list[str], prompt: str, provider: str,
                model: str, usecase: str, role: str, ledger_path: str | None,
                run_cli_fn=None) -> dict:
    """Run one reviewer. Fail closed -> verdict 'unavailable', never raises."""
    run_cli_fn = run_cli_fn or _default_run_cli
    try:
        assert_no_pii(prompt)
    except PIIBlocked as exc:
        log_cost(provider, model, usecase, role, len(prompt), 0, {},
                 "pii-blocked", ledger_path)
        return {"provider": provider, "model": model, "verdict": "unavailable",
                "reason": str(exc), "output": ""}
    rc, out, err = run_cli_fn(cli, cli_args, prompt)
    usage: dict = {}
    text = out.strip()
    if rc == 0 and text:
        # openai_ask.py --json carries usage; gemini CLI is text-only.
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and "text" in payload:
                text = payload.get("text", "")
                usage = payload.get("usage", {}) or {}
        except (ValueError, AttributeError):
            pass
        verdict = parse_verdict(text)
        status = "ok" if verdict != "unavailable" else "unavailable"
    else:
        verdict = "unavailable"
        status = "rate-limited" if _is_rate_limited(err) else "unavailable"
        text = ""
    log_cost(provider, model, usecase, role, len(prompt), len(text), usage,
             status, ledger_path)
    return {"provider": provider, "model": model, "verdict": verdict,
            "reason": ("rate-limited" if status == "rate-limited"
                       else ("ok" if verdict != "unavailable" else "review failed")),
            "output": text}


def review_change(diff_text: str, usecase: str = "code-review",
                  escalate: bool = False, gpt_model: str | None = None,
                  ledger_path: str | None = None,
                  run_cli_fn=None) -> dict:
    """Run the two-review adversarial loop over one change.

    Returns {"gemini": {...}, "gpt": {...}, "verdict": pass|fail|unavailable,
             "blocked": bool}. Never raises on review failure.
    """
    if usecase not in USE_CASES:
        raise ValueError(f"unknown usecase {usecase!r}; expected one of {USE_CASES}")
    model = gpt_model or (GPT_ESCALATED_MODEL if escalate else GPT_DEFAULT_MODEL)

    gemini = _review_one(
        GEMINI_CLI, ["--gap-test", "--system", VERDICT_SYSTEM,
                     "--max-tokens", "2048"],
        "Change under review (design/system content only):\n\n" + diff_text,
        provider="gemini", model="gemini-3.1-flash-lite",
        usecase=usecase, role="red-team",
        ledger_path=ledger_path, run_cli_fn=run_cli_fn)

    if usecase == "wording":
        gpt = _review_one(
            OPENAI_POLISH_CLI, ["--model", model],
            diff_text,  # single triage brief line
            provider="openai", model=model, usecase=usecase,
            role="usecase-review", ledger_path=ledger_path,
            run_cli_fn=run_cli_fn)
    else:
        gpt = _review_one(
            OPENAI_ASK_CLI, ["--json", "--model", model,
                             "--system", _USECASE_SYSTEMS[usecase],
                             "--max-tokens", "1500"],
            "Change under review (design/system/synthetic content only):\n\n" + diff_text,
            provider="openai", model=model, usecase=usecase,
            role="usecase-review", ledger_path=ledger_path,
            run_cli_fn=run_cli_fn)

    verdicts = {gemini["verdict"], gpt["verdict"]}
    if "fail" in verdicts:
        overall = "fail"
    elif verdicts == {"unavailable"}:
        overall = "unavailable"
    else:
        overall = "pass"
    return {"gemini": gemini, "gpt": gpt, "usecase": usecase,
            "gpt_model": model, "verdict": overall,
            "blocked": overall == "fail", "ts": _utcnow()}


def ship_gate(result: dict) -> tuple[bool, str]:
    """Decide whether a blackboard ship may proceed.

    Returns (allow, reason). FAIL blocks; unavailable never blocks.
    """
    if result.get("verdict") == "fail":
        findings = []
        for key in ("gemini", "gpt"):
            r = result.get(key) or {}
            if r.get("verdict") == "fail":
                findings.append(f"{r.get('provider')}: {(r.get('output') or '')[:2000]}")
        return (False, "adversarial review FAILED -- fix, don't bypass. Findings: "
                + " || ".join(findings))
    if result.get("verdict") == "unavailable":
        return (True, "adversarial review unavailable (fail-closed, non-blocking); "
                      "ship proceeds with review noted as unavailable")
    return (True, "adversarial review passed")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Adversarial review loop: red-team one change (Gemini + GPT)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff", help="path to a diff/change file under review")
    src.add_argument("--brief", help="change text passed directly")
    ap.add_argument("--usecase", default="code-review", choices=USE_CASES,
                    help="GPT review use case (fixtures|code-review|packet-qa|wording)")
    ap.add_argument("--escalate", action="store_true",
                    help="use the stronger GPT model for hard reasoning reviews")
    ap.add_argument("--model", default=None,
                    help="explicit GPT model override (a spend decision; logged)")
    ap.add_argument("--cost-ledger", default=None,
                    help="override cost-ledger path (tests)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the PII filter only; no API calls")
    ap.add_argument("--quiet-output", action="store_true",
                    help="omit reviewer output text from the JSON report")
    args = ap.parse_args(argv)

    if args.diff:
        with open(args.diff, encoding="utf-8") as fh:
            change = fh.read()
    else:
        change = args.brief
    if not change.strip():
        print(json.dumps({"ok": False, "error": "empty change"}))
        return 2

    if args.dry_run:
        try:
            assert_no_pii(change)
        except PIIBlocked as exc:
            print(json.dumps({"ok": False, "error": str(exc),
                              "verdict": "unavailable"}))
            return 3
        print(json.dumps({"ok": True, "dry_run": True, "verdict": "unavailable",
                          "note": "PII filter passed; no API calls made"}))
        return 0

    result = review_change(change, usecase=args.usecase,
                           escalate=args.escalate, gpt_model=args.model,
                           ledger_path=args.cost_ledger)
    if args.quiet_output:
        for key in ("gemini", "gpt"):
            result[key] = {k: v for k, v in result[key].items() if k != "output"}
    result["ok"] = True
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
