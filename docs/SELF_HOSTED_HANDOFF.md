# Keel 0.7 integration handoff

Additive candidate based on the supplied unified 0.6 release. The user's live integration status and 753 tests + 341 subtests report are received claims, not this package's test counts. The live repository and 837-lead export were not available for independent inspection here.

First port `keel_agent/revisions.py` and the exact mapping in `SEVEN_REVISIONS.md`. Normalize real policy/form/answers/attachments/target/approval/route records. Publish the resulting source-bound revisions through the existing canonical export adapter. Keep missing records, revocation, expiry, authority, human consent, and original holds visible. Do not map `envelope_inputs_ready` to permission or application readiness.

Then port `keel_agent` and the browser worker plus fixture tooling. `python3 -m keel_agent` composes snapshots, durable REVIEW jobs, operator-configured local inference, material-bound preflight, and separately approved browser preparation. Existing 0.6 packages and APIs remain intact. Source timestamps and receipts are never refreshed by this runtime.

Local state and approval authenticate the host OS account only. The receiving host must authenticate upstream sources and real human decisions. Local model endpoints do not establish offline behavior or independent weights. Strict no-cloud operation needs model-server cloud disabled and outbound traffic denied by the host.

Read `SELF_HOSTED.md` for commands; `LOCAL_MODELS.md`, `LOCAL_STATE.md`, `LOCAL_BROWSER.md`, and `SEVEN_REVISIONS.md` provide contracts. The included release evidence distinguishes unit/contract tests, actual loopback HTTP, synthetic state rehearsal, real model inference NOT_RUN, and rendered browser UNVERIFIED.

No live deployment, real model benchmark, canonical writes, or external submissions are claimed. Do not replace the live tree wholesale without reviewing the additive diff and adapting the actual source mapping. The next release gate is actual seven-source export plus target-host model and browser evidence.
