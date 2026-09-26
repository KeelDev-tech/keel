# Complete typed preparation and a restricted action gateway

This additive package prepares and verifies a **complete captured synthetic form**. It does not discover arbitrary employer forms, authenticate employer accounts, submit applications, or certify authentic data. Existing Keel packages and gates remain unchanged.

The frozen workload has 12 fields, including text, select, radio, checkbox, conditional fields, a résumé upload and a human attestation. Eleven fields are active for the default values; the twelfth is explicitly inactive. An optional empty answer and a declined optional recording consent are deliberate approved choices. They are never inferred.

## Trust boundary

The host must load the captured form, seven current revision hashes, actual evidence, current policy snapshot and genuine human preparation approvals from its trusted upstream workflow. **Do not accept these objects from model output as authorization.** The model supplies proposed field values only. An approval pins the complete contract, field, exact value, evidence hashes, actor and validity interval. Hash equality binds supplied records; it does not prove their truth or authenticate a human.

`ActionGateway` is held by the trusted host. Its constructor, `update_snapshot()` and approval inputs must not be exposed as model tools. The model must not hold a shell, browser handle, arbitrary Python executor, host policy setter, or gateway signing key. The host must refresh canonical state before each effect. A stale cached snapshot cannot reveal a revocation the host has not loaded.

The Python module does **not** supply OS process isolation, a hostile-code sandbox or an operating-system egress firewall. Playwright routing restricts page requests within this worker, but cannot prove that the browser process made no background network requests. The report explicitly leaves OS traffic unmeasured whenever rendering is attempted.

## API

```python
from keel_loki.forms import build_plan, inventory_blockers, validate_readback
from keel_loki.actions import ActionGateway

# All except proposed_values are trusted host inputs.
diagnostic = inventory_blockers(contract, proposed_values, host_approvals, now=now)
gateway = ActionGateway(contract, host_snapshot, host_approvals)
grant = gateway.issue(proposed_values, now=now, ttl=60)

# Host refreshes current policy before each effect, validates live form scope,
# then consumes each action exactly once before invoking the fixed adapter.
for proposed_action in grant["actions"]:
    gateway.update_snapshot(load_current_host_snapshot())
    action = gateway.consume(grant["capability"], proposed_action, now=current_time())
    # fixed_adapter.apply(action) -- no model-generated code or URL
```

The example intentionally leaves host authentication, canonical snapshot loading and live form capture to Keel's actual integration. Do not replace those with synthetic fixtures. Existing consent and submission workflows retain all authority.

`inventory_blockers()` reports every captured field, including missing values, unresolved conditional dependencies, invalid human approvals and unexpected values. It grants no authority. `build_plan()` refuses incomplete or invalid input. Even optional active fields need an explicit approved choice. Conditions reference earlier fields, so forward references and cycles are rejected. Active no-AI and unaided fields are blocked for this automated pathway; they need the separate human workflow.

`validate_readback()` strictly compares identity, origin, account, all fields, exact value types, activation state, attachment hash and absence of unexpected controls or submission. It returns `MATCH` or `MISMATCH`. A comparison of injected JSON cannot produce a rendered-browser validation claim. The plan is independently checked for complete field coverage before any comparison.

## Preparation capabilities

Capabilities bind one full plan, exact ordered actions and an expiration no later than any underlying human approval or host snapshot. They are signed with a process-local random key. Actions are limited to `set_text`, `select_option`, `choose_radio`, `set_checkbox`, `set_attestation` and `attach_file`. No submission, shell, arbitrary script, navigation or network operation exists in this vocabulary.

The gateway checks consent, current approval, no-AI policy, all holds, unknown attempts, the 429 hard stop, origin/account/form scope, seven revisions and revocations at issuance and again before each effect. Forged, modified, expired, revoked, replayed and out-of-order actions fail closed. Consumption is thread-safe and happens **before** the browser call. An effect followed by an error is uncertain, not automatically retryable. Revoke the capability and obtain a fresh verified readback. Process restart invalidates old capabilities; grants are not transferable between workers.

Human attestations require the same exact scoped human approval as other values. The fixture generates synthetic approvals solely for rehearsal; they cannot support a claim that any real person attested to an application.

## Local executable rehearsal

```python
from keel_loki.browser import run_fixture
report = run_fixture("/absolute/existing-parent/new-run-directory", render=False)
```

The destination must be new, under an existing nonsymlink parent. It receives private `contract.json`, `plan.json` and `report.json`. The default performs no model, browser or network work. It returns `PARTIAL`, valid typed preparation, and rendered browser `NOT_RUN`.

For an actual installed local browser:

```python
report = run_fixture("/absolute/existing-parent/another-new-run", render=True)
```

This opts into a nonce-bound temporary loopback HTTP fixture and the locally installed Python Playwright/Chromium runtime. The code downloads nothing. Missing Playwright or Chromium is `UNAVAILABLE`, never `PASS`. A real launch or readback error remains visible.

The worker checks the complete DOM contract before every typed action, verifies active and inactive fields, re-hashes actual uploaded bytes from the browser's File object, then compares exact readback. It has no submit button and prevents submission events; request routing allows only the exact loopback fixture document. A rendered `PASS` means **this frozen synthetic full form** was prepared and read back. It does not mean a real application was prepared, a model generated correct answers, or an external site is supported.

No injection adapter is accepted by `run_fixture`. The report can claim rendered success only on the actual Playwright code path. Unit tests of pure comparisons remain offline tests and are described that way.

## Remaining integration work

The Keel host must provide authenticated approval records, current source revisions, grounded value capture, exact employer-form contracts, durable uncertain-attempt handling and real browser runtime. Employer layouts, custom controls, authentication, CAPTCHAs and unsupported form semantics require additional adapters and tests. The current schema supports conjunction-free equality dependencies on earlier fields, not every possible JavaScript validation rule. Unsupported semantics must remain blocked. This release adds an executable complete fixture and a reusable typed boundary, not general website compatibility.
