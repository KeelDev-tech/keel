# Implemented versus deferred

| Proposed layer | This delivery | Explicit boundary |
|---|---|---|
| Scoped retrieval | Working FTS5/BM25 + token fallback over one filtered snapshot | Local caller authorization belongs to host; no embeddings/vector service |
| Exact cache | Working bounded process-local informational cache | No durable database; no authority/runtime checks cached |
| Workflow recipes | Seven finite built-in operations with prerequisites and mandatory checks | No general agent executor or arbitrary shell |
| Typed contracts | Strict envelopes + documented KeelContract/1 validator and diff | Not a complete JSON Schema or OpenAPI engine |
| Dependency analysis | Python static imports + explicit edges, impact witnesses and cycle remainder | Dynamic imports/runtime edges incompletely observed |
| Candidate staging | Existing-path, base-hash-bound replacements; protected paths enforced | No automatic patch generation, original-source writes, or deployment |
| Safer code inspection | Resource-bounded static parser child with stripped environment | Not a hostile-code sandbox and no target runtime tests |
| Supply diagnostics | Full-buffer/zero-refill distinction, null/stale/future handling | Not patched into live pool guardian |
| Process conformance | Normalized export duplicate/hold/retry diagnostics | No native-schema guessing, canonical reconciliation, or telemetry writes |
| Reproducible handoff | Exact byte-count/hash framing, verify-tree, no-overwrite restore | Hashes are integrity checks, not publisher signatures |
| Mutation evaluation | Six actual selected mutation probes detected by tests | Not an exhaustive mutation score or formal proof |
| Formal specifications | Deferred | No TLA+ verification claim |
| Runtime isolation | Deferred | No Podman/Bubblewrap implementation claimed |
| Service credentials | Deferred | No secret manager or OS identity provisioning |
| Update radar | Deferred | No automatic internet polling or release installation |
| Protocol adapters | Deferred until a specific boundary needs them | No MCP/A2A server exposure |
| Build provenance/SBOM standards | Simple source manifest only | No claim of CycloneDX/SLSA compliance |
| Live approvals/provider receipts | Existing system remains canonical | No alternate ledger, approval issuer, or gateway |

This is the first working maintenance extension, not implementation of every
technology mentioned in the prior design discussion. Core operation and its test
suite need no third-party Python package. Storage, compute and operator capacity
are still real resources even without a new subscription.
