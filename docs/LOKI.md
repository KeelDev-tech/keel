# Keel 0.12.0: local capability expansion

This is an additive integration candidate over the complete 0.11 reference.
It implements executable mechanisms in all eight roadmap areas. It does not
include model weights, a browser installation, a TLC distribution, a universal
employer-site adapter, authenticated human decisions or a production deployment.
No paid model API or hosted service is required by the new runtime modules.

| Roadmap area | Implemented code | Honest operating boundary |
|---|---|---|
| Complete preparation | `keel_loki.forms`, `actions`, `browser` | All fields in a captured typed contract; actual renderer is restricted to the supplied complete loopback fixture |
| Temporal evidence | `temporal`, `integration` | Immutable local observations, historical lookup and stale dependency projection; host authenticates sources and applies canonical changes |
| Question planning | `questions` | Deterministic shared-fact/dependency/fairness ranking and offline policy estimates; no fabricated answers or approval reuse |
| Adversarial experiments | `lab`, `research` | Labelled mutations, blind model stages, correction metrics and pinned hypothesis results; scripted responses are INJECTED |
| Procedural learning | `skills` | Versioned reference-based recipes, quarantined replay, promotion and rollback; replay is a finite interpreter, not real browser training |
| Restricted actions | `actions` | Typed, scoped, signed, expiring and ordered one-use preparation capabilities; this is not an OS sandbox |
| Model routing/calibration | `routing`, `calibration` | Local two-stage review, persistent budgets/429 and finite-family binomial risk estimates; statistical recommendations grant no authority |
| Formal progress/recovery | `modelcheck`, `recovery`, `formal/` | Executed finite Python state exploration, counterexamples, trace conformance and crash rehearsal; external TLC remains a separate check |

Additional `retrieval` code provides local lexical retrieval and optional
content-pinned supplied-vector fusion. It filters scope, use, expiry and
verification observations before ranking. It does not install an embedding
model; cosine similarity and source hashes are not truth scores.

Existing 0.11 modules and evidence remain unchanged. Older audit reports describe
their own release scopes. All real execution continues to require the actual
host's consent, no-AI/unaided-work, approval, hold, duplicate, UNKNOWN and 429
controls. A successful new report cannot substitute for any existing gate.

## Start with the offline rehearsal

Use existing parent directories and new output paths. Runtime state must be
outside the release source. The default demonstration makes no model, browser,
network or subprocess call; it uses clearly synthetic data and injected model
responses to exercise actual validators and persistence.

```sh
python3 -B -m keel_loki demo --home /private/new-loki-demo --out /private/new-loki-demo-result.json
python3 -B -m keel_loki inspect-host --out /private/new-loki-host.json
python3 -B -m keel_loki modelcheck --out /private/new-loki-modelcheck.json
python3 -B tools/run_loki_acceptance.py --out /private/new-loki-acceptance
python3 -B tools/run_loki_checks.py --out /private/new-loki-checks
```

The full guarded checks use the existing development dependencies and unchanged
guarded suite runners. They reject network activity during tests. A test audit
hook is not OS-level containment.

The top-level demonstration runs each module and an explicit integration chain:
actual local fixture bytes → recorded temporal fact → verification observation
→ exact approved field → complete preparation plan. A later correction makes
the old binding unusable. That chain enumerates one factual field; it explicitly
does not claim every applicant statement has been extracted and grounded.

## Command input and output

Every CLI result is wrapped in `keel.loki.command.v1` with the operation result
under `result`, actual release inventory pin, source-change status and false
execution/deployment flags. Store the inner result when supplying it to another
module API; do not feed the wrapper to a strict inner-schema validator.

An existing output is refused before work starts. A source change during an
operation produces `keel.loki.invalid-command.v1`, quarantines the result and
returns exit 4. Exit 2 means invalid input/configuration/output; exit 3 indicates
an observed blocked, incomplete or unavailable operation; exit 0 means the
requested operation completed, not that model quality or production readiness
was established. Detailed module results remain authoritative.

Most `--input` files use this closed request shape:

```json
{"operation":"method_name","arguments":{}}
```

Only the following explicitly enumerated methods are callable. Parameters and
full data schemas are documented in the companion module documents.

| Command | Allowed operations / input | Companion documentation |
|---|---|---|
| `forms-plan` | Direct `{contract, values, approvals, now}` object | `LOKI_FORMS.md` |
| `forms-inventory` | Direct `{contract, values, approvals, now}` object | `LOKI_FORMS.md` |
| `forms-readback` | Direct `{contract, plan, observed}` object | `LOKI_FORMS.md` |
| `questions` | `plan_questions`, `check_fact_reuse`, `evaluate_outcomes`, `evaluate_policy` | `LOKI_MEMORY.md` |
| `memory --home ...` | `record_claim`, `record_observation`, `register_artifact`, `register_approval`, `resolve`, `invalidation_projection`, `verify` | `LOKI_MEMORY.md` |
| `retrieval` | `search` | Function schema in `keel_loki/retrieval.py` |
| `lab` | `mutation_dataset`, `run_lab`, `optimize`, `validate_report` | `LOKI_LEARNING.md` |
| `skills --home ...` | `quarantine`, `freeze_fixtures`, `replay`, `promote`, `active`, `rollback` | `LOKI_LEARNING.md` |
| `research` | `propose`, `record_result` | `LOKI_LEARNING.md` |
| `route` | `make_policy`, `run_route` | `LOKI_ROUTING.md` |
| `calibration` | `make_plan`, `fit`, `evaluate`, `invalidate` | `LOKI_ROUTING.md` |
| `trace-check` | `replay_trace` | `LOKI_FORMAL.md` |
| `recovery --home ... --workspace-id ...` | `demo`, or read-only `snapshot` / `reconciliation_proposal` | `LOKI_FORMAL.md` |

Only `lab` and `route` accept `--allow-model-calls`. Input JSON cannot enable it
or inject an executable transport. The local model configurations use the
existing literal-loopback adapter and strict response parser. Real model runs
must be permitted by the host. For `route run_route`, also supply `--state` with
one operator-controlled durable state path. Never make a fresh state path to
evade a prior 429, budget or uncertain in-flight result.

The route and calibration CLI insert/check the actual source inventory pin.
Capture a policy or calibration plan first, preserve its hash and pass its
explicit expected hash for execution/evaluation. The lab records dataset,
configuration, mode, synthetic/split provenance and the complete case manifest
inside its plan. Preserve the emitted plan hash for later validation and research
comparisons. Hashes establish internal consistency, not issuer authentication.

Recovery controller methods are a long-lived trusted-host Python API. Opening a
new writable `RecoveryJournal` represents a controller restart, fences old
leases and preserves uncertain effects. Consequently the CLI does not expose
`claim` and `start` as separate short-lived commands. Its read-only view does not
advance controller generation or mutate the journal.

## Browser host trial

```sh
python3 -B -m keel_loki browser-trial --home /private/new-loki-browser --render-browser --out /private/new-loki-browser-result.json
```

This invokes installed Python Playwright and Chromium against a fresh nonce-bound
loopback fixture. The captured contract contains 12 fields: 11 active and one
explicitly inactive conditional field. The fixture includes text, select, radio,
checkbox, attestation and attachment controls. Missing runtime dependencies are
UNAVAILABLE, not PASS. The default without `--render-browser` is an offline typed
plan rehearsal and remains PARTIAL.

The renderer has no submit operation. It checks the DOM inventory and values,
account/origin/contract bindings, attachment bytes, unexpected controls and
attempted submission signals. Real website discovery, custom widgets, candidate
authentication, unaided answers, CAPTCHA handling and provider receipts still
need reviewed host adapters and real inputs. A complete fixture is not proof
that an arbitrary employer's application is supported.

## Evidence and learning boundaries

Temporal records and approvals are observations supplied by a trusted host.
They are not created by a model. Database integrity checks do not prove factual
truth, authenticate the reviewer or resist an owner who replaces every local
file and checkpoint. Protect the store and preserve independent checkpoints.

The skill workshop learns preparation references and procedures only. It cannot
write applicant facts, change policies, approve applications or execute arbitrary
Python/shell code. Its replay checks are real interpreter executions against
frozen fixtures, but fixture labels, trace provenance and independence remain
host responsibilities. No successful replay becomes real browser evidence.

The optimizer tries a bounded whitelist of critic-policy variants. It does not
train model weights or run Agent Lightning/GEPA as an installed dependency.
The research bridge records observations against a pinned hypothesis; it never
declares the user's paper proven, causal benefit established or reviewers
statistically independent.

Calibration uses a predeclared finite threshold family with one-sided binomial
upper bounds and Bonferroni correction. This is not an implementation of
anytime-valid conformal risk control. Its stated IID/label/selection assumptions
must be assessed separately, and configuration changes invalidate the fit.

The finite model checker reports explored states, transitions, invariants,
progress assumptions and counterexamples. TLA+ source/configuration is supplied,
but running Python exploration does not run TLC. A finite model and selected
implementation traces do not prove the whole system or an external website.

## Receiving-agent integration contract

1. Verify and extract the transfer into a new directory; preserve its hashes and
   release evidence. Use the additive installer only against an unchanged 0.11
   reference. Review a modified live tree rather than resetting real files.
2. Run the new acceptance and complete guarded suites. Inspect every component's
   measured result and unavailable status, not merely its process exit code.
3. Load real policy, source, form, answer, attachment, target, approval and route
   revisions from the host's authoritative stores. Do not manufacture approvals
   or use synthetic records to unblock live applications.
4. Bind the typed gateway to authenticated host state immediately before effects.
   The planner receives data-only interfaces. It must not receive direct browser,
   shell, credential or network handles that bypass the gateway. Python checks
   are not an OS sandbox; the host must enforce process and network separation.
5. Configure every model/embedding/reranker locally, disable cloud fallback and
   external telemetry, then measure real egress behavior. A loopback URL is not
   proof that the separate inference server never contacts an external service.
6. Perform the complete fixture trial on the host, then a reviewed employer
   adapter trial using authorized records. Preserve per-application human review
   and all prior holds. Do not enable submission based on this package alone.
7. Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED separately.
   Keep real model quality, authentic evidence, browser coverage and TLC results
   separate. No code path in this release grants deployment/publication authority.

## Research provenance

The roadmap informed these Keel-native implementations; dependencies below are
references, not bundled installed services or claimed benchmark results.

- Temporal memory: https://arxiv.org/abs/2501.13956
- Procedural memory: https://arxiv.org/abs/2409.07429
- Evolving playbooks: https://arxiv.org/abs/2510.04618
- Reflective optimization: https://arxiv.org/abs/2507.19457
- Control/data separation: https://arxiv.org/abs/2503.18813
- Browser research: https://github.com/ServiceNow/BrowserGym
- Local inference: https://github.com/ggml-org/llama.cpp
- Formal model checking: https://docs.tlapl.us/using:tlc:start

Trent's ECV, cognitive-risk and reliability research remains a source of testable
engineering hypotheses. Existing ECS and ACR code is retained unchanged.
