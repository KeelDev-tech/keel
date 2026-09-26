"""Derivative-data analysis for the Privacy Release Controller.

Covers AGGREGATE_METRICS and DERIVED_DATA artifacts (and any artifact whose
bytes were computed from other data). Deterministic checks:

  - aggregation methodology is stated explicitly (never "various");
  - contribution bounds are finite and stated (max per-unit contribution);
  - small-cell / differencing analysis: buckets below the suppression
    threshold are flagged, unbounded contributions are flagged, and any
    differencing risk across releases is noted as an open question when the
    release series is not provided.

This module does not claim anonymization. It reports the facts counsel needs
to judge re-identification risk; "anonymous" appears only inside quoted
limitations, never as a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SUPPRESSION_THRESHOLD = 5


@dataclass
class DerivativeAnalysis:
    artifact_id: str
    methodology: dict                    # {"function": ..., "grouping_keys": [...], ...}
    contribution_bounds: dict            # {"max_per_unit": N, "bound_method": ...}
    bucket_sizes: list[int] = field(default_factory=list)  # n per published bucket
    suppression_threshold: int = DEFAULT_SUPPRESSION_THRESHOLD
    release_series_context: str = ""     # how this release relates to prior ones
    verdict: str = "PENDING"             # set by analyze()
    findings: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "aggregation_methodology": dict(self.methodology),
            "contribution_bounds": dict(self.contribution_bounds),
            "suppression_threshold": self.suppression_threshold,
            "bucket_sizes": list(self.bucket_sizes),
            "release_series_context": self.release_series_context,
            "verdict": self.verdict,
            "findings": list(self.findings),
            "open_questions": list(self.open_questions),
        }


def analyze_derivative(artifact_id: str,
                       methodology: dict,
                       contribution_bounds: dict,
                       bucket_sizes: list[int] | None = None,
                       suppression_threshold: int = DEFAULT_SUPPRESSION_THRESHOLD,
                       release_series_context: str = "") -> DerivativeAnalysis:
    """Run the deterministic derivative-data checks."""
    analysis = DerivativeAnalysis(
        artifact_id=artifact_id,
        methodology=dict(methodology or {}),
        contribution_bounds=dict(contribution_bounds or {}),
        bucket_sizes=list(bucket_sizes or []),
        suppression_threshold=suppression_threshold,
        release_series_context=release_series_context,
    )
    f = analysis.findings
    q = analysis.open_questions

    # 1. Methodology must be explicit.
    if not analysis.methodology.get("function"):
        f.append("aggregation function is unstated (methodology.function missing)")
        q.append("What exact aggregation function produced these values?")
    if not analysis.methodology.get("grouping_keys"):
        f.append("grouping keys unstated — cannot assess bucket semantics")

    # 2. Contribution bounds must be finite and stated.
    max_per_unit = analysis.contribution_bounds.get("max_per_unit")
    if max_per_unit is None:
        f.append("max per-unit contribution is UNBOUNDED/unstated — one unit can "
                 "dominate any bucket")
        q.append("What bounds the contribution of a single unit (lead, user, event)?")
    elif not isinstance(max_per_unit, (int, float)) or max_per_unit <= 0:
        f.append(f"max_per_unit={max_per_unit!r} is not a positive number")
    if not analysis.contribution_bounds.get("bound_method"):
        f.append("bound_method unstated — no verifiable enforcement of the bound")

    # 3. Small-cell suppression.
    small = [n for n in analysis.bucket_sizes if n < suppression_threshold]
    if analysis.bucket_sizes and small:
        f.append(f"{len(small)}/{len(analysis.bucket_sizes)} published buckets have "
                 f"n < suppression threshold ({suppression_threshold}) — "
                 f"small-cell disclosure risk")
        q.append("Should sub-threshold buckets be suppressed or merged before release?")
    elif not analysis.bucket_sizes:
        f.append("no bucket sizes provided — small-cell risk cannot be assessed")
        q.append("Provide per-bucket unit counts so small cells can be checked.")

    # 4. Differencing across releases.
    if not release_series_context:
        f.append("no release-series context — differencing risk against prior/future "
                 "releases cannot be assessed")
        q.append("Will this series be published repeatedly? If so, differencing "
                 "across releases must be analyzed before any release ships.")
    else:
        # Informational, not a finding: counsel's judgment call either way.
        q.append("Release-series context was provided; counsel should still "
                 "assess differencing risk across the series before release.")

    # Verdict is descriptive, never a clearance.
    blocking = [x for x in f if "UNBOUNDED" in x or "unstated (methodology" in x
                or "sub-threshold" in x or "n < suppression" in x]
    analysis.verdict = ("NEEDS_MITIGATION" if blocking else
                        "REVIEW_REQUIRED" if f else "NO_AUTOMATED_FINDINGS")
    # Every derivative analysis ends with counsel review required — the verdict
    # names automated findings only; it never approves release.
    if analysis.verdict == "NO_AUTOMATED_FINDINGS":
        q.append("Automated checks found nothing; counsel review of re-identification "
                 "risk is still required before release.")
    return analysis
