"""Content classification: every byte is either trusted or it is not.

Job listings, webpages, PDFs, emails, application questions, resumes, ATS
responses, and scraped content are ALL untrusted data. Unknown source
types are untrusted too (fail closed). Only Keel's own system prompts,
operator messages, and versioned policy/config count as trusted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class Origin(Enum):
    TRUSTED_SYSTEM = "trusted_system"      # Keel system prompts, code
    TRUSTED_OPERATOR = "trusted_operator"  # Trent's own messages
    TRUSTED_POLICY = "trusted_policy"      # versioned policy/config
    UNTRUSTED_EXTERNAL = "untrusted_external"  # web, email, listings...
    UNTRUSTED_SCRAPED = "untrusted_scraped"    # scraper/ATS output


TRUSTED_ORIGINS = frozenset({Origin.TRUSTED_SYSTEM, Origin.TRUSTED_OPERATOR,
                             Origin.TRUSTED_POLICY})

# Source types that are untrusted by definition. Anything not listed here
# is ALSO untrusted (fail closed) — this table documents the known ones.
UNTRUSTED_SOURCE_TYPES = frozenset({
    "job_listing", "webpage", "pdf", "email", "application_question",
    "resume", "ats_response", "scraped_content", "aggregator_listing",
    "form_html", "confirmation_page", "api_response",
})

TRUSTED_SOURCE_TYPES = frozenset({
    "system_prompt", "operator_message", "policy_document",
    "engine_config", "security_rule",
})


def classify_source(source_type: str) -> Origin:
    """Map a source-type label to an Origin. Unknown -> UNTRUSTED."""
    st = (source_type or "").strip().lower()
    if st in TRUSTED_SOURCE_TYPES:
        if st == "operator_message":
            return Origin.TRUSTED_OPERATOR
        if st in ("policy_document", "engine_config", "security_rule"):
            return Origin.TRUSTED_POLICY
        return Origin.TRUSTED_SYSTEM
    # Everything else — including unknown labels — is untrusted.
    if st in UNTRUSTED_SOURCE_TYPES:
        return Origin.UNTRUSTED_EXTERNAL
    return Origin.UNTRUSTED_EXTERNAL


@dataclass
class Content:
    """A classified blob of text with its trust origin attached."""
    text: str
    origin: Origin
    source_type: str = ""
    source_label: str = ""
    sha256: str = field(init=False)

    def __post_init__(self):
        self.sha256 = hashlib.sha256(
            self.text.encode("utf-8", errors="replace")).hexdigest()

    @classmethod
    def from_source(cls, text: str, source_type: str,
                    source_label: str = "") -> "Content":
        return cls(text=text or "", origin=classify_source(source_type),
                   source_type=source_type, source_label=source_label)

    def is_trusted(self) -> bool:
        return self.origin in TRUSTED_ORIGINS

    def to_dict(self) -> dict:
        return {"origin": self.origin.value, "source_type": self.source_type,
                "source_label": self.source_label, "sha256": self.sha256,
                "length": len(self.text)}
