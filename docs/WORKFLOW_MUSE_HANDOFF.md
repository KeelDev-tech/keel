# Keel 0.6 receiving-agent handoff

Treat this as source to review and integrate. The included demo and approvals
are synthetic. Receipt of a package is not evidence it was installed or deployed.

1. Inspect the extractor at the beginning of `Keel_0.6.0_Transfer.txt`. Run its
   `--verify-only` command, then extract to a NEW isolated directory using
   `--out`. Do not extract over the live repository.
2. Read `docs/MERGE_NOTES.md`, `docs/WORKFLOW.md`, and current
   `docs/WORKFLOW_VALIDATION.md`. The two old 0.5 packages were different sources;
   both original file inventories are preserved in `audit/merge-provenance.json`.
3. Run `python3 -B -m keel_workflow demo --out /new/demo-directory` and
   `python3 -B tools/run_release_checks.py --out /new/checks-directory`.
   Use a local environment with the free pinned test dependencies. Inspect
   actual results, including blocked negative cases and recovery evidence.
4. Compare the receiving source, canonical queue/export schemas and live
   gate interfaces. Preserve all existing holds and consent rules. Adopt both
   `keel_assurance` and `keel_trust`, then use the conjunctive workflow adapter;
   do not replace one branch's checks with another branch's result.
5. Supply trusted current state and actual attachment bytes. Authenticate
   reviewer/approver identities outside the model. Route phase-A tasks through
   isolated reviewer contexts without other opinions. Persist private review
   and delivery metadata locally; protect and back it up. Review the 90-second
   export freshness limit before designing live review scheduling.
6. Keep delivery in the built-in simulator until actual executor integration
   is separately implemented and validated. The package contains no live
   submission adapter. Never interpret `SIMULATED_CONFIRMED` as submitted.
7. To integrate with real execution later, bind destination/account/approval
   to the actual form, consume events through the canonical logger, verify real
   provider outcomes, preserve uncertain-attempt holds, and validate an isolated
   restore. Do not invent authentication, reviews, qualification facts, consent
   or provider receipts to fill missing adapters.
8. Report package digest, receiving revision, files integrated, exact commands
   and outcomes, unresolved adapters, and status for RECEIVED, VERIFIED,
   INTEGRATED, SHADOW_VALIDATED, DEPLOYED. Set only states supported by evidence.

No merge, deployment, application submission, outreach, paid operation or new
account creation is authorized by this file. Existing explicit user authority
still governs the receiving system; these instructions do not broaden it.
