# Keel 0.11.0 receiving-agent handoff

Status: tested integration candidate, not deployed. This additive release
preserves all 607 members of the supplied 0.10 reference. It adds pinned repeated
trial plans, bounded local-model runs, paired comparisons, exact partition
overlap checks, and a synthetic source-to-model-to-browser rehearsal bridge.
No paid service is added. No model weights or browser dependencies are bundled.

## Receive

The complete package is one Python TXT transfer; no ZIP is required.

```sh
python3 -B Keel_0.11.0_Transfer.txt --verify-only
python3 -B Keel_0.11.0_Transfer.txt --out /existing-parent/new-keel-0.11
```

Use a new extraction directory. Read `docs/BENCH.md` and `docs/BENCH_HOST.md`.
The separate Release Evidence JSON records measured test counts, synthetic
rehearsal outcomes, exact source inventory and transfer hashes. Historical audit
reports retain their original release scope.

## Check and port

The installer expects an unchanged 0.10 reference. It checks 606 prerequisite
members, excluding historical root MANIFEST.json packaging metadata. All 607
predecessor files are retained in the reference; no host file is overwritten.

```sh
cd /existing-parent/new-keel-0.11
python3 -B tools/install_bench.py --target /path/to/clean-0.10-reference
python3 -B tools/install_bench.py --target /path/to/clean-0.10-reference --install
```

The first command is read-only. Installation is additive and idempotent;
conflicting existing files or changed prerequisites block it. For a modified
live host or an older release, review the additive port against the actual tree
and retain all host controls. Do not reset real configuration to match hashes.
Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED separately.

Run the unchanged guarded suites and new offline acceptance on the actual port:

```sh
python3 -B tools/run_bench_checks.py --out /existing-parent/new-checks
python3 -B tools/run_bench_acceptance.py --out /existing-parent/new-acceptance
python3 -B -m keel_bench demo --out /private/new-synthetic-comparison.json
python3 -B -m keel_bench inspect-host --out /private/new-host-inspection.json
```

Free development test dependencies remain in requirements-dev.txt. The scripted
demo tests measurement behavior, not model quality. It intentionally accepts
unsupported claims so false-PASS accounting can be verified.

## Run the missing host measurements

1. Prepare separately reviewed development and held-out datasets from authentic,
   authorized records. The partition audit catches exact overlaps; its success
   does not certify independence or truth.
2. Supply installed, licensed local models. Freeze an experiment with the exact
   dataset, source, configurations, repeated trial schedule and resource limits.
   Preserve the emitted plan hash and use the explicit model-call opt-in.
3. Inspect comparison reports: false PASS, supported cases withheld, error and
   abstention counts, repeated-task consistency and latency. A zero-error exit
   is not a model acceptance decision. Report hardware and human time separately;
   this version does not measure those operating costs.
4. Supply Node, Playwright and Chromium, then run `host-trial --render-browser`
   in a new private directory. Optionally supply `--model-config` together with
   `--allow-model-calls` to connect a real reviewer to that same trial.
5. Expand host validation to complete forms and genuine human decisions. This
   release's rendered rehearsal covers one grounded Motivation field and exact
   packet attachment. It never submits and always declares full_application_trial
   false. Missing runtime dependencies remain unavailable, not passed.

Real inference, rendered preparation, authentic labels/data and live integration
have not been established by this delivery. No current report establishes
competitive superiority or state-of-the-art performance. Existing consent,
unaided-work, approval, unknown-attempt, hold and 429 rules remain in force.
