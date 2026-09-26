"""Deterministic classification of outbound material.

Exactly nine classes (no other value is valid). Classification is structural
and conservative: any detection of personal data or secrets OVERRIDES the
structural guess, because the riskiest content present decides the class.

    external_egress is ALWAYS "DENY" at classification time. No classification
    can authorize egress; only a verified counsel decision (counsel_decision +
    publication_guard) can lift the deny for a specific artifact digest.

This module performs no network I/O and holds no credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

EGRESS_DENY = "DENY"


class ArtifactClass(str, Enum):
    SOURCE_CODE = "SOURCE_CODE"
    DOCUMENTATION = "DOCUMENTATION"
    AGGREGATE_METRICS = "AGGREGATE_METRICS"
    TELEMETRY = "TELEMETRY"
    APPLICATION_DATA = "APPLICATION_DATA"
    SECURITY_EVIDENCE = "SECURITY_EVIDENCE"
    PERSONAL_DATA = "PERSONAL_DATA"
    SECRETS = "SECRETS"
    DERIVED_DATA = "DERIVED_DATA"


VALID_CLASSES = {c.value for c in ArtifactClass}

# ---------------------------------------------------------------------------
# Structural (extension / media-type) classification rules.
# ---------------------------------------------------------------------------

_EXTENSION_MAP: dict[str, ArtifactClass] = {
    # source code
    ".py": ArtifactClass.SOURCE_CODE, ".js": ArtifactClass.SOURCE_CODE,
    ".ts": ArtifactClass.SOURCE_CODE, ".go": ArtifactClass.SOURCE_CODE,
    ".rs": ArtifactClass.SOURCE_CODE, ".java": ArtifactClass.SOURCE_CODE,
    ".c": ArtifactClass.SOURCE_CODE, ".cpp": ArtifactClass.SOURCE_CODE,
    ".h": ArtifactClass.SOURCE_CODE, ".sh": ArtifactClass.SOURCE_CODE,
    ".rb": ArtifactClass.SOURCE_CODE, ".php": ArtifactClass.SOURCE_CODE,
    ".swift": ArtifactClass.SOURCE_CODE, ".kt": ArtifactClass.SOURCE_CODE,
    # documentation
    ".md": ArtifactClass.DOCUMENTATION, ".rst": ArtifactClass.DOCUMENTATION,
    ".txt": ArtifactClass.DOCUMENTATION, ".pdf": ArtifactClass.DOCUMENTATION,
    ".docx": ArtifactClass.DOCUMENTATION, ".html": ArtifactClass.DOCUMENTATION,
    # application data / telemetry-shaped data
    ".json": ArtifactClass.APPLICATION_DATA, ".jsonl": ArtifactClass.TELEMETRY,
    ".csv": ArtifactClass.APPLICATION_DATA, ".parquet": ArtifactClass.APPLICATION_DATA,
    ".ndjson": ArtifactClass.TELEMETRY, ".log": ArtifactClass.TELEMETRY,
    ".db": ArtifactClass.APPLICATION_DATA, ".sqlite": ArtifactClass.APPLICATION_DATA,
}

_FILENAME_HINTS: list[tuple[re.Pattern, ArtifactClass]] = [
    (re.compile(r"metric|aggregate|summary|rollup|kpi", re.I), ArtifactClass.AGGREGATE_METRICS),
    (re.compile(r"telemetry|event[-_ ]?log|trace", re.I), ArtifactClass.TELEMETRY),
    (re.compile(r"audit|evidence|attestation|sbom|scan[-_ ]?report", re.I), ArtifactClass.SECURITY_EVIDENCE),
    (re.compile(r"derived|feature[-_ ]?store|embedding|model[-_ ]?output", re.I), ArtifactClass.DERIVED_DATA),
]

# ---------------------------------------------------------------------------
# Content scans. Conservative: a hit promotes the classification upward.
# Findings never carry raw values — only masked samples.
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")),
    ("ssn_like", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("dob_like", re.compile(r"(?i)\b(?:dob|date of birth|birthdate)\b\s*[:=]\s*\S+")),
    ("street_address_like", re.compile(r"\b\d{1,5}\s+[A-Za-z0-9.'\- ]+\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Terrace|Ter)\b", re.I)),
]

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b")),
    ("generic_api_key", re.compile(r"(?i)\bapi[_-]?key\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}['\"]?")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.~+/=]{16,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("password_assignment", re.compile(r"(?i)\bpassword\b\s*[:=]\s*['\"]?\S{6,}['\"]?")),
    ("db_connection_secret", re.compile(r"(?i)\b(?:postgres|mysql|mongodb)(?:\+srv)?://[^/\s:]+:[^/\s@]+@")),
]

_MAX_SCAN_BYTES = 2 * 1024 * 1024  # scan first 2 MiB of text-decodable content


def _mask(value: str) -> str:
    """Mask a matched value: keep shape, drop content."""
    if len(value) <= 6:
        return "***"
    return value[:2] + "***" + value[-2:]


@dataclass
class ScanFinding:
    pattern: str
    count: int
    masked_samples: list[str] = field(default_factory=list)


@dataclass
class ClassificationResult:
    artifact_class: str
    external_egress: str = EGRESS_DENY
    reasons: list[str] = field(default_factory=list)
    pii_findings: list[ScanFinding] = field(default_factory=list)
    secret_findings: list[ScanFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "artifact_class": self.artifact_class,
            "external_egress": self.external_egress,
            "reasons": list(self.reasons),
            "pii_findings": [f.__dict__ for f in self.pii_findings],
            "secret_findings": [f.__dict__ for f in self.secret_findings],
        }


def _scan_text(text: str, patterns: list[tuple[str, re.Pattern]]) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for name, pat in patterns:
        matches = pat.findall(text)
        if matches:
            uniq = []
            for m in matches:
                s = m if isinstance(m, str) else m[0]
                ms = _mask(s)
                if ms not in uniq:
                    uniq.append(ms)
                if len(uniq) >= 3:
                    break
            findings.append(ScanFinding(pattern=name, count=len(matches),
                                        masked_samples=uniq))
    return findings


def scan_content(data: bytes) -> tuple[list[ScanFinding], list[ScanFinding]]:
    """Return (pii_findings, secret_findings) for byte content."""
    try:
        text = data[:_MAX_SCAN_BYTES].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return [], []  # binary: no text scan; structural class stands
    return (_scan_text(text, _PII_PATTERNS), _scan_text(text, _SECRET_PATTERNS))


def classify(name: str, media_type: str = "",
             content: bytes | None = None) -> ClassificationResult:
    """Deterministically classify one artifact.

    Priority: SECRETS > PERSONAL_DATA > filename hints > extension map >
    media-type fallback > UNCLASSIFIED(->APPLICATION_DATA fallback).

    Unclassifiable material defaults to APPLICATION_DATA with a reason noting
    the fallback, and external_egress stays DENY in all cases.
    """
    reasons: list[str] = []
    pii_findings: list[ScanFinding] = []
    secret_findings: list[ScanFinding] = []

    if content is not None:
        pii_findings, secret_findings = scan_content(content)

    if secret_findings:
        reasons.append("secret-pattern content scan hit -> SECRETS (override)")
        return ClassificationResult(ArtifactClass.SECRETS.value, reasons=reasons,
                                    pii_findings=pii_findings,
                                    secret_findings=secret_findings)
    if pii_findings:
        reasons.append("personal-data content scan hit -> PERSONAL_DATA (override)")
        return ClassificationResult(ArtifactClass.PERSONAL_DATA.value, reasons=reasons,
                                    pii_findings=pii_findings,
                                    secret_findings=secret_findings)

    stem = Path(name).name
    for pat, cls in _FILENAME_HINTS:
        if pat.search(stem):
            reasons.append(f"filename hint matched {pat.pattern!r} -> {cls.value}")
            return ClassificationResult(cls.value, reasons=reasons)

    ext = Path(name).suffix.lower()
    if ext in _EXTENSION_MAP:
        cls = _EXTENSION_MAP[ext]
        reasons.append(f"extension {ext!r} -> {cls.value}")
        return ClassificationResult(cls.value, reasons=reasons)

    mt = (media_type or "").lower()
    if mt.startswith("text/"):
        reasons.append(f"media-type {media_type!r} -> DOCUMENTATION")
        return ClassificationResult(ArtifactClass.DOCUMENTATION.value, reasons=reasons)

    reasons.append("no structural rule matched -> APPLICATION_DATA (conservative fallback)")
    return ClassificationResult(ArtifactClass.APPLICATION_DATA.value, reasons=reasons)


def assert_valid_class(value: str) -> str:
    if value not in VALID_CLASSES:
        raise ValueError(
            f"invalid artifact class {value!r}; must be one of {sorted(VALID_CLASSES)}")
    return value
