"""Bounded, deterministic evaluation of versioned necessary-dependency graphs.

This module checks supplied metadata. It does not authenticate evidence, verify
the truth of a claim, or infer that a reported revision is a content digest.
Every parent is necessary: an invalid parent invalidates its descendants.
Callers should use separate nodes for different revisions of an artifact.

The entire graph is validated before evaluation, including disconnected nodes.
Malformed graphs raise ValueError; well-formed but stale, revoked, unknown or
revision-mismatched nodes produce invalid results. No input is mutated.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import re


MAX_NODES = 4096
MAX_EDGES = 16384
MAX_DEPTH = 1024
MAX_ROOTS = 128
MAX_ID_LENGTH = 128
MAX_REVISION_LENGTH = 256

_FIELDS = frozenset({
    "id", "revision", "expected_revision", "status", "observed_at",
    "expires_at", "parents",
})
_STATUSES = frozenset({"CURRENT", "REVOKED", "UNKNOWN"})
_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:\d{2})\Z"
)


@dataclass(frozen=True)
class _Node:
    identifier: str
    revision: str
    expected_revision: str
    status: str
    observed_at: datetime
    expires_at: datetime
    parents: tuple[str, ...]


def _string(value: object, label: str, limit: int) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    if not value or len(value) > limit or value != value.strip():
        raise ValueError(f"{label} must have 1..{limit} characters and no edge whitespace")
    if not value.isprintable():
        raise ValueError(f"{label} must contain only printable characters")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if type(value) is not str or len(value) > 40 or not _ISO.fullmatch(value):
        raise ValueError(f"{label} must be an aware ISO timestamp with seconds")
    # ISO permits offsets only through 23:59. datetime.fromisoformat normalizes
    # some out-of-range minute values, so enforce the written offset explicitly.
    if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError(f"{label} has an invalid UTC offset")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a valid aware ISO timestamp") from exc


def _now(value: object) -> datetime:
    if type(value) is str:
        return _timestamp(value, "now")
    if type(value) is not datetime:
        raise ValueError("now must be an aware datetime or ISO timestamp")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone aware")
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError("now must be a valid timezone-aware datetime") from exc


def _ids(values: object, label: str, limit: int) -> tuple[str, ...]:
    if type(values) is not list or len(values) > limit:
        raise ValueError(f"{label} must be a list with at most {limit} entries")
    identifiers = tuple(_string(item, f"{label} entry", MAX_ID_LENGTH) for item in values)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"{label} must not contain duplicate IDs")
    return tuple(sorted(identifiers))


def _graph(nodes: object) -> tuple[dict[str, _Node], dict[str, list[str]], tuple[str, ...]]:
    if type(nodes) is not list or len(nodes) > MAX_NODES:
        raise ValueError(f"nodes must be a list with at most {MAX_NODES} entries")
    by_id: dict[str, _Node] = {}
    edges = 0
    for raw in nodes:
        if type(raw) is not dict or set(raw) != _FIELDS:
            raise ValueError("each node must contain exactly the documented fields")
        identifier = _string(raw["id"], "node id", MAX_ID_LENGTH)
        if identifier in by_id:
            raise ValueError(f"duplicate node ID: {identifier}")
        revision = _string(raw["revision"], "revision", MAX_REVISION_LENGTH)
        expected_revision = _string(raw["expected_revision"], "expected_revision", MAX_REVISION_LENGTH)
        status = _string(raw["status"], "status", 16)
        if status not in _STATUSES:
            raise ValueError(f"invalid node status: {status}")
        observed_at = _timestamp(raw["observed_at"], "observed_at")
        expires_at = _timestamp(raw["expires_at"], "expires_at")
        if observed_at >= expires_at:
            raise ValueError("observed_at must be strictly earlier than expires_at")
        parents = _ids(raw["parents"], "parents", MAX_NODES)
        edges += len(parents)
        if edges > MAX_EDGES:
            raise ValueError(f"graph exceeds {MAX_EDGES} edges")
        by_id[identifier] = _Node(
            identifier, revision, expected_revision, status, observed_at, expires_at, parents
        )

    children: dict[str, list[str]] = {identifier: [] for identifier in by_id}
    pending: dict[str, int] = {}
    for identifier, node in by_id.items():
        pending[identifier] = len(node.parents)
        for parent in node.parents:
            if parent not in by_id:
                raise ValueError(f"dangling dependency: {identifier} -> {parent}")
            children[parent].append(identifier)
    for child_list in children.values():
        child_list.sort()

    # Kahn's algorithm avoids recursion and detects cycles anywhere in the graph.
    queue = [identifier for identifier, count in pending.items() if count == 0]
    heapq.heapify(queue)
    order: list[str] = []
    depths: dict[str, int] = {}
    while queue:
        identifier = heapq.heappop(queue)
        depth = 1 + max((depths[parent] for parent in by_id[identifier].parents), default=0)
        if depth > MAX_DEPTH:
            raise ValueError(f"graph exceeds dependency depth {MAX_DEPTH}")
        depths[identifier] = depth
        order.append(identifier)
        for child in children[identifier]:
            pending[child] -= 1
            if pending[child] == 0:
                heapq.heappush(queue, child)
    if len(order) != len(by_id):
        raise ValueError("dependency graph contains a cycle")
    return by_id, children, tuple(order)


def evaluate_lineage(nodes: list[dict], roots: list[str], *, now: datetime | str) -> dict:
    """Evaluate all necessary ancestors of each selected root.

    Return ``{"roots": {id: {"valid": bool, "reasons": [str, ...]}},
    "invalidated_ids": [str, ...]}``. Invalidated IDs cover *all* supplied nodes,
    including disconnected nodes. A root's reasons contain every local failure
    in its ancestor closure, formatted ``<id>:<code>``. Codes are ``revoked``,
    ``unknown_status``, ``revision_mismatch``, ``observed_in_future``, ``expired``.
    IDs, root keys and reasons are sorted for deterministic output.

    Validity is ``CURRENT and revision == expected_revision and
    observed_at <= now < expires_at and all(parents_valid)``. Empty graphs and
    root lists are allowed; an empty root set does not certify an action.
    Bounds: 4096 nodes, 16384 edges, 1024 depth, 128 selected roots. These limits
    are contract limits, not silent truncation. Neither evidence truth nor
    historical authorization is established by this metadata-only operation.
    """
    timestamp = _now(now)
    by_id, _children, order = _graph(nodes)
    selected = _ids(roots, "roots", MAX_ROOTS)
    if any(identifier not in by_id for identifier in selected):
        raise ValueError("roots contains an unknown node ID")

    local_reasons: dict[str, tuple[str, ...]] = {}
    valid: dict[str, bool] = {}
    for identifier in order:
        node = by_id[identifier]
        reasons: list[str] = []
        if node.status == "REVOKED":
            reasons.append(f"{identifier}:revoked")
        elif node.status == "UNKNOWN":
            reasons.append(f"{identifier}:unknown_status")
        if node.revision != node.expected_revision:
            reasons.append(f"{identifier}:revision_mismatch")
        if node.observed_at > timestamp:
            reasons.append(f"{identifier}:observed_in_future")
        if timestamp >= node.expires_at:
            reasons.append(f"{identifier}:expired")
        local_reasons[identifier] = tuple(reasons)
        valid[identifier] = not reasons and all(valid[parent] for parent in node.parents)

    root_results: dict[str, dict] = {}
    for root in selected:
        reasons: set[str] = set()
        pending = [root]
        seen: set[str] = set()
        while pending:
            identifier = pending.pop()
            if identifier in seen:
                continue
            seen.add(identifier)
            reasons.update(local_reasons[identifier])
            pending.extend(by_id[identifier].parents)
        root_results[root] = {"valid": valid[root], "reasons": sorted(reasons)}
    return {
        "roots": root_results,
        "invalidated_ids": sorted(identifier for identifier, is_valid in valid.items() if not is_valid),
    }


def affected(nodes: list[dict], changed_ids: list[str]) -> list[str]:
    """Return changed IDs and their transitive dependents, sorted and deduplicated.

    This is a structural invalidation/recomputation set, not a finding that
    these nodes are false or invalid. It validates the entire graph contract,
    but does not evaluate freshness because it has no clock argument.
    Unknown/duplicate changed IDs raise ValueError; an empty list returns [].
    """
    by_id, children, _order = _graph(nodes)
    changed = _ids(changed_ids, "changed_ids", MAX_NODES)
    if any(identifier not in by_id for identifier in changed):
        raise ValueError("changed_ids contains an unknown node ID")
    seen = set(changed)
    queue = deque(changed)
    while queue:
        identifier = queue.popleft()
        for child in children[identifier]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return sorted(seen)
