# Synthetic host rehearsal

`keel_bench.host` connects actual local evidence files, the existing scoped
answer resolver, the packet verifier, an optional local model review, and the
existing rendered browser adapter. All records in this command are explicitly
synthetic. It never opens a public site or submits either a local or an external
application. No dependency or model download is implemented.

This is deliberately a **partial form rehearsal**: the `Motivation` field is
filled with the exact grounded answer, and the exact verified `answer.txt` packet
bytes are uploaded to the fixture's résumé control. Full name, email, work
authorization and accuracy attestation remain empty. A successful readback does
not establish a complete application, authentic human approval or operational
readiness.

## Python API

The trial directory must not already exist; its parent must exist. Each trial
gets a new private directory. Existing files and directories are never reused
or overwritten, and symlink or parent-traversal paths are rejected.

```python
from keel_bench.host import inspect_host, run_host_trial

inspection = inspect_host()
report = run_host_trial("/absolute/existing-parent/new-synthetic-trial")
```

The default reads and verifies real local fixture files and does not start a
server, browser or model. Its status is `PARTIAL`; browser and model are
`NOT_RUN`. Filesystem inspection reports Python, Node, Playwright and configured
Chromium observations without running them. File presence never becomes a
runnability claim. CPU count and physical memory are OS observations, not an
attestation of container limits, available memory, accelerators or capacity.
Energy cost is `NOT_MEASURED`. Configuration values are not copied into reports.

The release CLI saves an exclusive new report file; `--out` must have an
existing private parent directory. For example:

```sh
python -m keel_bench inspect-host --out /absolute/private-parent/inspection.json
python -m keel_bench host-trial --home /absolute/private-parent/new-trial --out /absolute/private-parent/trial.json
python -m keel_bench host-trial --home /absolute/private-parent/new-browser-trial --render-browser --out /absolute/private-parent/browser-trial.json
```

For the optional local reviewer, add `--model-config /absolute/model-config.json`
and `--allow-model-calls` to `host-trial`. The configuration file contains the
existing `ReviewerConfig` fields for your installed local server; supplying it
does not trigger a model download. The CLI binds its before/after release source
inventory to the report as `release_source_sha256`. A passing trial exits zero;
`PARTIAL` and `FAIL` exit 3 and still produce the reviewable report. Filesystem
inspection does not become a runtime pass merely because its report was saved.

To prepare and read back the local rendered fixture:

```python
report = run_host_trial(
    "/absolute/existing-parent/new-browser-trial",
    render_browser=True,
)
```

This uses the existing `keel_agent.browser.BrowserAdapter` and reviewed worker.
Node, a compatible Playwright installation and Chromium must already be
available. The existing `KEEL_PLAYWRIGHT_MODULE` and
`KEEL_CHROMIUM_EXECUTABLE` environment variables can select host installations.
No installation is performed. Missing dependencies remain `UNAVAILABLE`.
Malformed readback, mapping errors and timeouts are `FAIL`, never relabelled as
successful preparation or ordinary absence of a dependency.

The server binds an ephemeral port on literal `127.0.0.1`, generates a fresh
256-bit fixture nonce and starts only after all file checks pass. The fixed
browser worker creates a new browser context and accepts only the reviewed
origin, fixture identity, field value, attachment identity and form fingerprint.
The host command calls only `prepare`. It additionally checks the full returned
readback, refuses submission flags or receipts, and closes the fixture in a
`finally` block. Each call creates a fresh fixture; no sessions, cookies or
application receipts are reused.

## Optional model-to-browser slice

```python
from keel_agent.models import ReviewerConfig
from keel_bench.host import run_host_trial

config = ReviewerConfig(
    reviewer_id="host-reviewer",
    backend="ollama",
    endpoint="http://127.0.0.1:11434/api/chat",
    model="YOUR_INSTALLED_LOCAL_MODEL",
)
report = run_host_trial(
    "/absolute/existing-parent/new-model-browser-trial",
    reviewer_config=config,
    allow_model_calls=True,
    render_browser=True,
)
```

The caller must explicitly request model calls and supply a valid existing
`ReviewerConfig`. The existing evaluation transport permits only literal
loopback endpoints; there is no cloud fallback, redirect, download or retry.
The local server itself remains operator-managed; its weights, identity and
egress are not attested by this client.

After evidence, scoped answer, packet and answer-to-packet checks pass, the
source file is reread with its expected hash. The existing `keel_eval.run_local`
then asks the configured local reviewer to judge one supported synthetic claim
against that verified source value. Labels and rationale are excluded from the
request by the existing evaluator. This is an identity-support smoke test, not
a held-out reasoning benchmark. A valid model `PASS` permits fixture preparation;
`FAIL`, `ABSTAIN`, malformed output, transport error and HTTP 429 block the browser
before its server is created. HTTP 429 is never retried.

An injected `transport` is always `SIMULATED`, even if it delegates to a server.
An injected browser `adapter` is also always `SIMULATED`, even if it is a real
`BrowserAdapter` instance. These injection seams test orchestration without
promoting supplied outputs to evidence of actual runtime execution.

## Report meanings

The report schema is `keel.bench.host-trial.v1`.

| Field | Meaning |
| --- | --- |
| `synthetic` | Always `true`; fixture approval references are not human decisions. |
| `trial_scope` | Always `PARTIAL_FORM_READBACK`. |
| `checks` | Evidence, scoped answer, packet, answer-packet binding and rendered readback; model review is added when requested. |
| `status` | `PASS` only if every requested check passed; `PARTIAL` for missing runtime execution or injected simulations; `FAIL` for a failed check or withheld requested model review. |
| `browser.status` | `NOT_RUN`, `UNAVAILABLE`, `SIMULATED`, `PASS` or `FAIL`. |
| `model.status` | `NOT_RUN`, `SIMULATED`, `PASS` or `BLOCKED`. |
| `model.observed_verdict` | Actual validated assessment `PASS`, `FAIL`, `ABSTAIN`, or `ERROR`; simulated status does not conceal withholding. |
| `planned_field_labels` | The single intended field, `Motivation`. |
| `prepared_field_labels` | Empty until actual rendered readback passes. |
| `simulated_field_labels` | Records simulated readback without claiming preparation. |
| `model_to_rendered_slice_verified` | True only for actual local-model PASS followed by actual rendered readback PASS in this synthetic slice. |
| `whole_form_prepared` / `full_application_trial` | Always `false`. |
| `execution_authorized` | Always `false`. |
| `canonical_writes` | Always zero; fixture directory writes are separate. |
| `external_submission` / `local_submission` | Always false for this prepare-only command. |
| `human_approval_verified` / `truth_independently_verified` | Always false. |
| `live_integration` | Always `NOT_RUN`. |

Hashes bind the trust snapshot, packet manifest, packet bytes, selected answer,
browser contract and readback. Optional model reports bind configuration,
dataset, request, response and assessment and record measured latency. They do
not authenticate a reviewer, attest model weights or authorize dispatch.

The fixture uses a documented fixed synthetic clock so its expiry tests remain
reproducible; that clock is never evidence of fresh live input. Authentic records,
real human decisions, full-form behavior, independent model-quality measurement
and integration into the actual Keel host remain separate validation work.
