"""Keel security authority — data protection.

Deterministic sensitivity classification, redaction, and data-loss-prevention
policy for fields, values, and free text.  All rules fail closed: unknown
field names classify INTERNAL (never PUBLIC), unknown DLP destinations deny,
and no classification decision is ever made by an LLM.
"""
from . import dlp
from .classifier import (Sensitivity, classify_field, classify_text,
                         max_sensitivity)
from .redactor import redact

__all__ = ["Sensitivity", "classify_field", "classify_text", "max_sensitivity",
           "redact", "dlp"]
