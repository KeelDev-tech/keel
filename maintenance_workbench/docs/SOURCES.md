# Sources and evidence

## Project inputs actually read

Keel_0.3.1_Review_Source.zip from the user's Library, SHA-256:
`1cbe0db81f839f6d30b9a240b365a8780071fbd60a6d0d632fbfced7e27b112e`.
All 180 manifested entries were verified before extraction. The source was read
for static integration only and was not imported/executed. The original 262-test
claim in its handoff is historical, not a new result from this workbench.

Keel_0.3.1_MUSE_Handoff.txt supplied the current review-boundary constraints.
Blocker breakdown.md dated 18 September 2026 supplied the reported supply-starvation
case and current exclusions. Its counts were not independently verified against a
live queue here. The new supply test uses the reported (5,0) input as a regression
scenario, not as a claim about current production inventory.

## Official technical references checked during implementation

- SQLite FTS5, BM25 and extension availability: https://www.sqlite.org/fts5.html
- Python 3.12 AST parsing and parser resource caveats: https://docs.python.org/3.12/library/ast.html
- Python filesystem flags and directory-descriptor operations: https://docs.python.org/3.12/library/os.html
- unittest results/discovery behavior: https://docs.python.org/3.12/library/unittest.html
- Podman network/read-only/pull/security options: https://docs.podman.io/en/latest/markdown/podman-run.1.html

Podman was evaluated as a future boundary only; it is not installed or integrated
by this package. The delivered implementation is original extension code under
Apache-2.0. KEEL reference source is not bundled into this extension.

## Validation evidence

`evidence/tests.json`, `evidence/tests.log`: actual workbench suite execution.
`evidence/mutation-probes.json`: six selected disposable-copy mutations.
`evidence/reference-analysis.json`: actual static run over the retrieved source.
`evidence/demo-result.json`: synthetic contract-change/mapping-repair sequence.
`VALIDATION.md`: scope, environment, results and unrun gates.
