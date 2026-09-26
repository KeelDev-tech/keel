# Migration from the supplied 0.2.0 package

Work on an empty workspace or a copy. The original upload was not edited. No historical production state or authorization was imported automatically.

| Change | Migration action |
|---|---|
| Code and data are separate | Keep the release directory intact. Set `KEEL_HOME` or `--home` to your data workspace. |
| Queue/ledger paths standardized | Canonical paths are `data/queues/*-queue.json` and `data/application-ledger.json`. Compare any old `queue/` or `ledger/` copies before moving them; conflicting copies require reconciliation. |
| Assertions require receipts | Run `confirm-answer` for true, applicable values. Do not bulk fabricate provenance for old defaults. |
| New packet contract | Rebuild old packets; they lack input hashes and preparation-only authority fields. |
| No IN-FLIGHT after packet creation | A prepared file is not an executing task. Integrations consuming the old status transition must adapt. |
| Claim event vocabulary | New public writes use `submission_claimed`. Historical `submitted` rows remain unverified observations. Migrate downstream reports before relying on their totals. |
| Corruption is surfaced | Restore or reconcile malformed state; do not replace it with an empty success-looking file. |
| New preparation leases | Lock file identities are hashed and scope is preparation only. These leases must never replace private submission holds. |
| Private scheduler dependencies removed | Monitor defaults express observation thresholds; no cron or task registration is installed. Configure thresholds for your real scheduler and compare actual scheduler exports. |
| Complete test command | Install free dev dependencies and use `tools/run_tests.py`. `unittest` is a subset. |
| Public review boundary | Do not apply this candidate over an unseen private executor. Preserve standing directives and adapt the source-specific integration with evidence. |

All original limitations retain their original IDs in `ORIGINAL_100_MAPPING.md`. “Locally addressed” does not mean deployed or verified in production.

## 0.3.1 HTTP state

Readers create `data/http-cooldowns/` under `KEEL_HOME`. Keep it available to cooperating workers and never unlink a live lock file. Cooldowns survive restarts. Network reads require writable local storage; separate workspaces do not coordinate. See `docs/HTTP_ADMISSION.md`.
