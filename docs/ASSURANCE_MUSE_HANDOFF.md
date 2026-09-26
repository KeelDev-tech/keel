# Keel 0.5.0-review.1 — Cognitive Assurance integration handoff

Trent requested implementation of his ECV and cognitive-risk research in Keel.
This release is a locally integrated and tested reference candidate based on
`Keel_0.4.0_Flow_Source.txt`, not a replacement for the private live repository.
Treat the earlier 0.4.0 handoff as historical baseline context. This instruction
does not grant any new submission, spending, browser, consent or outreach power.

## Read and reproduce

Read README.md, docs/ASSURANCE.md and this handoff. Then use a new isolated
POSIX Python environment and the pinned requirements-dev.txt if pytest is absent:

```bash
python3 -B tools/run_release_checks.py --out /new/path/keel-assurance-checks
python3 -B -m keel_flow board sample_data/flow.example.json --assurance sample_data/assurance.example.json --now 2026-09-18T12:00:00+00:00 --format markdown
python3 -B -m keel_assurance audit sample_data/assurance.example.json --now 2026-09-18T12:00:00+00:00
python3 -B -m keel_assurance metrics sample_data/assurance-metrics.example.json
python3 -B -m keel_assurance plan sample_data/assurance-plan.example.json
python3 -B -m keel_assurance conformal sample_data/assurance-conformal.example.json
```

Every example is SYNTHETIC. `make_assurance_demo.refresh_reviews` creates test
fixtures; it is NEVER a production validator, reviewer, receipt issuer or
approval generator. Do not copy its facts, GRANTED/APPROVE observations, model
metadata, scores, labels or timestamps into real state.

The audit command exits 0 only when supplied assurance checks pass, 3 when
well-formed observations require review, and 2 on malformed inputs/I/O errors.
Even exit 0 has execution_authorized=false. The combined board is a report and
retains its established exit convention; inspect the structured results.

## Integrate in this order

1. Locate the actual current repository and read its applicable AGENTS.md,
   current branch/revision, guardian, canonical queue, answer bank, issuer and
   attempt/receipt adapters. Preserve local work. Use a candidate branch/copy;
   do NOT overwrite production with the full reference tree.
2. Compare the supplied `audit/assurance-changes.json` and patch against the live
   baseline. Port the new `keel_assurance` package and additive `keel_flow`
   board/CLI changes deliberately. Preserve existing hold/attempt/rate-limit
   behavior. Do not update unrelated historical source or maintenance files.
3. Implement a read-only export adapter over canonical stores. Use one
   consistent revision, exact source timestamps and live current time. Export
   minimally necessary scoped facts, current necessary dependencies, and all
   relevant actions. Do not mark an incomplete export complete.
4. Bind action identity, exact role target, full lead digest, packet dependency
   hash and actual payload bytes. Independently check claim extraction covers
   all material claims, form values, documents and attachments. Hash matching
   does not establish that an invented qualification is true.
5. Resolve existing authenticated authority and human-review records. Scope
   them exactly using proposal_scope(action). This helper is NOT an issuer.
   Missing/unknown/expired authority remains blocked; no historical YES or
   generic preference becomes a new attestation.
6. Connect real permitted reviewer adapters. They must inspect the full bound
   material and return actual PASS/FAIL/ABSTAIN evidence, not fixture-shaped
   guesses. Record current family/method/group and independently assess their
   failure correlation. Do not relabel two prompts on one model as independence.
   If two suitable reviews are unavailable, remain REVIEW_REQUIRED.
7. Run the combined board on the actual export and actual evaluation time.
   Route missing claims, disagreement, stale evidence and scope mismatches to
   existing review ownership surfaces. Do not clear queue holds from a report.
8. Deploy only shadow observation first. Preserve canonical stores as authority.
   Before any later enforcement, require the envelope at the adapter boundary,
   recheck all bound revisions under the existing dispatch lock, preserve the
   original operation identity, and reconcile ambiguous outcomes before retry.
   This candidate has no live executor and does not enable a transport.
9. Collect mature independent outcome labels, audit references, abstention,
   operator effort, latency and resource usage. Report unknown denominators.
   Use chronological holdouts and semantic deduplication. Never tune on the
   evaluation set or call synthetic ECS/conformal outputs calibrated production
   performance. Compare extra reviewers against a strong single-reviewer baseline.
10. Run all current live-baseline tests plus these regressions with isolated
    fixtures. Verify unchanged canonical state, original holds, unknown attempts,
    429 stops, missing assurance, dependency revocation, cross-role binding,
    human rejection, review disagreements and stale approvals. Do not drain a
    real queue or submit an application to create a test result.

## Preserve existing restrictions

- No invented applicant facts, qualifications, scores, job liveness or receipts.
- No consent inference, attestation drafting where assistance is prohibited,
  bulk hold release, or change to Stripe/WhatsApp quarantine without Trent's
  explicit current scoped answer.
- No append-only ledger rewrite, competing identity store, unsanctioned queue
  mutation, model installation, paid fallback or credential extraction.
- The existing browser lane and ownership restrictions remain unchanged.
  No browser spawning, steering, takeover or closing is authorized here.
- HTTP 429 and existing provider limits remain hard stops. No retry via another
  transport after an ambiguous result. Reuse canonical attempt identity.
- No outreach, reference contact, spending, new recurring scheduler, or
  production rollout is performed by this source handoff.

## Return evidence

Return actual received-source digest, applied diff, base/current revisions,
real test logs and counts, current export timestamp/hash, named adapter gaps,
canonical event IDs where already authorized, and measured shadow behavior.
Separate RECEIVED, BUILT LOCALLY, INTEGRATED IN REFERENCE, INTEGRATED LIVE and
OBSERVED LIVE. Do not report live integration or state-of-the-art performance
from a successful text extraction or a test fixture.
