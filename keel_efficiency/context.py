"""Bounded, revision-bound context assembled only from caller-supplied data.

Packets contain evidence, not authority. No filesystem or network retrieval is
performed. Token counts are deliberately labelled byte-based estimates rather
than model-tokenizer measurements; use a provider tokenizer before dispatch.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Mapping, Sequence


class ContextError(ValueError):
    """Malformed context input (as opposed to a valid request that is held)."""


def _text(value: object, name: str, limit: int = 262144) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ContextError(f"{name} must be a nonempty bounded string")
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _budget(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 16 * 1024 * 1024:
        raise ContextError("max_bytes must be an integer in 1..16777216")
    return value


@dataclass(frozen=True)
class Binding:
    source_id: str
    revision: str

    def __post_init__(self):
        _text(self.source_id, "source_id", 256)
        _text(self.revision, "revision", 256)


@dataclass(frozen=True)
class ContextSource:
    source_id: str
    revision: str
    content: str
    kind: str = "document"
    keywords: tuple[str, ...] = ()
    dependencies: tuple[Binding, ...] = ()
    provenance_refs: tuple[str, ...] = ()
    required: bool = False

    def __post_init__(self):
        Binding(self.source_id, self.revision)
        _text(self.content, "content")
        if self.kind not in {"document", "skill", "persona", "evidence", "constraint", "handoff"}:
            raise ContextError("unknown source kind")
        if type(self.required) is not bool:
            raise ContextError("required must be boolean")
        for name in ("keywords", "dependencies", "provenance_refs"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or len(values) > 128:
                raise ContextError(f"{name} must be a bounded sequence")
            object.__setattr__(self, name, tuple(values))
        for keyword in self.keywords:
            _text(keyword, "keyword", 256)
        for ref in self.provenance_refs:
            _text(ref, "provenance reference", 2048)
        if any(not isinstance(dep, Binding) for dep in self.dependencies):
            raise ContextError("dependencies must contain Binding objects")
        if len({dep.source_id for dep in self.dependencies}) != len(self.dependencies):
            raise ContextError("duplicate dependency")

    def record(self) -> dict:
        result = asdict(self)
        result["content_sha256"] = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        return json.loads(_json(result))


@dataclass(frozen=True)
class ContextResult:
    status: str
    reason: str
    packet: dict | None
    encoded: bytes
    included: tuple[str, ...]
    omitted: tuple[str, ...]
    byte_count: int
    estimated_token_upper: int
    token_estimate_method: str = "utf8_bytes_proxy_not_measured_tokens"
    execution_authorized: bool = False


def _hold(reason: str, ids: Sequence[str] = ()) -> ContextResult:
    return ContextResult("HOLD", reason, None, b"", (), tuple(sorted(ids)), 0, 0)


class ContextAssembler:
    """Deterministic relevance selection with indivisible dependency closures."""

    def assemble(self, *, task: str, sources: Sequence[ContextSource],
                 required_constraints: Sequence[str], max_bytes: int,
                 current_revisions: Mapping[str, str]) -> ContextResult:
        _text(task, "task", 65536)
        _budget(max_bytes)
        if not isinstance(sources, (tuple, list)) or len(sources) > 2048:
            raise ContextError("sources must be a sequence of at most 2048 objects")
        if any(not isinstance(source, ContextSource) for source in sources):
            raise ContextError("sources must contain ContextSource objects")
        if not isinstance(required_constraints, (tuple, list)) or len(required_constraints) > 128:
            raise ContextError("required_constraints must be a bounded sequence")
        for constraint in required_constraints:
            _text(constraint, "required constraint", 8192)
        if not isinstance(current_revisions, Mapping):
            raise ContextError("current_revisions must be a mapping")
        by_id = {source.source_id: source for source in sources}
        if len(by_id) != len(sources):
            raise ContextError("duplicate source ID")

        # Reject ambiguous revisions even for omitted sources. Never silently use
        # a stale optional source to fill an otherwise valid packet.
        for source in sources:
            if current_revisions.get(source.source_id) != source.revision:
                return _hold("STALE_OR_UNBOUND_SOURCE:" + source.source_id, by_id)
            for dep in source.dependencies:
                if dep.source_id not in by_id or by_id[dep.source_id].revision != dep.revision:
                    return _hold("MISSING_OR_STALE_DEPENDENCY:" + dep.source_id, by_id)

        def closure(ids: set[str]) -> set[str]:
            result = set(ids)
            pending = list(ids)
            while pending:
                for dep in by_id[pending.pop()].dependencies:
                    if dep.source_id not in result:
                        result.add(dep.source_id)
                        pending.append(dep.source_id)
            return result

        records = {}
        record_sizes = {}
        supplied_bytes = 0
        for source in sources:
            record = source.record()
            size = len(_json(record))
            supplied_bytes += size
            if supplied_bytes > 16 * 1024 * 1024:
                raise ContextError("supplied source objects exceed 16 MiB")
            records[source.source_id] = record
            record_sizes[source.source_id] = size

        def packet(ids: set[str]) -> dict:
            return {"schema": "keel.efficiency.context.v1", "task": task,
                    "required_constraints": list(required_constraints),
                    "sources": [records[key] for key in sorted(ids)],
                    "execution_authorized": False, "approval_reusable": False,
                    "token_estimate_method": "utf8_bytes_proxy_not_measured_tokens"}

        selected = closure({s.source_id for s in sources if s.required or s.kind == "constraint"})
        base_bytes = len(_json(packet(set())))

        def packet_size(ids: set[str]) -> int:
            return base_bytes + sum(record_sizes[key] for key in ids) + max(0, len(ids) - 1)

        if packet_size(selected) > max_bytes:
            return _hold("REQUIRED_CONTEXT_EXCEEDS_BUDGET", by_id)
        words = set(re.findall(r"\w+", task.casefold()))

        def relevance(source: ContextSource) -> int:
            keywords = set(re.findall(r"\w+", " ".join(source.keywords).casefold()))
            content = set(re.findall(r"\w+", source.content.casefold()))
            return 4 * len(words & keywords) + len(words & content)

        scores = {source.source_id: relevance(source) for source in sources}
        candidates = sorted(sources, key=lambda s: (-scores[s.source_id], s.source_id))
        for source in candidates:
            if source.source_id in selected or scores[source.source_id] == 0:
                continue
            trial = closure(selected | {source.source_id})
            if packet_size(trial) <= max_bytes:
                selected = trial
        result = packet(selected)
        encoded = _json(result)
        # Most common tokenizers cannot produce more tokens than UTF-8 bytes;
        # this remains an estimate, never a billable usage measurement.
        return ContextResult("READY", "BOUNDED_CONTEXT", result, encoded, tuple(sorted(selected)),
                             tuple(sorted(set(by_id) - selected)), len(encoded), len(encoded))


def compact_handoff(*, goal: str, status: str, findings: Sequence[str],
                    next_steps: Sequence[str], required_constraints: Sequence[str],
                    bindings: Sequence[Binding], max_bytes: int) -> ContextResult:
    """Create an indivisible structured handoff; never truncate constraints.

    Findings and next steps must already be concise caller-supplied statements.
    Oversize handoffs HOLD instead of silently summarizing away evidence.
    """
    _budget(max_bytes)
    _text(goal, "goal", 65536)
    if status not in {"IN_PROGRESS", "COMPLETE", "BLOCKED"}:
        raise ContextError("unknown handoff status")
    for name, values in (("findings", findings), ("next_steps", next_steps),
                         ("required_constraints", required_constraints)):
        if not isinstance(values, (tuple, list)) or len(values) > 128:
            raise ContextError(f"{name} must be a bounded sequence")
        for value in values:
            _text(value, name, 8192)
    if not isinstance(bindings, (tuple, list)) or len(bindings) > 2048:
        raise ContextError("bindings must be a bounded sequence")
    if any(not isinstance(binding, Binding) for binding in bindings):
        raise ContextError("bindings must contain Binding objects")
    if len({b.source_id for b in bindings}) != len(bindings):
        raise ContextError("duplicate handoff binding")
    result = {"schema": "keel.efficiency.handoff.v1", "goal": goal, "status": status,
              "findings": list(findings), "next_steps": list(next_steps),
              "required_constraints": list(required_constraints),
              "bindings": [asdict(b) for b in sorted(bindings, key=lambda b: b.source_id)],
              "execution_authorized": False, "approval_reusable": False}
    encoded = _json(result)
    if len(encoded) > max_bytes:
        return _hold("HANDOFF_EXCEEDS_BUDGET", [b.source_id for b in bindings])
    return ContextResult("READY", "STRUCTURED_HANDOFF", result, encoded,
                         tuple(sorted(b.source_id for b in bindings)), (), len(encoded), len(encoded))
