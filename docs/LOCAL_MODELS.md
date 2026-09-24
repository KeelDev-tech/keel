# Local model reviewers

Keel can request structured assessments from a local Ollama server or a local
llama.cpp server. The adapter uses Python's standard library. There is no API
subscription, paid provider, API key, automatic model download, or remote fallback.
You supply the hardware, electricity, installed inference runtime, and appropriately
licensed model weights. Model quality and usable speed depend on those choices.

**Validation status: REAL_MODEL_NOT_RUN.** Unit tests use injected responses;
`tools/check_local_models.py` uses real local HTTP with synthetic responders.
These test transport and workflow behavior, not a model's accuracy or throughput.

## Server setup

Start an already installed Ollama server with cloud features disabled:

```bash
OLLAMA_NO_CLOUD=1 OLLAMA_HOST=127.0.0.1:11434 ollama serve
```

If it already runs as a service, put these settings into that service's environment
and restart it; starting a second process does not reconfigure the first one.
Verify the server log says `Ollama cloud disabled: true`. Configure the exact
installed local model name, including its tag. Keel rejects names containing
`cloud`. Ollama supports JSON/schema output on `/api/chat`; Keel requests a
non-streaming response and explicitly disables returned thinking.
[Ollama chat API](https://docs.ollama.com/api/chat),
[Ollama local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).

Alternatively, start an installed llama.cpp server using a local GGUF file:

```bash
llama-server --model /absolute/path/model.gguf --alias keel-local --host 127.0.0.1 --port 8080
```

Select `model="keel-local"` in Keel. The adapter uses
`/v1/chat/completions` with JSON schema output. Do not enable remote model download,
distributed RPC servers, or cloud proxies in an offline deployment.
[llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

Keel's requests go only to literal `127.0.0.1` or `::1`, on an explicit port and
the exact supported backend path. It does not use DNS, environment proxies,
redirects, cookies, or ambient credentials. A localhost service could itself
forward traffic: for a strict offline guarantee deny outbound networking to the
model-server process and verify its configuration. Keel does not attest runtime
binaries, loaded weights, GPU execution, or the server's own logs.

## Configuration and Python API

Keep the roster in operator-controlled configuration. Documents and model output
cannot select endpoints, model names, reviewer IDs, or tools. Two reviewer IDs
using the same model are correlated checks, not independent model families.
The runtime calls `validate_roster_diversity(configs, registry)` to reject obvious
same-model/family contradictions and require a single pair with different model
names and declared methods, families, and independence groups. Case and the
omitted-versus-`:latest` alias cannot disguise the same configured model. Distinct
names can still point to shared weights; this check establishes consistency of
operator declarations, not weight identity or statistical independence.

```python
from datetime import datetime, timedelta, timezone
from keel_agent.models import ReviewerConfig, reviewer_config_digest, run_blind_review
from keel_workflow.reviews import ReviewStore

reviewers = [
    ReviewerConfig("reviewer_a", "ollama",
                   "http://127.0.0.1:11434/api/chat", "installed-model-a:tag"),
    ReviewerConfig("reviewer_b", "llama_cpp",
                   "http://127.0.0.1:8080/v1/chat/completions", "keel-local"),
]
subject = {
    "task": "Evaluate these claims against the supplied source evidence",
    "required_claim_ids": ["experience"],
    "claims": {"experience": "The proposed application statement"},
    "evidence": {"experience": "Verified source content supplied by the host"},
    "reviewer_config_sha256": {
        reviewer.reviewer_id: reviewer_config_digest(reviewer)
        for reviewer in reviewers
    },
}
result = run_blind_review(
    ReviewStore("/absolute/private/state/reviews.sqlite"),
    round_id="unique-round-id", subject=subject, reviewers=reviewers,
    expires_at=datetime.now(timezone.utc) + timedelta(seconds=900),
)
```

The system workflow should build `subject` from the stable material snapshot,
bind reviewer configuration using `reviewer_config_sha256`, then independently
check fresh execution conditions immediately before any action. Changing the
model, endpoint, time/token limit, or roster changes the configuration digest and
requires a new review. Reusing an existing round ID is rejected. Reviewer configs
default to 60 seconds per call, 2,048 generated tokens, and a 256 KiB response.
The maximum round lifetime remains 900 seconds; slower hardware may require
smaller material, model, or roster. Expiration cannot be silently extended.

## Review guarantees and limits

Each reviewer gets a new two-message context for phase A. The host stores all A
assessments as immutable commitments before revealing any to phase B. The model
gets no browser, files, shell, or callable tools. Phase B receives the original
subject and the sealed A opinions; it does not receive hidden model reasoning.

An accepted assessment has exactly `verdict`, `covered_claim_ids`, and `findings`.
The verdict is PASS, FAIL, or ABSTAIN. Every required claim ID must appear exactly
once; findings contain brief unresolved issues. A PASS cannot carry findings.
Unsupported schema, incomplete output, unexpected model name, transport failure,
redirect, timeout, excessive response, or tool call becomes a recorded ABSTAIN.
Keel does not repair malformed output into a PASS or retry it with another model.

Call metadata is persisted in `local_model_calls` in the review database: local
request ID, backend, declared model, endpoint, timestamps, content hashes, phase,
status, and server-declared response identifier. Ollama provides `created_at`, so
that field is explicitly labelled as a timestamp, not a unique server request ID.
The subject and concise assessment findings remain in the existing review tables.
Raw requests, raw model responses, and hidden reasoning are not written to the
call table. The model server may log inputs; configure and protect it separately.

The host controls the roster, request contexts and transport, but cannot prove
statistical reviewer independence or external model blindness. File owners can
read or modify SQLite: use a dedicated local account and private storage. Returned
results always retain `execution_authorized=false`. Even clean reviews are only
ready for human review; they do not create consent, authenticate facts, or grant
the model authority to submit an application.

## Reproduce the checks

```bash
python -m pytest -q tests/test_agent_models.py
python tools/check_local_models.py
```

The HTTP fixture check runs outside the network-disabled unit-test gate and sends
only synthetic content to a server it starts on loopback. A useful next validation
is a held-out, labelled local-model assessment set: record missed defects, false
blocks, abstentions, latency, memory, and the exact model/runtime/configuration.
Do not use a transport-fixture PASS as evidence of real inference quality.
