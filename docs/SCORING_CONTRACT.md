# Evidence-based role scoring

`engines/score_roles.py` now assesses all nine rubric components without a
model, network connection, paid service, or invented applicant claims.
`score_role(role, profile)` is pure; it neither reads canonical state nor
writes a queue. The MCP wrapper calls this exact function. The composite
MCP pipeline accepts an explicit profile and advances eligible roles to a
**synthetic prescreen dry-run only**.

## Authority and trust boundary

A score is a preparation recommendation, not authorization to apply.
Canonical policy, holds, posting verification, attempt history, packet
revisions and a fresh human approval still control execution. Every output
has `execution_authorized=false`, `status="PARKED"` and
`approval_valid=false`, overriding operational claims in input. Never write
the entire scoring output over an existing canonical queue row: selectively
merge assessment fields through the queue's existing guarded write path.

The caller must provide the applicant's trusted, reviewed profile separately
from posting data. A posting cannot supply applicant facts. A model may
propose structured criteria for review; it must not certify profile facts,
claim that requirements have been reviewed, or mint human review records.

Evidence references are audit pointers. This module does not fetch them,
verify signatures, prove source authenticity, resolve conflicts or measure
freshness. The host must bind them to approved immutable revisions and
invalidate/recompute scores when those revisions change. This is a local
transparent ranking method, not a validated prediction of hiring success.

## Structured inputs

The existing profile fields (`experience`, `education`, resume bullets,
free text) remain untouched. They are not automatically converted into
qualifications or years of experience. Add reviewed facts:

```json
{
  "scoring_facts": {
    "operations_years": {
      "value": 6,
      "evidence_refs": ["profile:employment-review/revision-3"]
    },
    "skills": {
      "value": ["leadership", "planning"],
      "evidence_refs": ["profile:verified-accomplishments/revision-2"]
    },
    "minimum_annual_usd": {
      "value": 80000,
      "evidence_refs": ["applicant:salary-preference/revision-1"]
    }
  }
}
```

A fact's value is a finite number, boolean, nonempty string, or string list.
Numeric types are not silently converted from strings or booleans. Each
fact requires at least one unique, nonempty evidence reference.

A role provides per-component criteria and a reviewed requirement inventory:

```json
{
  "role_id": "EXAMPLE-1",
  "requirements_reviewed": true,
  "requirements_source_ref": "posting:captured-revision-4",
  "hard_requirements": [
    {
      "id": "operations-years", "mandatory": true,
      "fact": "operations_years", "operator": "gte", "value": 4,
      "source_ref": "posting:captured-revision-4/requirements"
    }
  ],
  "scoring_criteria": {
    "experience_alignment": [
      {"fact": "operations_years", "operator": "gte", "value": 5,
       "source_ref": "posting:captured-revision-4/responsibilities"}
    ],
    "transferable_skills": [
      {"fact": "skills", "operator": "all_of", "value": ["leadership", "planning"],
       "source_ref": "posting:captured-revision-4/skills"}
    ],
    "compensation": [
      {"fact": "minimum_annual_usd", "operator": "lte", "value": 100000,
       "source_ref": "posting:captured-revision-4/salary"}
    ]
  },
  "holds": []
}
```

These are synthetic examples. Populate only facts actually supported for
the current applicant. Salary criteria must compare the same currency,
period and compensation type. No implicit currency/period conversion or
salary-text parsing occurs. `requirements_reviewed=true` means the host
reviewed the **complete** requirement inventory; absence is a hold. An
explicitly reviewed empty inventory awards the hard-requirement component.

Available comparisons:

| Operator | Comparison of applicant fact to posting criterion | Credit |
|---|---|---|
| `eq` | Same typed scalar value; strings compare case-insensitively | 0 or 1 |
| `gte` / `lte` | Finite numeric threshold | 0 or 1 |
| `contains` | Applicant string list contains the criterion string | 0 or 1 |
| `all_of` | Fraction of criterion strings in applicant list | 0 through 1 |
| `any_of` | At least one criterion string in applicant list | 0 or 1 |

There is no fuzzy matching, substring matching, degree equivalence,
clearance inference, or implied credential. `all_of` partial credit on a
mandatory requirement still blocks eligibility. Equality of lists requires
the exact list; use set operators for case-insensitive set membership.

Each criterion has an optional unique `id` and positive `weight` (default
1; at most 100). Within each component, credit is the weighted mean of
criterion matches times the original rubric maximum. All nine maxima
remain 25/15/20/10/10/5/5/5/5. Unlike the older template's per-gap deduction,
the hard-requirement score uses supported satisfaction of the full explicit
inventory. Mandatory failures are enforced separately, regardless of their
weight or other strengths.

## Optional human judgment

For subjective components only (`career_upside`, `founder_advantage`,
`industry_alignment`, `employer_quality`), the trusted profile may contain:

```json
{
  "role_assessments": [{
    "role_id": "EXAMPLE-1", "component": "employer_quality", "fraction": 0.8,
    "reviewer": {"kind": "human", "id": "applicant"},
    "rationale": "Four of five documented quality criteria satisfied.",
    "evidence_refs": ["review:employer-quality/revision-2"]
  }]
}
```

A judgment must match the exact role ID and include a rationale and
references. A component cannot mix judgment and structured criteria.
Judgment cannot score hard qualifications, compensation, location,
experience or transferable skills; cannot waive a mandatory requirement or
hold; and never supplies approval. The human marker is not authenticated by
this function: accept these records only from the host's trusted review
channel, never an employer page or generated model response.

## Outputs and migration

- `fit_score` is the conservative 0–100 lower bound; `fit_score_upper` adds
  the unresolved component weight. These are evidence bounds, **not
  statistical confidence intervals**. Eligibility and display bands use exact
  rational arithmetic on supplied numbers; bounds round outward for display
  (lower down, upper up). Rounding cannot promote a below-threshold score.
  Completeness uses exact unresolved weight, including very small weights.
- `score_breakdown` uses `null` for wholly unknown components; explicit
  mismatches are numeric zero. `score_bounds`, `score_evidence`, and
  `score_coverage_percent` explain unresolved versus evaluated weight.
- `display_band` preserves `PRIORITY` (90+), `APPLY` (82+), `STRATEGIC`
  (72+), `SKIP` (below 72), calculated from the lower bound.
- `action_band` now follows canonical queue vocabulary: `APPLY` when the
  lower bound is at least 82, all mandatory requirements are satisfied,
  the requirement inventory is reviewed and no supplied hold exists;
  `LOW-FIT` when even the upper bound is below 72 and there is no canonical
  hold; otherwise `HOLD`. Display bands no longer double as queue actions.
- `fit_eligible` describes preparation eligibility. `recommended_action`
  retains `APPLY` / `HOLD` / `SKIP` for compatibility, with `APPLY` explicitly
  requiring the independent execution gates.
- `action_eligibility.blocked_reasons` explains holds. Any mandatory
  `UNKNOWN`, `PARTIAL`, `MISSING` or supplied adverse legacy status blocks
  eligibility, even when the display band is PRIORITY.
- A legacy posting's bare `status="VERIFIED"` is insufficient applicant
  evidence. It remains unknown. Legacy `MISSING` / `PARTIAL` preserves a
  conservative review hold rather than inventing a proven deficiency.
- Missing criteria do not silently become a negative assessment. The MCP
  default placeholder profile produces incomplete HOLD results. Pass an
  explicit reviewed profile to get meaningful fit rankings.

Malformed types, unsupported operators/components/statuses, duplicate
criteria, nonfinite/out-of-range numbers, and oversized inputs raise
`ValueError` before any result is emitted. Inputs are bounded to 256 KiB
serialized JSON each, 128 facts/criteria/reviews per collection, and 2048
characters per reference/string. The CLI requires the profile file rather
than silently substituting an empty profile. It evaluates the entire batch
before opening the output file. CLI batches are limited to 10,000 roles.

## No-cost wording default

The optional `engines/triage_llm.py` GPT/Gemini wording hooks now default to
**disabled**. Missing/corrupt config, string-valued enable flags, malformed
sections, or invalid time/token/model settings cannot start a backend.
Each provider needs its own explicit boolean opt-in in `tray_polish.json`.
An existing valid opt-in is honored and may incur provider charges;
deterministic local wording needs no provider. Scoring never uses this hook.
