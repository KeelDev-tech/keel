"""Persistent evidence projections; retrieval never grants execution authority."""
from .index import EvidenceIndex, IndexError, document_digest, text_digest

__all__ = ['EvidenceIndex', 'IndexError', 'document_digest', 'text_digest']
