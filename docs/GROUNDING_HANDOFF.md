# Keel 0.10.0 — receiving-agent handoff

Status: integration candidate, not deployed. This release adds actual-file
evidence resolution, grounded scoped answers, complete declared packet-content
verification and a labelled local-model evaluation harness. Existing 0.9
reference files remain byte-identical. Read the delivered Release Evidence JSON
for measured regression counts, synthetic rehearsal results and source hashes.
Historical audit reports describe their original releases, not new runs.

## Receive and inspect

The complete transfer is one executable Python TXT attachment; no ZIP is needed.
Save the whole file without truncation or editing its encoded payload.

```sh
python3 -B Keel_0.10.0_Transfer.txt --verify-only
python3 -B Keel_0.10.0_Transfer.txt --out /existing-parent/new-keel-0.10
```

Extract into a new directory, never over the live Keel tree. Read
`docs/GROUNDING.md` and `docs/GROUNDING_EVALUATION.md`. The synthetic demo is:

```sh
cd /existing-parent/new-keel-0.10
python3 -B -m keel_grounding demo --home /existing-parent/new-synthetic-fixture
python3 -B -m keel_eval replay fixtures/grounding_eval/dataset.json fixtures/grounding_eval/replay.json --out /existing-parent/new-replay-report.json
```

Replay measures the scoring code and supplied synthetic answers. It does not
measure a real model. The fixture includes labelled abstentions; regression
checks also inject wrong and malformed responses to verify error accounting.
A scoring exit status is never model acceptance.

## Additive integration

The installer targets the unchanged 0.9 reference baseline. It cannot certify a
different private host checkout. Inspect with `python3 -B tools/install_grounding.py
--help` and use its read-only mode before applying. It refuses mismatched
prerequisites, overwriting or conflicting additions, and supports idempotent
installation. Original historical `MANIFEST.json` is reference packaging data,
not a required or replaced host file.

If the live host is modified or still on 0.7/0.8, do not reset host configuration
or remove controls to satisfy the reference hashes. Review the additive modules
against the real tree, complete earlier ports as appropriate, then run the live
guarded suite. Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED
separately. Our supplied-tree verification does not establish live integration.

For a clean reference, free development test dependencies are in
`requirements-dev.txt`. Bind a fresh run to the release source:

```sh
python3 -B tools/run_grounding_checks.py --out /existing-parent/new-checks
python3 -B tools/run_grounding_acceptance.py --out /existing-parent/new-acceptance
```

## Host wiring required

1. Emit actual source files and exact byte hashes, plus reviewed selectors and
   value bindings for existing trust claims. Missing real records remain blockers.
2. Bind each answer to the entire recorded bank entry. The trusted question
   adapter supplies the claim predicate and exact applicant/employer/posting
   context. Keep no-AI, consent and attestation rules intact.
3. Obtain the packet manifest digest from the existing authenticated review
   context. Include every intended artifact, field mapping and attachment.
   Wire `keel_grounding.integration.build_grounded_proof` into the existing
   read-only Workbench proof path; it derives the canonical trust document and
   intersects fresh packet checks with all existing capture/review gates.
4. Keep content immutable through actual use or recheck bytes and current
   authority immediately before execution. Verification reports are not tokens.
5. Run an independently reviewed held-out dataset against the already installed
   local model using the explicit evaluator command. Inspect false PASS rates,
   missed supported cases, abstentions, response errors and latency. No real
   inference, rendered browser, real data or production run is claimed here.

Authentication, source truth and natural-language entailment remain distinct
from byte equality. Binary attachments have identity checks only. No new code
can manufacture source evidence, approval, consent or execution authority.
All existing holds and protection rules remain required. No paid API is added.
