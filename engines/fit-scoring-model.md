# Evidence-based fit scoring (0–100)

The implemented contract is [Evidence-based role scoring](../docs/SCORING_CONTRACT.md)
for `engines/score_roles.py`. This rubric summarizes that contract; it does not
supply applicant facts, waive requirements, or authorize an application.

| Component | Maximum |
|---|---:|
| Experience alignment | 25 |
| Transferable skills | 15 |
| Hard requirements | 20 |
| Career upside | 10 |
| Compensation | 10 |
| Founder advantage | 5 |
| Industry alignment | 5 |
| Location / work model | 5 |
| Employer quality | 5 |

Each component uses the weighted mean of explicit, evidence-backed criterion
matches times its maximum. Supported human assessments are available only for
the subjective components described in the contract. The scorer sums component
bounds; it does not multiply compensation, freshness or other ranking factors,
and it does not apply fixed per-gap deductions. Compensation and work-model
preferences come from the reviewed applicant profile, not universal defaults.

`fit_score` is the supported lower bound; `fit_score_upper` includes unresolved
weight. Unknown evidence is not a proven mismatch. `score_breakdown`,
`score_bounds`, `score_evidence` and `score_coverage_percent` explain the result;
the MCP composite pipeline preserves these fields too. References are audit
pointers, not authenticated proof or statistical confidence intervals.

Display bands use the lower bound: PRIORITY at 90+, APPLY at 82+, STRATEGIC at 72+,
and SKIP below 72. These labels are not execution decisions. The scorer recommends
APPLY only at 82+ with reviewed requirements, all mandatory requirements satisfied,
and no supplied hold. Mandatory UNKNOWN, PARTIAL, MISSING or adverse legacy status
blocks eligibility regardless of other strengths; a subjective assessment cannot
waive it. Other outcomes follow the contract's HOLD/LOW-FIT rules.

The operational intake/READY fit floor remains 75. It is a separate gate from the
scorer's 82-point recommendation threshold; neither replaces canonical readiness,
policy, posting verification, attempt history or fresh human approval. Scoring
always returns `execution_authorized=false`, `approval_valid=false` and PARKED
status. Do not overwrite an operational queue row with the scoring result.
