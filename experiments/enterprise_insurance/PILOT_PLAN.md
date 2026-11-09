# Enterprise lane: buyer-discovery and pilot qualification plan

## The decision being tested

Can a buyer use an evidence-linked account review queue to reduce manual
qualification work and improve the quality of accepted business accounts?

There are two distinct propositions. Do not mix their datasets or results:

1. **Enterprise acquisition:** qualify agencies as potential clients for Keel.
   This is the scope modeled by the current software.
2. **An agency's prospect pipeline:** qualify the agency's approved target
   organizations on its behalf. This is a possible future use case, not an
   established need. Consumer life-insurance leads, agent recruiting and
   insurance sales require different schemas, authorities and operating controls.

An agency may only want consumer leads or recruits. That is a scope mismatch,
not a reason to relabel consumer records as business-account evidence. Confirm
this before investing in collectors, enrichment or CRM integrations.

## Discovery account brief: private, manual, initially unverified

Keep the real target mapping out of this public repository. The initial brief
must distinguish facts, hypotheses and unknowns. Do not infer buying authority
from a recruiter or an interview invitation.

| Question | Evidence needed | Current disposition |
| --- | --- | --- |
| Which exact business is the counterparty? | Legal name, official domain, business identity | Unverified |
| Who owns the problem and can sponsor procurement? | Identified role and explicit internal introduction | Unknown |
| What leads are actually needed? | B2B client accounts, partner accounts, consumers or recruits | Unknown |
| What is failing today? | An agreed baseline for review time, duplicates, rejection reasons | Unknown |
| How much eligible volume exists? | Count and period for authorized business-account records | Unknown |
| Which system would receive output? | CRM, required fields, permissions, integration owner | Unknown |
| What data may be processed? | Written field-level scope, source rights, retention, security terms | Not authorized |
| What would justify purchase? | Buyer-defined success criteria, budget owner, commercial next step | Unknown |

First useful deliverable: a one-page findings note and anonymized workflow map,
not a promise of leads, revenue, fraud detection or licensing compliance.

## Prototype qualification rubric

Identity 10; sector fit 15; demonstrated need 25; buyer path 15; meaningful volume
15; workflow compatibility 20. The weights and 75-point floor are experimental
heuristics, not calibrated probabilities. Every criterion needs current,
unambiguous evidence (including explicit negatives); unknowns stay parked.
Identity, sector and need must be positive regardless of total score.

Evidence fields carry source reference, asserted value, observation time,
expiration and a source-permission marker. The current system accepts only
synthetic assertions. It does not retrieve a page, interpret an interview,
verify a legal entity or authenticate a source. Those are future adapter and
human-review responsibilities; passing fixtures does not satisfy them.

## Proposed real-pilot entry gates: all remain open

- A named sponsor confirms a specific **business-account** use case and agrees
  that qualified-for-review does not mean permission to contact or sell.
- Written authorization identifies the legal counterparty, allowed dataset,
  field inventory, intended purpose, retention/deletion and output recipient.
- Source-by-source processing rights and a suitable private storage environment
  are reviewed; no applicant/recruiter or consumer/policyholder data is included.
- Security review qualifies authentication, role boundaries, encryption,
  audit anchoring, backup/deletion, network controls and minimal integration.
- Qualified legal review scopes privacy, outreach, licensing and compensation
  requirements for the actual jurisdiction, channel, product and activity.
- Host interfaces and Python/CI targets are tested against the actual host tree.
  No changes to a private Keel runtime may be inferred from this public branch.

Commercial email is not exempt from CAN-SPAM merely because it is B2B. The FTC
explains accurate sender information, opt-out and other requirements [1]. The
California Department of Insurance describes life-agent licensing [2]. These
sources identify review areas; they do not constitute a legal opinion about a
specific service, data source or commission/referral arrangement. No outbound
or insurance-transaction implementation is included in the prototype.

## Suggested bounded pilot, after authorization and a separate implementation

Start with **25 authorized business-account records**, selected before tuning.
This is a proposed learning cohort, not a statistical accuracy guarantee. Have
a buyer-side reviewer independently label acceptance and rejection reasons;
withhold those labels from rule tuning. Freeze the criterion definitions and
version before evaluation. Include rejected, incomplete, duplicate and stale
records rather than measuring only apparent successes.

Proposed acceptance criteria, to be agreed with the buyer rather than advertised:
100% permitted-field/source traceability, zero unauthorized transfers, zero
cross-lane imports, all duplicates explained, all unknowns visible, and a
meaningful reduction in median human review time against the same-work baseline.
A 30% time reduction and 90% reviewer acceptance may be negotiation starting
points, **not measured results, promises or universal standards**. Report exact
numerators/denominators and keep inconclusive outcomes inconclusive.

Measure: unique admissible accounts; complete evidence / admissible accounts;
reviewer-accepted / all submitted review packets; duplicate and correction
counts; median reviewer minutes; stage age; number and cause of holds. A pilot
acceptance rate is not sales conversion. Revenue requires actual contracted or
collected amounts and independently linked outcomes, not score-derived estimates.

Economic test: measured review hours avoided multiplied by the buyer's agreed
hourly cost, less actual incremental operating and support costs. Keep this
separate from hypothetical sales contribution. The current standard-library
prototype requires no new paid service; labor, real hosting and future approved
integrations are not assumed free.

## Go / revise / stop

**Go:** a sponsor, permitted B2B data and an actual use case exist, the independent
pilot passes agreed controls, and the buyer confirms a commercially meaningful
next step. Build only the adapter needed for that approved scope.

**Revise:** workflow savings are plausible but evidence, field mappings or review
criteria need a bounded correction. Re-test on a new held-out cohort.

**Stop or retarget:** the agency wants a different lead type, will not authorize
data, has no internal buyer, requires unapproved spending, or the measured
benefit does not justify effort. Technical correctness alone is not demand.

## References reviewed October 9, 2026

[1] Federal Trade Commission, CAN-SPAM Act: A Compliance Guide for Business.
https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business

[2] California Department of Insurance, Life Agent.
https://www.insurance.ca.gov/0200-industry/0050-renew-license/0200-requirements/life-agent/
