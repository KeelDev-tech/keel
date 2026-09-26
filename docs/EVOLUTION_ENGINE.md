# Keel Evolution Engine

`keel_evolve` turns bounded local experience into reviewable candidates. It
stores no model weights, calls no model, contacts no service, and grants no
execution authority. Runtime dependencies are Python and SQLite from the
standard library.

## Lifecycle

1. `observe` records an idempotent evidence identity, outcome, context and
   source digest.
2. `propose_lesson` or `propose_procedure` creates a `CANDIDATE` bound to
   existing evidence.
3. `tournament` compares declared held-out results. Passing this screen moves
   an artifact only to `SIMULATION`; it is not returned by production memory
   retrieval.
4. `qualify` invokes Keel's pinned paired reliability harness. Promotion to
   `PROMOTED` requires observed local callbacks, a non-synthetic held-out
   dataset, host-attested adjudication and independence, at least the frozen
   statistical gates, no false-pass/error/instability threshold breach, and a
   qualifying lower confidence bound.
5. `retire` removes a promoted artifact from retrieval while retaining its
   evidence history.

Promotion establishes performance only for the qualified frozen task
distribution and exact runner pins. It does not authorize actions or establish
general production quality.

## Replayable procedures

A procedure records inert steps, exact required state, the only inputs that may
be rebound, validator hashes and its evidence. Replay rejects candidates,
retired procedures, changed state and missing or extra inputs. A simulation
screen can produce only a simulation plan. Every plan has
`execution_authorized: false` and requires fresh authority.

`bind_action_contract` binds a plan to exact target, evidence and authority
revisions. `validate_action_contract` reports `STALE` when any binding changes.
A current contract still reports `CURRENT_REQUIRES_ACTION_GATE`; the existing
Keel action gate must independently admit any effect.

## Failure laboratory

`failure_to_scenario` converts a caller-supplied sanitized failure, fixture and
invariant into a deduplicated inert scenario. `run_scenario` evaluates only a
fixed path plus `equals`, `not_equals` or `present`; it does not turn text into code.
The caller remains responsible for removing private data and validating the
fixture.

An opt-in scenario index exports only identifiers and hashes. It contains no
fixture content or evidence IDs. Imported indexes remain `QUARANTINED` until an
exact matching local scenario exists. A matching hash identifies equal local
bytes; it does not authenticate the remote author.

## Scheduling

`prioritize` uses declared affected-task count, information gain, success
probability, cost and deadline weight. Investigations that require authority
are ineligible for automatic selection.

`route_compute` assigns deterministic work, standard review, advanced review,
or a budget hold from explicit risk, ambiguity and novelty values. It returns a
plan and makes zero model calls. These declared values are inputs, not measured
truth; hosts should derive them from validated telemetry before operational use.

## Offline demonstration

From an extracted local profile on Linux with Python 3.11 or newer:

```sh
python3 -B -m keel_next evolve --home /tmp/keel-new-evolution-demo
```

The home must be new. The demonstration uses synthetic records and therefore
stops at simulation status. Production promotion requires the `qualify` Python
API with a real held-out dataset and trusted local runner callbacks.
