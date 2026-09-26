# Keel 0.12.0 receiving-agent handoff

Status: tested integration candidate, not deployed. This additive release
preserves all 639 members of the supplied 0.11 reference. It adds a local
capability laboratory with eight connected areas:

1. Complete typed form planning and fixture readback verification.
2. Time-sensitive evidence memory and dependency invalidation.
3. Clarification-question planning and scoped fact reuse checks.
4. Bounded adversarial experiments and pinned research measurements.
5. Versioned skill recipes, replay checks, promotion and rollback.
6. Restricted, scoped preparation actions through a typed gateway.
7. Local model routing and calibration measurements.
8. Bounded state exploration, recovery and implementation trace checks.

These are implementation areas, not eight production certifications. No paid
API is required. Model weights, browser dependencies and the external TLC
checker are not installed or bundled by this release.

## Receive and verify

The complete source package is one Python TXT transfer; no ZIP is required.

```sh
python3 -B Keel_0.12.0_Transfer.txt --verify-only
python3 -B Keel_0.12.0_Transfer.txt --out /existing-parent/new-keel-0.12
```

Extract into a new directory. Read `docs/LOKI.md` for the exact module contracts,
input schemas and commands. The separate Release Evidence JSON records measured
test counts, synthetic acceptance results and exact source and transfer hashes.
Historical audit reports retain their original release scope. Hashes establish
internal consistency; they do not authenticate an author, source or reviewer.

## Check and port

The installer expects an unchanged 0.11 reference and verifies 638 prerequisite
files. Historical root `MANIFEST.json` is retained in the full reference but is
neither required nor installed on a host. No existing host file is overwritten.

```sh
cd /existing-parent/new-keel-0.12
python3 -B tools/install_loki.py --target /path/to/clean-0.11-reference
python3 -B tools/install_loki.py --target /path/to/clean-0.11-reference --install
```

The first command is read-only. Installation is additive and idempotent;
conflicts or changed prerequisites block it. On a modified live host or an older
release, review and port the additions against the actual tree. Do not reset
real configuration or replace host controls merely to satisfy reference hashes.
Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED separately.

Run the unchanged guarded suites and the new synthetic acceptance on the port:

```sh
python3 -B tools/run_loki_checks.py --out /existing-parent/new-checks
python3 -B tools/run_loki_acceptance.py --out /existing-parent/new-acceptance
python3 -B -m keel_loki --help
```

Free development test dependencies remain in `requirements-dev.txt`. A passing
test run verifies the measured behaviors within the supplied reference scope;
it does not establish reliable operation on every site or complete application.

## Use the laboratory

Use new private output locations and synthetic records first. The integrated
`demo` takes `--home` and `--out`. The `inspect-host` command records runtime
observations without converting dependency presence into a successful trial.
The individual commands and their exact inputs are described in `docs/LOKI.md`.

Memory, skill proposals, experiment findings and recovery records do not grant
submission authority. Questions that require the applicant's own words,
attestations or consent must still reach the applicant. Evidence excerpts and
model outputs remain data, not instructions that can redefine policy.

Model runs require locally installed, licensed weights and explicit
`--allow-model-calls`. Review run budgets, rate-limit handling and the exact
model/configuration binding before enabling them. Synthetic or injected
responses do not establish model quality, calibration on real tasks or
competitive advantage.

Rendered fixture trials require installed Python Playwright and Chromium, plus
the explicit `browser-trial --render-browser` option. An unavailable dependency
is reported as unavailable. Planner or simulated readback success is not a
rendered browser result, and fixture preparation is not an application
submission or a proof of arbitrary-site reliability.

The Python `modelcheck` command explores a bounded abstract state model. The
separate files `formal/KeelLoki.tla` and `formal/KeelLoki.cfg` are supplied for
external TLC checking. TLC has not been run for this delivery. Neither the
bounded exploration nor the model text proves the implementation or its host
environment correct. The recovery rehearsal also checks selected actions from
an actual local SQLite journal against the model; it does not establish all
possible recovery paths or behavior on the live host.

## Remaining host work

Supply authentic, authorized records, genuinely reviewed labels and real human
decisions. Run the laboratory with actual installed local models and browser
dependencies. Record results on held-out tasks, including failures, abstentions,
human interventions, latency and the hardware used. Validate live integration
and recovery against the host's real state before enabling any corresponding
production path.

Real inference, rendered preparation, live source capture and live integration
have not been established by this delivery. Existing consent, unaided-work,
approval, unknown-attempt, hold and 429 rules remain in force. Execution remains
unauthorized. No current result establishes state-of-the-art performance,
competitive superiority, independent factual truth or production readiness.
