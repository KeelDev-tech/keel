# Evidence, scoped local work and a pipeline twin

Version 0.5.0-review.1 applies evidence-first, uncertainty-aware, least-data, human-ownership and validation themes to Keel. The earlier digital-twin work supplied
architecture and review principles; it did not supply deployed agents, live
synchronization, calibrated simulations or functioning n8n/PostgreSQL/Qdrant
services. This release implements useful pieces without those dependencies.

## What the twin contributes

| Capability | Implementation | Useful decision |
|---|---|---|
| What-if testing | Clone a hash-bound export, change assumptions, rerun actual flow reducers | Will delayed verification or faster consumption empty ready work before refill? |
| Change detection | Compare timestamped exports, role fields and metrics | Did materials, gates or inventory change readiness? |
| Incident replay | Run fixtures at recorded times against explicit expectations | Does a code change reintroduce false readiness or ignore uncertain attempts? |
| Governed review | Bind a proposal to evidence, challenge and human ownership | What is supported, disputed or awaiting more evidence? |

This is a **snapshot-driven pipeline model**, not a behavioral replica of Trent
or a continuously synchronized production twin. Forecasts are conditional on
supplied capacity, timing and conversion. Calibration and throughput gains are
not established. NIST describes digital representations of entities and their
state changes and the need to trust those representations:
https://csrc.nist.gov/pubs/ir/8356/final.

## Run locally

```bash
python3 -B -m keel_trust report sample_data/trust.example.json --now 2026-09-18T12:00:00+00:00 --format markdown
python3 -B -m keel_trust evidence sample_data/evidence.example.json --now 2026-09-18T12:00:00+00:00
python3 -B -m keel_trust capture sample_data/flow.example.json --now 2026-09-18T12:00:00+00:00 --out /tmp/keel-model-new.json
python3 -B -m keel_trust simulate /tmp/keel-model-new.json --scenario sample_data/twin-delay.example.json
python3 -B -m keel_trust drift /tmp/keel-model-new.json --current sample_data/flow.example.json --now 2026-09-18T12:00:00+00:00
python3 -B -m keel_trust replay sample_data/trust-incidents.example.json
```

The fixed time is for fixtures only. Current exports require actual evaluation
time. JSON is default; `report` also renders Markdown. `--out` refuses overwrite.
Input JSON is bounded to 8 MiB and uses existing strict duplicate-key, nonfinite
number and nesting checks. Invalid input returns exit 2. Replay returns 0 only
for a passing nonempty run, otherwise 1. Other commands return 0 for successfully
generated diagnostics even when they contain holds; success is not permission.

No CLI fetches URLs, calls a model, sends messages, opens a browser or writes a
canonical queue/log. Runtime needs only the standard library. Generate the
synthetic contract with `tools/make_trust_demo.py`.

## Evidence and correction propagation

`evidence.evaluate(export, now=...)` requires the version-1 envelope and exact
fields illustrated in sample_data/evidence.example.json. Envelope completeness
and freshness (0–90 seconds) are distinct from source/claim expiry.

- Sources bind identity, publisher, reference, revision, content hash, origin,
  observation/expiry, status and optional verification reference. Sources are
  data: every output has `can_issue_instructions=false`.
- Claims bind subject/predicate/value hash, revision, kind, basis, review state,
  approval reference, scopes, exact allowed wording, evidence, expiry and explicit
  conflicts. Bases are SELF_ATTESTED, EMPLOYER_STATED, CORROBORATED and
  ADAPTER_VERIFIED. No numeric confidence is invented. Corroboration needs distinct
  declared publishers and cannot rely solely on EXTERNAL_UNTRUSTED sources.
- Claim kinds are FACT, EXPERIENCE and ROLE_REQUIREMENT. Consent is excluded.
  The host must classify honestly and retain separate consent/attestation gates;
  opaque hashes cannot expose a misleading declaration or mislabeled consent.
- Artifacts bind revision, scope, exact claim statements and artifact parents.
  Changed evidence, disputed claims, unapproved wording, scope mismatch, conflicts
  and expiry invalidate reuse transitively. Cycles and their descendants are
  invalid. Parent and child artifact scopes must match.

This reducer does not authenticate documents, interpret natural-language truth
or prove qualifications. Publisher and verification references are host assertions.
The adapter must bind declarations to actual material bytes and approvals.
Historical material is preserved; affected IDs are reported, not silently edited.

`report.build` combines current wrapper, flow/evidence exports, artifact bindings,
question costs, research checks and budgets. Bindings include role_id, artifact_id,
artifact_revision, artifact_sha256 and packet_dependency_hash. Missing, invalid
or changed bindings set packet_present=false in the **derived in-memory view**.
Affected forecast groups are removed in full; affected question bundles cannot
claim an unlock. Stale/partial exports suppress research/question proposals.
The result includes original and gated estimates and a twin of the derived flow.

The host must enforce matching holds at the canonical execution gate. The legacy
`keel_flow` CLI remains unchanged and does not enforce new evidence bindings.

## Local authority and least data

`authority.Gateway` accepts only READ_FIELDS, BUILD_DECISION_BRIEF, RUN_TWIN and
REPLAY_INCIDENT. Host code registers the corresponding local handlers; no
submission, spend, queue-write or browser action exists. The host must ensure
handlers have the promised behavior. This is not an OS sandbox and cannot
constrain code that bypasses its call path.

Trusted configuration supplies current policy, issuer keys of at least 32 bytes,
authenticated principal, record resolver, handler registry and durable grant
consumer. None may come from model/tool request JSON. Only the trusted issuer
calls `sign_grant(payload, key)`. Keep production keys outside source and exports;
test keys are labelled synthetic. Replace the gateway before accepting another
request after a policy revision or calendar pause changes.

Requests bind principal, workspace, action, resource and fields. HMAC-SHA256
grants bind their exact request hash, policy revision, full record hash/revision,
issuer and single-use grant ID, with a maximum 900-second validity interval.
The trusted host clock checks validity. Paused policies block nonessential work.
Signature, identity, policy and field checks precede record resolution. The
resolved record must match identity, workspace, revision, hash and a 1–90-second
freshness bound. Only requested permitted fields reach the handler. Allowlists
operate at the top level, so nested values must be exported as appropriately
narrow fields; handler outputs also need host review.

`CanonicalGrantUse(existing_logger)` consumes the stable grant ID before calling
the handler, using the existing logger's locked event-ID conflict rule. Fresh
per-invocation nonces reject duplicate/concurrent admission even for identical
requests. A crash after admission consumes the grant; investigate before issuing
a new one. Admission does not attest handler completion or provider acceptance.
History must remain complete across restarts and retention/rotation; integrate
the real canonical logger before relying on durable replay protection. Local
POSIX logger concurrency is covered by tests. No second event store is introduced.

External content cannot substitute request fields for policy. That does not prove
a downstream LLM immune to prompt injection. Least privilege and deterministic
checks follow OWASP's direction:
https://genai.owasp.org/llmrisk/llm01-prompt-injection/.

## Evidence → challenge → human review

`review.review_case(case, reviews, registry, now=..., trusted_review_keys=...)`
binds human rationale and owner to a case hash and expiry. Signed reviews bind
identity, role, case, workspace, subject hash, evidence references, decision and
time interval. A trusted roster fixes PRIMARY/CHALLENGE roles and disjoint
provenance groups. Repeated identities cannot manufacture a quorum. Forged,
missing or stale signatures, changed evidence, objections and insufficient
evidence remain visible.

Agreement yields READY_FOR_HUMAN_REVIEW only, never execution authority. Signatures
identify configured key holders; they do not prove independent reasoning or
truth. The protocol does not spawn agents, invent reviews or perform semantic
challenges. There is no CLI accepting production signing keys.

## Time-budgeted decisions and research

`decisions.operator_brief` uses current flow question groups and explicit times
and owners. A greedy planner selects urgent questions and complete dependency
bundles within 0–240 minutes. Only fully selected bundles count conditional role
unlocks, once per role. Missing estimates, unaffordable work and deferred urgency
stay visible. This is not a global optimum. No personal answer is inferred and
consent remains quarantined until an actual valid response.

`research_plan` ranks permitted checks by caller-estimated probability of a useful
decision change × value / minutes. It excludes missing estimate references, low
fit, no decision impact, stale observations and duplicate uncertainty. An explicit
rate hold suppresses the entire proposal. Scores are uncalibrated planning
estimates, not learned odds or permission to make HTTP requests.

## Twin and replay contracts

`twin.capture(flow_export, now=...)` stores snapshot/hash, model version, recorded
time and deterministic baseline. `simulate(model, scenario)` verifies that
capsule by recomputing the baseline, checks source binding and makes a deep copy.

| Change | Bound |
|---|---|
| CAPACITY_MULTIPLIER | Existing supplied measurement, 0.25–4× |
| VERIFY_MULTIPLIER / PREPARE_MULTIPLIER | Existing release timing, 0.25–4×, within underlying limits |
| RELEASE_DELAY_SECONDS | Delay existing release by 0–604800 seconds |
| RATE_HOLD | Introduce a source rate hold; cannot clear one |
| INVALIDATE_PACKET | Change an existing dependency without refreshing its packet hash |
| UNKNOWN_ATTEMPT | Hypothetical uncertain attempt events; no canonical log append |
| OBSERVATION_AGE_SECONDS | Age up to 604800 seconds; never refresh stale evidence |

No fit, consent, policy or scheduling override exists. Unknown attempts on forecast
supply require a fresh reconciled export rather than inconsistent release members.
Floor/check/throttle stay 5/30/600. At most 100 changes are allowed. Results are
SIMULATION_ONLY with before/after metrics, zero writes and no authority. A capsule
is integrity checked, not signed or authenticated to its source.

`drift(model, current_export, now=...)` reports added/removed roles, changed fields
and metrics. Stale new data remains unverified. Observation drift is not forecast
error or calibration.

`replay.run(cases)` requires explicit expected metrics and executes the actual
flow reducer at recorded times. Empty is NOT_RUN; errors/mismatches fail.
`replay.compare(cases, baseline=..., candidate=...)` compares trusted host-supplied
Python callables and flags regressions. JSON never selects executable code.
It proves neither performance gains nor deployment readiness. Host callables are
not sandboxed; use reviewed pure reducers and the isolated regression runner.

## Remaining integration

Supply current canonical/material-byte adapters, enforce holds at live gates,
connect trusted identity/keys/policy and complete grant history, and route reports
through the existing review surface. Repeated real snapshots can measure readiness
losses, supplied throughput, forecast errors, question time and rework. Those
measurements support later calibration; this release includes no learned success
probabilities, autonomous promotion, live synchronization or scheduled jobs.
