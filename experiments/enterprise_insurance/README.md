# Enterprise insurance lane — isolated feasibility experiment

Status: DESIGN ONLY / DISABLED. Candidate account: The Fine Group. No company authorization or partnership is implied.

## Goal
Evaluate whether Keel's evidence-first workflow can qualify **business accounts** for an insurance-distribution enterprise pilot without changing the job application pipeline. This is not a consumer insurance lead scraper or outreach engine.

## Isolation contract
- Separate namespace `enterprise_insurance_v0`, data root, event ledger, schemas, and retention policy; never import applicant profiles, recruiter correspondence, ATS data, answer banks, or job application queues.
- Feature flag `KEEL_ENTERPRISE_INSURANCE_ENABLED=false` by default. No connection to apply-loop, ATS execution, or SRF bridge. No automatic sending, calling, quoting, or insurance transactions.
- Synthetic or explicitly authorized business-account records only. Never collect health, financial, policyholder, or other sensitive consumer information.
- SRF findings remain advisory; no automatic disqualification, accusation, or outbound action.
- Use existing Keel mechanisms as design inspiration only; do not reuse application-specific semantics or data.

## Minimal candidate schema
`account_id`, `organization_name`, `business_domain`, `source_url`, `source_observed_at`, `source_permission`, `industry_fit`, `hypothesized_need`, `evidence_refs`, `review_state`, `outreach_authorized` (default false), `owner`, `retention_until`.

## States
DISCOVERED -> EVIDENCE_REVIEW -> QUALIFIED_FOR_DISCOVERY -> HUMAN_APPROVED -> PILOT_CANDIDATE.
Uncertain provenance, consent, identity, or eligibility -> PARKED. No state grants outreach or system access.

## Pilot qualification questions
1. Does the organization want third-party lead intelligence or CRM assistance?
2. What is its existing CRM and integration policy?
3. Are leads supplied by the organization and contractually usable?
4. What licenses, marketing-consent records, TCPA/DNC and state-law controls apply?
5. What security, privacy, retention, procurement and audit requirements apply?

## Acceptance gates before implementation
- Unit tests demonstrate strict namespace separation, denial of applicant-data import, default-deny outbound actions, evidence provenance, deduplication, and fail-closed states.
- Human review of licensing, TCPA/DNC, CAN-SPAM, privacy, data sourcing, and any state-specific insurance rules.
- Written permission before touching company systems or importing company records.
- Only after these gates: implement a synthetic CLI prototype, then request separate approval for a real pilot.

## Success measures
Authorized-account precision, evidence completeness, reviewer time per qualified account, false-positive rate, duplicate rate, and consent/permission coverage. Do not claim conversion or revenue from synthetic trials.

## Initial seed
The Fine Group — **UNVERIFIED ENTERPRISE PROSPECT**. Discovery only. No contacts or private data seeded. No outreach authorization.
