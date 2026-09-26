#!/usr/bin/env python3
"""Scoped answer reuse with the compound-growth property (2026-09-21).

bank_scope.py governs WRITES (which scope a new entry gets).
answer_resolver.py governs single READS (scope-verified, fail-closed).
This module governs REUSE: resolving many banked answers for ONE
employer/role inside a session where

  1. the employer scope is verified before EVERY reuse — a banked answer
     applies only within its recorded employer scope unless explicitly
     global (delegation to answer_resolver.resolve; this module never
     re-implements scope logic);
  2. each reuse is logged as evidence — an in-memory, caller-owned
     ReuseEvidence record per reuse (key, status, scope, source,
     employer, timestamp). No queue / ledger / telemetry writes: a
     per-reuse durable write would re-spend the cost the reuse saves,
     and this module's task boundary is code+tests only;
  3. the compound-growth property holds — the first answer costs once
     (bank load + exactly one employer-registry snapshot at session
     construction); every reuse after that is a pure in-memory scope
     check with ZERO per-reuse I/O. No per-reuse re-verification loops.

The cost leak this closes (measured 2026-09-21): answer_resolver's
legacy free-text path called known_employers() — three queue JSON files
re-parsed from disk — on EVERY resolve(). Ten legacy resolves cost
~5.0s; ten machine-scope resolves cost ~0.0s. Reusing N legacy answers
re-spent the registry-read cost N times: linear, not compounding.
ReuseSession snapshots the registry once; reuses cost nothing but the
in-memory check.

Deliberately EXCLUDED (irreconcilable with the compound-growth
directive, 2026-09-16):
  - per-reuse re-verification against the applicant (re-asking the tray
    per reuse re-spends the saved cost on every reuse);
  - per-reuse durable writes (telemetry/ledger/queue) — cost per reuse
    plus log pollution; evidence stays in-memory and caller-owned;
  - re-deriving scope at reuse time (scope was decided once at bank
    time and stored; re-deriving is redundant compute and can flip-flop
    if classifiers change — the recorded machine scope is authoritative);
  - broadening employer-scoped answers to other employers ("reuse after
    N successes") — an integrity weakening, not a cost question; the
    resolver's rule 4 stays absolute.

Fail-closed: any exception inside reuse() yields an ABSTAIN Resolution
(never an invented answer) and is still evidence-logged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import answer_resolver as _resolver

try:
    from bank_scope import known_employers as _known_employers
except Exception:  # pragma: no cover — fail-safe
    def _known_employers():  # type: ignore
        return set()


@dataclass(frozen=True)
class ReuseEvidence:
    """One reuse's authority evidence (metadata only — never the value).

    The answer VALUE is deliberately absent: evidence proves WHICH answer
    was reused under WHAT authority, for audit/briefing; values travel in
    the Resolution, not in logs that may be surfaced.
    """
    seq: int            # 1-based order within the session
    key: str            # bank key that was reused (or attempted)
    status: str         # resolved | abstain | legacy
    scope: str          # scope the resolution was authorized under
    source: str         # provenance / authority text (<=500 chars)
    employer: str       # requesting employer the reuse was verified for
    reused_at: str      # UTC ISO timestamp of the reuse
    reason: str = ""    # why abstained / why flagged


class ReuseSession:
    """One employer/role's scoped-reuse session.

    Construction performs the session's ONLY I/O: a single
    known_employers() snapshot. Every reuse() after that is pure
    in-memory work through answer_resolver.resolve() with the snapshot
    registry — employer scope verified per reuse, evidence logged per
    reuse, zero per-reuse re-verification cost.
    """

    def __init__(self, answers, employer=None, role_context=None):
        # TOCTOU-hardened bindings: snapshot to plain strings at entry,
        # mirroring answer_resolver.resolve(). Caller-side mutation of the
        # inputs after construction cannot alter any reuse in the session.
        try:
            self._answers = dict(answers or {})
        except Exception:
            self._answers = {}
        self._employer = str(employer or "")
        self._role_ctx = {"role_id": "", "company": ""}
        try:
            rctx = role_context or {}
            self._role_ctx["role_id"] = str(rctx.get("role_id") or "")
            _ce = rctx.get("company") or rctx.get("employer") or ""
            self._role_ctx["company"] = str(_ce)
        except Exception:
            pass
        # The session's single registry load (the "first answer costs
        # once" half of the compound property). Fail-safe: an unreadable
        # registry degrades legacy name detection toward abstain, never
        # toward an unauthorized resolve — the snapshot is a detection
        # aid, never authority (answer_resolver's trust boundary).
        try:
            self._registry = set(_known_employers() or set())
        except Exception:
            self._registry = set()
        self._registry_loads = 1
        self._evidence: list[ReuseEvidence] = []
        self._seq = 0
        self._counts = {"resolved": 0, "abstain": 0, "legacy": 0}

    # ------------------------------------------------------------- reuse

    def reuse(self, key) -> "_resolver.Resolution":
        """Reuse one banked answer for this session's employer/role.

        Returns the answer_resolver Resolution (scope-verified, fail-closed)
        and appends a ReuseEvidence record. Never raises: any internal
        error yields ABSTAIN and is evidence-logged as such.
        """
        k = str(key or "")
        try:
            res = _resolver.resolve(
                k, self._answers.get(k),
                employer=self._employer,
                role_context=dict(self._role_ctx),
                registry=self._registry)
        except Exception as exc:  # pragma: no cover — total function
            res = _resolver.Resolution(
                key=k, status=_resolver.STATUS_ABSTAIN,
                reason=f"reuse session error (fail-closed): {exc}")
        self._seq += 1
        try:
            self._evidence.append(ReuseEvidence(
                seq=self._seq,
                key=res.key,
                status=res.status,
                scope=res.scope,
                source=res.source,
                employer=self._employer,
                reused_at=datetime.now(timezone.utc).isoformat(),
                reason=res.reason))
        except Exception:
            pass  # evidence logging never breaks a reuse
        if res.status in self._counts:
            self._counts[res.status] += 1
        return res

    def reuse_many(self, keys) -> dict:
        """Reuse several keys; returns {key: Resolution}. Fail-safe."""
        out = {}
        try:
            for k in keys or ():
                out[str(k)] = self.reuse(k)
        except Exception:
            pass
        return out

    # ---------------------------------------------------------- evidence

    def evidence(self) -> tuple:
        """Caller-owned copy of the per-reuse evidence log (immutable)."""
        return tuple(self._evidence)

    def cost_report(self) -> dict:
        """The compound-growth accounting for this session.

        registry_loads is ALWAYS 1 no matter how many reuses ran: the
        first answer paid for the snapshot, every reuse after that was
        free of I/O. A session where per-reuse cost grew with reuse count
        would fail the compound-growth directive — this report is how a
        caller proves it didn't.
        """
        return {
            "registry_loads": self._registry_loads,
            "reuses": self._seq,
            "resolved": self._counts["resolved"],
            "abstained": self._counts["abstain"],
            "legacy": self._counts["legacy"],
            "evidence_entries": len(self._evidence),
        }
