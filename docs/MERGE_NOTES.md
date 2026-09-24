# Reconciling the two Keel 0.5.0 candidates

The assurance and trust/twin inputs independently used `0.5.0-review.1`.
They are different source trees. The combined candidate is
`0.6.0-review.1`; neither prior release's test evidence proves this merge.
`audit/merge-provenance.json` records both original file inventories and
SHA-256 values, trust-only copied files, and every common-path conflict.
Those hashes check content consistency, not the identity of the source author.

## Preserved behavior

- The full `keel_assurance` package and its evidence, review, lineage,
  calibration and bounded planning contracts remain available.
- `keel_flow` retains optional `--assurance` and the conjunctive
  `base_executable AND assurance_checks_passed` calculation. Missing
  assurance is visibly `NOT_CONFIGURED`, never a qualified readiness count.
- All trust/twin modules, examples, tests and historical evidence are added.
  Scoped local authority admission adds `authority_use` to known telemetry
  events. It does not become evidence of a provider submission.
- Source inventory includes assurance, trust and the new workflow package.
  Both existing guarded release suites remain required.
- The research assurance document remains at `docs/ASSURANCE.md`.
  The trust branch's different boundary document is preserved as
  `docs/TRUST_ASSURANCE_ORIGINAL.md`.

## Combining evaluations

`keel_trust.report.build` creates a derived in-memory view that removes invalid
material bindings from supply. It is read-only and does not itself accept an
assurance envelope. `keel_flow.board.build(..., assurance=...)` validates its
envelope against the exact original snapshot and action bindings.

An integration must evaluate assurance against that original canonical
snapshot and conjunct it with the trust result. Never rewrite the envelope's
snapshot digest to make it fit a derived trust view; that would falsely reuse
reviews over changed inputs. The combined workflow documents its own gate
intersection and keeps these component boundaries explicit.

The flow, assurance and trust runtime reports use the merged candidate version.
Original `audit/assurance-*` and `audit/trust-*` records retain their original
bytes and versions. The old `tools/build_assurance_release.py` is a historical
assurance release helper with fixed 0.5.0 filenames; use the current handoff's
release commands for this combined candidate.

## Authority boundary

This merge does not add an external dispatcher, migrate the canonical queue,
infer consent, or turn observations into authenticated authority. Runtime
reports retain `execution_authorized: false`. The host must authenticate
issuers, check actual payload bytes, reconcile ambiguous outcomes and enforce
current canonical gates at any separately authorized execution boundary.
