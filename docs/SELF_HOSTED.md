# Keel 0.7 self-hosted bridge

This additive integration candidate connects real-source revision exports, local state, durable review jobs, local model inference, and bounded browser preparation. It requires no subscription or paid API. You supply a machine and appropriately licensed model weights; electricity, hardware, and maintenance still cost money.

The current priority is the seven-source producer in `SEVEN_REVISIONS.md`. The reported live export of 837 blocked leads is user-supplied status, not independently observed here. The included 837-scope reproduction is explicitly synthetic. Missing exports are system diagnostics; absent actual consent remains a human decision. Never fill either gap with generated evidence.

## What runs locally

| Component | Implementation | Limit |
|---|---|---|
| Source revision producer | policy, form, answers, attachments, target, approval, route | normalized real source records required; structural completeness only |
| State | SQLite, private host key, signed sequenced snapshots, lease fencing, audit events | local OS account is the trust boundary; no upstream identity authentication |
| Review | local Ollama or llama.cpp HTTP, phase A commit, seal, phase B audit | actual model quality and installed weights are not attested |
| Fresh preflight | retained original receipt anchor plus current flow/assurance/trust | material changes invalidate the review; fresh facts cannot be invented |
| Browser | Playwright, exact destination and bytes, preparation only for public sites | rendered browser could not be verified in this build environment |
| Local submit fixture | bundled loopback test server and explicit fixture contract | no production application submission adapter |

The CLI worker currently processes REVIEW jobs. READ/PREPARE/SIMULATE/LOCAL_SUBMIT durability APIs are available for host adapters; they are not general autonomous handlers. No model-generated shell commands, uncontrolled tools, or remote fallback are provided.

## Installation

Use Python 3.10 or later on your own Linux host. The core package uses the standard library. Optional browser support requires Node.js and Playwright. Start from the provided unified source ZIP or verified single-file transfer; keep the live 0.6 tree intact while reviewing this addition.

```sh
python3 -m keel_agent --help
python3 -m keel_agent init --home /absolute/private/keel-agent --workspace YOUR_WORKSPACE
```

The new home must be private 0700; database and host key use 0600. Keep them together in backups. Missing or changed keys do not silently recreate access to an existing database. Run as a dedicated OS user. This release does not install a privileged service or alter the existing live scheduler.

For local inference, install either Ollama or llama.cpp using their official instructions, then install models that fit your RAM/VRAM and whose licenses permit your use. No model is downloaded by Keel. For Ollama, disable cloud features with `OLLAMA_NO_CLOUD=1` on the model-server process, restart it, and verify its startup configuration. Strict offline use additionally requires denying the model-server process outbound network access. A localhost URL by itself does not prove the server stays offline.

Create an operator-owned reviewer configuration, substituting the exact locally installed model tags and the reviewer IDs already present in the assurance registry:

```json
{
  "reviewers": [
    {"reviewer_id":"reviewer-a","backend":"ollama","endpoint":"http://127.0.0.1:11434/api/chat","model":"YOUR_INSTALLED_MODEL_A"},
    {"reviewer_id":"reviewer-b","backend":"llama_cpp","endpoint":"http://127.0.0.1:8080/v1/chat/completions","model":"YOUR_SERVER_MODEL_B"}
  ]
}
```

These are placeholders, not claims that two particular models fit your machine. The assurance registry must truthfully describe different model families, methods, and independence groups. Two personas of one model do not establish diversity. Distinct configured model names are not cryptographic proof of independent weights or reasoning. Review expiry is at most 15 minutes; reduce workload or use faster models if the round cannot finish before expiry. Failure produces a hold, never an inferred pass.

Official references: [Ollama chat API](https://docs.ollama.com/api/chat), [Ollama local-only configuration](https://docs.ollama.com/faq), [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [Playwright installation](https://playwright.dev/docs/intro).

## Wire the seven sources first

Follow `SEVEN_REVISIONS.md`. The receiving live host must map its actual fields into `keel.revision_sources.v1`, preserving source IDs, source versions, observation times, validity, and revocation. Attachment hashes are derived from the actual contained files. Approval is supplied from an existing authentic decision and binds the other six revisions; it is never generated as a convenience.

```sh
python3 -m keel_agent revisions normalized-sources.json --attachments /absolute/packet-directory --out revision-report.json
```

Exit3 means at least one scope is blocked. The report's `roles[].revisions` supplies the seven named revisions to the existing live exporter. `envelope_inputs_ready` does not replace assurance validation, the trust twin, original holds, 429 hard-stop behavior, or consent. The private live database schema is not present in this package, so there is no claim that the mapping is installed on that server.

## Import observations and run a review

The producer writes a body containing exactly `flow`, `assurance`, and `trust`. These are real exports from the existing components, or null for a missing assurance/trust export. Missing components can be observed but cannot pass the review gate. The producer supplies real capture and expiry times; Keel never changes them to make old evidence fresh.

```sh
python3 -m keel_agent snapshot-seal snapshot-body.json --home /absolute/private/keel-agent --workspace YOUR_WORKSPACE --source-revision ACTUAL_SOURCE_REVISION --sequence 1 --captured-at ACTUAL_CAPTURE_TIME --expires-at ACTUAL_EXPIRY_TIME --out signed-snapshot.json
python3 -m keel_agent snapshot-import signed-snapshot.json --home /absolute/private/keel-agent --workspace YOUR_WORKSPACE
```

The lifetime is at most 90 seconds, sequences start at 1 and advance contiguously, and replay is rejected. Signing authenticates possession of this host key; it does not authenticate the upstream producer's facts. Keep observation collection and signing in trusted host code, separate from model output.

`bundle-input.json` contains exactly a `bundle` object: `workspace_id`, `role_id`, `action`, `destination`, `account_id`, `revisions`, `content`, and `attachments`. The existing bundle action is `SIMULATE_SUBMISSION`. `content` contains the exact application contract; attachments map reviewed file names to relative paths inside the input folder. Construct revisions with `keel_agent.scope.material_context_revisions`; do not transplant legacy full-observation hashes. Retain existing genuine authority and human receipts unchanged.

```sh
python3 -m keel_agent enqueue-review bundle-input.json --config reviewers.json --home /absolute/private/keel-agent --workspace YOUR_WORKSPACE --idempotency-key MATERIAL_OPERATION_ID --round-id UNIQUE_REVIEW_ROUND
python3 -m keel_agent worker-once --home /absolute/private/keel-agent --workspace YOUR_WORKSPACE
python3 -m keel_agent preflight --job-id RETURNED_JOB_ID --home /absolute/private/keel-agent --workspace YOUR_WORKSPACE
```

Refresh the signed observation export after a long review before preflight. A genuinely reobserved lead may differ from the stored original only in its top-level `observed_at`; source observations, evidence, facts, policy, approvals, route, attachments, and identity remain bound. Do not rewrite the original receipt's lead hash. A worker crash can be reclaimed after its lease, but a partially created review round is not automatically rerun or replaced. Investigate the failed job and create a new explicit round when justified.

The CLI returns 0 for successful processing, 2 for invalid input/runtime errors, and 3 for blocked/failed checks. A COMPLETED job means its processing finished; read its review verdict. Preflight success still reports `execution_authorized:false`.

## Browser boundary

See `LOCAL_BROWSER.md` for free dependencies and fixture verification. Public pages are prepare-only: scripts disabled, exact-origin loading, and all network sealed before private values or attachments are filled. That restricted mode is intentionally incompatible with many dynamic or authenticated ATS pages. An account marker is not a verified authenticated session.

The `approve-prepare` command records an explicit local operator decision only after successful preflight; it does not establish an external authority identity. `prepare-browser` requires that one-use decision, rechecks current evidence, and compares every answer, attachment byte, destination, and account against the persisted reviewed bundle. There is no public-site submit command. Installing dependencies does not make browser evidence verified; run the actual fixture on the target host.

## Verification and next steps

1. Map the seven real source families and rerun the fresh export. Review categories and unresolved actual decisions before generating truthful envelopes.
2. Install two appropriate local model families and run a small, held-out set of real tasks with known outcomes. Measure erroneous passes, abstentions, latency, and memory usage. Synthetic protocol tests do not measure intelligence.
3. Run the rendered browser fixture and authenticated site-specific adapter tests on your host. Keep external preparation disabled until those tests pass.
4. Shadow-run alongside the existing pipeline, preserving all holds and consent. Production activation requires separate real evidence for source authenticity, account identity, recovery, and the submission boundary.

This release does not establish state-of-the-art quality or a competitive lead. The defendable advantage is your verified source data, evaluated research-backed rules, reliable execution, and measured results on your own workload.
