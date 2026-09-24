# Loki experiment laboratory and procedural workshop

These additive modules implement the laboratory, verified procedural learning and research bridge from the 0.12 roadmap. Their runtime uses Python's standard library and Keel's existing local model adapter. Nothing installs weights, reaches a paid API, grants application approval or changes canonical applicant records.

## Adversarial evidence laboratory

`keel_loki.lab.mutation_dataset()` creates eight **synthetic development** cases: original support, missing support, explicit contradiction, expired support, wrong target, irrelevant persuasion, embedded hostile instructions and conflicting support. Transformations preserve subject and target identities while changing only the intended condition. Their labels follow the fixture scenario; they do not validate real applicant evidence. Custom seed values remain fictional scenario constants.

`make_lab_plan(dataset, configs, expected_dataset_sha256=..., mode=...)` freezes the full case manifest, gold labels, dataset identity and provenance, three declared model configurations, critic policy and resource limits before a run. Modes are `DISABLED`, `INJECTED` and `LOCAL_LOOPBACK`. Record the plan digest separately. Use `run_lab(..., expected_plan_sha256=...)` to reject changes after this review. `configs` has exactly `drafter`, `reviewer` and `second_reviewer`, each an existing `ReviewerConfig` or its complete JSON dictionary. Distinct names do not establish independent model errors.

For every case the runner collects three blind assessments before showing any peer output. It then runs a reviewer revision against the drafter and a second revision against the first review, including each reviser's own frozen first response. Peer opinions are separate from original evidence and explicitly untrusted. Requests never include gold labels, rationales, case IDs, the case manifest or the labelled dataset digest. Final reports contain transcript hashes and measured timing rather than prompt text or generated findings.

Calls are opt-in. Default execution reports every planned stage as disabled. A real run uses the pre-existing exact literal-loopback adapter; supplied callables are labelled `INJECTED`, even if a callable itself delegates elsewhere. The caller must trust injected Python code. There is no retry or model replacement. HTTP 429 stops every later role and case. Call and wall limits keep unrun stages visible as errors. A configured model deadline must fit the remaining wall budget before a call starts. Up to 64 cases and 1,024 calls are accepted.

Metrics keep errors distinct from a valid model ABSTAIN. Every correction denominator includes every case; missing assessments are counted as unmeasured transitions. Wrong-to-correct, correct-to-wrong and retained-error transitions are separate. Useful PASS is measured among supported labels. `false_pass` uses the non-PASS-label denominator; `false_pass_all` uses every case. An always-abstaining model has zero useful PASS. Neither zero observed errors nor agreement proves safety, factual truth, independence or generalization.

`validate_report(report, expected_plan_sha256=..., dataset=...)` checks complete ordered case coverage, labels and subject hashes against the pinned manifest, fixed provenance, recomputed metrics, timing types, attempted-call evidence, budgets and global 429 state. A self-consistent JSON object is not independently authenticated run evidence: use an externally retained plan pin and dataset when consuming transferred results.

## Bounded proposal and selection loop

`optimize(dataset, configs, ...)` evaluates up to three fixed critic instructions: evidence first, contradiction first and scope first. It accepts development data only, applies a shared call budget and stops the candidate sequence after a rate limit. Every candidate is explicitly evaluated or recorded as not run. Incomplete candidates cannot win. Selection first reduces false PASS and harmful corrections, then increases useful PASS and exact agreement. It produces a proposed selection and never promotes it or evaluates a final held-out partition.

`propose_candidates(config, development_report, ...)` optionally makes one local model call using only aggregate development metrics and the fixed policy allowlist. It rejects code, added permissions and unknown candidates. A rate-limited feedback run cannot trigger a proposal call. Proposal call counts are explicit; a receiving host must account for this additional call when orchestrating a combined optimization campaign. Repeated tuning consumes development data, not fresh evidence of generalization.

## Procedural skills

`SkillWorkshop(home)` maintains a private append-only event chain. Supported operations are `freeze_fixtures`, `quarantine`, `replay`, `promote`, `active` and `rollback`. Updates take a directory lock and exclusively publish complete private event files. Every operation checks the event chain. A filesystem owner can still replace history; the chain provides consistency checking, not third-party attestation.

A recipe comes from a supplied observed trace and begins `QUARANTINED`. Its version binds the trace digest, workspace, HTTPS origin, account, role, form revision, observation time, expiry, typed preconditions and steps. Steps are only `locate`, `fill_approved` and `attach_approved`; a section can only be located. No shell, eval, arbitrary file access, network navigation, fact editing, policy editing or approval action exists in the interpreter.

Replay acts solely on **in-memory reference slots** in frozen local fixtures. It checks exact scope and form revision, expiry, enabled controls, approved reference identity and all required final readbacks. Unknown required fields and wrong attachment references fail. This is not rendered browser execution or independent evidence that a reference is authorized. Any real execution must separately pass Keel's complete form and action gateways, including consent, attestations, no-AI rules and existing human approval controls.

At least two distinct fixture states must be frozen before a candidate is created. Changing only fixture IDs or observation timestamps does not make distinct variants. The same corpus cannot evaluate a different recipe after it has already been used; a changed candidate needs a fresh frozen corpus. Fixture independence itself is not authenticated. Promotion consumes a matching, internally stored, passing replay event and cannot accept a caller-supplied PASS boolean. It binds the exact recipe version and expires with that recipe. Retrieval requires current scope and form revision. Rollback can deactivate a skill or select a previously promoted, unexpired version.

## Research bridge

`research.propose(...)` records the paper reference, proposition, falsifiable hypothesis, comparator and experimental stages, metric, expected direction, dataset digest and experiment plan digest. Prepare it against `make_lab_plan` before a real experiment. `record_result` requires the retained hypothesis digest and experiment plan digest, validates the run and recomputes the observed comparison. It propagates synthetic/injected provenance and labels missing stages `INCOMPLETE`.

A directional difference is an observation on that run, not statistical significance, a causal effect, proof of a research proposition or evidence of a broader cognitive benefit. Preregistration is not independently attested by these local records.

## Offline demonstrations

```python
from keel_loki import lab, skills, research
lab_report = lab.demo()                         # Explicitly injected synthetic responses
skill_report = skills.demo('/private/new-home') # Local reference replay; no browser
research_report = research.demo()               # Observed zero change; no proven benefit
```

Run `tests/test_loki_lab.py`, `tests/test_loki_skills.py` and `tests/test_loki_research.py` with the existing test environment. Independent adversarial regressions also live in `tests/test_loki_review.py`. Real model quality, rendered behavior, authentic labels and live integration require separate host runs and remain unestablished by these demonstrations.
