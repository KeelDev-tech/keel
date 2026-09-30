# Metadata-only supply measurements and synthetic failure replay

`supply-measurements` is a read-only inventory projection. It does not capture prompts, answers, credentials, names, titles, URLs, exact fit scores, browser task IDs or raw evidence. It reuses the coherent supply census and admission checks. Role and target references use domain-separated HMAC-SHA256 under an operator-held 32-byte random key. References correlate snapshots within a workspace; they are pseudonymous, not anonymous, and should have restricted access and retention. Keep the key private and outside version control. Changing the key changes references and prevents cross-key comparisons. References are workspace-scoped even if a key is reused. Snapshot integrity hashes detect changes; they do not authenticate the source or prove reported evidence is true.

Create a key once in the workspace's private directory using `secrets.token_bytes(32)` and an exclusively created file with permissions 0600. The measurement command requires an existing key, reads it without following symlinks, and never generates or prints it:

```sh
python keel.py --home WORKSPACE supply-measurements --key-file hidden_files/measurement.key
```

Save the JSON output to a private file inside that workspace to compare later:

```sh
python keel.py --home WORKSPACE supply-measurements --key-file hidden_files/measurement.key --before hidden_files/previous-measurements.json
```

Snapshots contain per-role stage, coarse fit bucket, allowlisted blocker categories, local ownership-marker presence, prepared-packet validity, and retained verification signal/transport categories. Stage and fit counts partition the inventory; blocker counts can overlap. Unknown reason strings collapse to a fixed category. Comparison requires the same workspace, key, controller code and policy inputs plus an advancing timestamp and conserved inventory. Retargeted roles are counted separately and excluded from preparation gains. Duplicate queue homes prevent comparison.

Reader attempts shared by several roles are counted once using opaque dispatch references. These are observations retained in the latest queue-row evidence, not lifetime request totals or complete interval billing. A row can overwrite or lose evidence between snapshots; comparison reports new retained dispatches and lost references, while complete coverage stays false. Gains per new retained dispatch are observational and are null when the denominator is zero. Historical model tokens, credits and launchable READY remain unknown. This measurement command performs no network calls, model inference, queue writes or approval changes.

Export at most 25 representative recipes with `supply-measurements --key-file hidden_files/measurement.key --failure-cases`. Recipes contain only a hash-derived case identifier and a fixed scenario name for HTTP 429, unanswered questions, ledger history, office holds or an observed local ownership marker. Unsupported failures are not inferred. Save the output privately, then run:

```sh
python -S tools/run_supply_replay.py --cases PRIVATE_CASES.json --out /tmp/keel-replay-UNIQUE
```

Replay accepts only these bounded fixed recipes and executes the production supply acceptance path with fresh synthetic fixtures. It never reads applicant payloads, reconstructs an incident, authenticates imported claims, targets a real role, or authorizes an action. A terminal row without a local ownership marker is not relabeled as an ownership incident. Empty recipes fail explicitly. The CLI audit guard prohibits network and child-process attempts; it is not an OS sandbox. Source and postcondition results are recorded in `replay.json`, with synthetic fixture evidence in the child directory.

The clean recovery workflow includes the metadata/privacy/comparison/replay regressions. These exports provide a local way to see supply losses and turn supported failure categories into regression seeds without adding an observability service or paid model calls. Live Muse connectivity and authenticated host measurements still require the runtime integration. Full lifecycle traces and authoritative provider token/billing measurements are future integration work.
