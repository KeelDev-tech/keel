"""Prompt-injection firewall: deterministic pattern scanner.

Scans untrusted content for direct and indirect injection, encoded
payloads, secret requests, tool-invocation attempts, suspicious URLs,
hidden-DOM markers, policy-modification attempts, fake approvals, and
confused-deputy phrasing. Deterministic regex + decoding — no LLM in the
detection path, so the scanner itself cannot be prompt-injected.

Encoded payloads are decoded (base64 / hex / \\uXXXX escapes) and the
decoded bytes are re-scanned: an instruction does not become safe by
being encoded.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field

SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass
class Finding:
    pattern_id: str
    severity: str  # LOW | MEDIUM | HIGH
    excerpt: str   # redacted, truncated context around the match
    offset: int

    def to_dict(self) -> dict:
        return {"pattern_id": self.pattern_id, "severity": self.severity,
                "excerpt": self.excerpt, "offset": self.offset}


@dataclass
class ScanResult:
    findings: list = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.findings

    @property
    def highest_severity(self) -> str | None:
        if not self.findings:
            return None
        return max(self.findings,
                   key=lambda f: SEVERITY_ORDER[f.severity]).severity

    def to_dict(self) -> dict:
        return {"clean": self.is_clean,
                "highest_severity": self.highest_severity,
                "findings": [f.to_dict() for f in self.findings]}


# (pattern_id, severity, compiled_regex)
_PATTERNS: list[tuple[str, str, re.Pattern]] = []


def _p(pid: str, severity: str, rx: str, flags=re.IGNORECASE):
    _PATTERNS.append((pid, severity, re.compile(rx, flags)))


# --- instruction-override attempts (direct + common variants) ---
_p("ignore_previous_instructions", "HIGH",
   r"ignore\s+(all\s+|any\s+|your\s+)?(previous|prior|earlier)\s+instructions?")
_p("disregard_instructions", "HIGH",
   r"disregard\s+(all\s+|any\s+|your\s+|the\s+)?instructions?")
_p("forget_system_prompt", "HIGH",
   r"(forget|discard|drop)\s+(your\s+)?(system\s+prompt|system\s+instructions|training)")
_p("override_instructions", "HIGH",
   r"(override|supersede|replace)\s+(your\s+|these\s+)?instructions?")
_p("ai_assistant_roleplay", "HIGH",
   r"(you\s+are\s+now|from\s+now\s+on\s+you\s+are|act\s+as\s+(if\s+)?you\s+are)\s+"
   r"(an?\s+)?(ai|assistant|agent|system)\s+(without|with\s+no)\s+")
_p("jailbreak_markers", "HIGH",
   r"\b(DAN|developer\s+mode|jailbreak|do\s+anything\s+now)\b")

# --- policy-modification attempts ---
_p("policy_modification", "HIGH",
   r"(update|change|modify|disable|remove|bypass)\s+(your\s+|the\s+)?"
   r"(security\s+policy|policy|guardrails|restrictions|content\s+filter|safety)")
_p("standing_order_injection", "HIGH",
   r"(new\s+standing\s+order|standing\s+order\s*:|as\s+a\s+standing\s+rule)")

# --- secret / credential requests ---
_p("secret_request", "HIGH",
   r"(reveal|disclose|send|share|print|output|show)\s+(me\s+|us\s+|your\s+|the\s+)?"
   r"(your\s+|the\s+)?(system\s+prompt|api[\s_-]?key|password|secret|credentials?|"
   r"session\s+token|private\s+key|auth\s+token)")
_p("prompt_extraction", "HIGH",
   r"(what\s+(is|are)\s+your\s+(system\s+)?(prompt|instructions)|"
   r"repeat\s+(your\s+)?(system\s+)?(prompt|instructions))")

# --- tool-invocation requests from data ---
_p("tool_invocation", "MEDIUM",
   r"(\[TOOL_CALL\]|\[SYSTEM\]|\[ADMIN\]|"
   r"execute\s+(the\s+following\s+)?command|run\s+the\s+(shell\s+)?command|"
   r"call\s+the\s+tool|invoke\s+[a-z_]+\s*\()")
_p("shell_command_block", "MEDIUM",
   r"(^|\n)\s*(rm\s+-rf|curl\s+.*\|\s*sh|wget\s+.*\|\s*sh|chmod\s+\+x\s+/tmp/)")

# --- confused deputy / authority spoofing ---
_p("confused_deputy", "MEDIUM",
   r"(on\s+behalf\s+of|as)\s+the\s+(security|policy)\s+(agent|engine|authority)")
_p("fake_approval", "MEDIUM",
   r"(pre-?approved|auto-?approved|already\s+approved)\s+(by\s+\w+\s+)?"
   r"(skip|bypass|without)\s+(checks?|review|approval)|"
   r"approval\s*:\s*(auto-?granted|bypassed|waived)")

# --- suspicious URLs ---
_p("suspicious_url", "MEDIUM",
   r"https?://\d{1,3}(\.\d{1,3}){3}(:\d+)?(/|\b)|"          # IP host
   r"https?://[^/\s]*xn--[^/\s]*|"                          # punycode
   r"https?://[^/\s]*@[^/\s]*|"                            # userinfo trick
   r"https?://[^/\s]*(login|secure|verify|account)[^/\s]*\."
   r"(tk|ml|ga|cf|gq|pw|top|xyz|club)(/|\b)")               # shady TLD + lure

# --- hidden-DOM / steganographic markers ---
_p("hidden_dom", "LOW",
   r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(px)?|"
   r"color\s*:\s*#(fff|ffffff)\s*;\s*background\s*:\s*#(fff|ffffff)")
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def _excerpt(text: str, start: int, end: int, width: int = 60) -> str:
    s = max(0, start - width)
    e = min(len(text), end + width)
    frag = text[s:e].replace("\n", " ")
    return frag[: 2 * width + 40]


def _scan_patterns(text: str, findings: list[Finding],
                   tag: str = "") -> None:
    for pid, severity, rx in _PATTERNS:
        for m in rx.finditer(text):
            findings.append(Finding(
                pattern_id=f"{pid}{tag}", severity=severity,
                excerpt=_excerpt(text, m.start(), m.end()),
                offset=m.start()))
    if _ZERO_WIDTH.search(text):
        m = _ZERO_WIDTH.search(text)
        findings.append(Finding(
            pattern_id=f"zero_width_chars{tag}", severity="LOW",
            excerpt=_excerpt(text, m.start(), m.end()), offset=m.start()))


# Encoded-blob candidates: long base64 / hex runs, \uXXXX escape runs.
_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")
_HEX_RE = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{2}){32,}(?![0-9a-fA-F])")
_UNI_RE = re.compile(r"(?:\\u[0-9a-fA-F]{4}){8,}")


def _try_b64(blob: str) -> str | None:
    try:
        padded = blob + "=" * (-len(blob) % 4)
        raw = base64.b64decode(padded, validate=True)
        txt = raw.decode("utf-8", errors="strict")
        if sum(c.isprintable() or c.isspace() for c in txt) / max(len(txt), 1) > 0.8:
            return txt
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    return None


def _try_hex(blob: str) -> str | None:
    try:
        txt = bytes.fromhex(blob).decode("utf-8", errors="strict")
        if sum(c.isprintable() or c.isspace() for c in txt) / max(len(txt), 1) > 0.8:
            return txt
    except (ValueError, UnicodeDecodeError):
        pass
    return None


def _try_unicode_escapes(blob: str) -> str | None:
    try:
        return blob.encode("utf-8").decode("unicode_escape")
    except (ValueError, UnicodeDecodeError):
        return None


def scan(text: str) -> ScanResult:
    """Scan text for injection payloads. Deterministic; no network; no LLM."""
    text = text or ""
    findings: list[Finding] = []
    _scan_patterns(text, findings)
    # Decode-and-rescan: encoded instructions are still instructions.
    for blob in _B64_RE.findall(text):
        dec = _try_b64(blob)
        if dec:
            findings.append(Finding("encoded_payload_base64", "MEDIUM",
                                    _excerpt(text, text.index(blob),
                                             text.index(blob) + len(blob)),
                                    text.index(blob)))
            _scan_patterns(dec, findings, tag=":decoded_base64")
    for blob in _HEX_RE.findall(text):
        dec = _try_hex(blob)
        if dec:
            findings.append(Finding("encoded_payload_hex", "MEDIUM",
                                    _excerpt(text, text.index(blob),
                                             text.index(blob) + len(blob)),
                                    text.index(blob)))
            _scan_patterns(dec, findings, tag=":decoded_hex")
    for blob in _UNI_RE.findall(text):
        dec = _try_unicode_escapes(blob)
        if dec and dec != blob:
            _scan_patterns(dec, findings, tag=":decoded_unicode")
    # Deduplicate identical (pattern_id, offset) pairs.
    seen = set()
    unique = []
    for f in findings:
        key = (f.pattern_id, f.offset)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    unique.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.offset),
                reverse=True)
    return ScanResult(unique)
