# Muse observation and bounded supply recovery

The intake `FIT_BAR` is the canonical fit floor (currently 75). Tray visibility follows it by default. Explicit `--min-fit` or `KEEL_TRAY_MIN_FIT` overrides change tray visibility only; READY and recovery eligibility still use the canonical floor.

`keel.py --home WORKSPACE muse-census` reads a coherent inventory, hashes policy inputs and code, and reports duplicate queue homes. Launchable READY remains unknown without host evidence. `--host-snapshot PATH` validates an in-workspace snapshot against exact role, posting, row, workspace and code digests plus freshness, but labels it unauthenticated. It cannot grant approval.

The `engines/muse_bridge.py` HostProvider contract is the integration point for Muse. A production provider must perform bounded exact lookups, authenticate the runtime and validate current approval receipts. This repository does not contain that runtime adapter or credentials. It never executes a browser or submits an application.

Run `supply-plan --limit 25 --max-requests 10` to produce a reproducible plan ID. A canary requires that ID: `supply-canary --plan-id ID --limit 25 --max-requests 10`. This default previews bounded public posting reads without queue writes. At most 25 roles and 10 actual reader dispatches are permitted. Retried requests consume that budget. Holds, ledger history, questions, duplicate identities, cooldowns, owned attempts and low fit remain excluded.

Live posting commits additionally require an injected authoritative HostProvider. The CLI therefore cannot run a nonempty live cohort until the runtime integration exists. Policy hashes and selected rows are rechecked before commits. The controller performs no queue moves, READY promotions, answer inference or submission.

Live canaries journal STARTED before dispatch and COMPLETE after reconciliation. Interrupted runs block further canaries. `supply-reconcile` reports matching committed observations without replaying requests or closing the journal. History capacity is bounded; exhausted history requires explicit operator review.

Scheduling reserves half of a cohort for least recently selected work. Yield affects the remaining capacity only after two completed, conserved windows under the same code and fit policy. Reports count observed preparation gains per reader dispatch, including unattributed requests; they do not claim causality. Retargeted postings are excluded from gains. Unknown credit usage and gains per model token remain null; the controller itself uses zero model tokens. This is evidence for pacing, not evidence of successful submissions or recovered live supply.

## Local validation, 2026-09-30

The final extracted source package passed 346 maintained recovery tests across 13 modules. All 45 new fit/Muse/supply contract cases passed. Cold CLI smoke checks passed with Python `-S`, including the census, empty plan, empty canary and reconciliation commands. Integration with the newly merged fit-default changes passed 121 focused cases before the final four host-evidence cases were added.

The broad core run reported 6,277 passed, 45 failed and 23 skipped. Of the failures, 44 match the recorded pre-existing baseline. The additional failure was an unchanged concurrent-cache test racing a transient SQLite journal file; its complete module passed all 76 cases on recheck. The separate local-profile suite passed all eight cases. These results do not establish live Muse availability, production supply recovery, submission authority or a fully green broad test suite.
