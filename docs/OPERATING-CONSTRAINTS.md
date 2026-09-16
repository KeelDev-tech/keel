# Operating constraints

Keel's guardrails as rules, not vibes. Each constraint below is enforced in
code (the module named in parentheses); this document is the human-readable
spine. They are ordered as registered (C-01…C-20); the numbers are stable
so telemetry, code comments, and docs can cite them.

The operator may extend these constraints but never weaken them. Every
constraint is written operator-neutral: "the applicant" is you, the person
running this pipeline on your own machine.

## C-01 — Gate vocabulary

`needs_input` is RESERVED for items genuinely needing the applicant's own
words/answers. Agent-doable posting verification is `pending_verification`;
fit rejections are `low_fit`. Never default an unknown parked state to
`needs_input` — an inflated input tray teaches the operator to ignore it.
(`engines/genuine_pat.py`)

## C-11 — Single logging path

Workers never call `log_event.py` directly and never write to the private
execution layer's state. Every per-attempt outcome is reported inside the
result envelope; the ingest step routes each attempt into `record_outcome.py`
exactly once and fail-closes any `submitted` claim without quoted
confirmation evidence. Direct logging causes double-counted telemetry.
(`worker-charter/ingest_envelope.py`, `engines/log_event.py`)

## C-14 — Canonical counting

Counts come from the exact count command on the canonical file, compared
against the previous run's number. A +1 is real ONLY if the number
increased. Never infer a new submission from a browser task, an email, or a
ledger row's timestamp. Figures carry their count date; nothing is
estimated, projected, or extrapolated. (`geo-pipeline/recount.py`)

## C-15 — Commitment-gate clearing

An `attest` gate — or any binding office/relocation/travel commitment
gate — clears ONLY via a `gate_cleared` event carrying a direct quote of
the applicant's agreement and its date, or via a documented false-positive
re-adjudication with live-page evidence. A queue note claiming "override"
is not a clearing. (`engines/prescreen.py`)

## C-17 — Queue-write validation

Every new queue entry is validated before writing — zero errors required.
Required on every entry: `role_id`, `company`, `title`, `action_band`,
`status` (all non-empty). APPLY band additionally requires `fit_score >= 75`
AND a posting URL. READY additionally requires `action_band` APPLY. Legacy
violations are grandfathered, not re-written. (`engines/queue_intake.py`)

## C-18 — Browser-launch event

Every spawned browser task emits a `browser_launched` event as its first
action, carrying `role_id`, the browser task/session id, and the start
timestamp. Launch events travel on the lifecycle telemetry channel, not the
worker-envelope path (which would route them into outcome recording — the
wrong tool). The apply loop's launch watcher emits the same event loop-side
on IN-FLIGHT detection, deduped per `role_id`, as a fail-safe backstop.
(`engines/apply_loop.py`, `engines/log_event.py`)

## C-19 — Park verification and the selection tripwire

A park is complete only when its queue write verifies: after saving, the
park routine re-reads the queue files and asserts the lead left the standard
queue and landed in the input queue with `PARKED-NEEDS-INPUT`, returning
`ok: false` on mismatch — callers must branch on that value. Telemetry park
events never substitute for a verified queue write; the queue is the system
of record. Selection additionally tripwire-skips any READY lead whose
`needs_input`-family `gate_blocked` is newer than its queue `status_updated`.
(`engines/prescreen.py`, `engines/apply_loop.py`)

## C-20 — Per-arm work trees

Discovery and sweep arms work inside their own per-arm work tree. Raw
scrapes and intermediate files stay in the tree's `raw/` directory and
never enter the discovery staging area directly. Only finished, validated
candidate files are published to staging through the single sanctioned
path. This makes the settle window a structural guarantee instead of a
timing heuristic: a half-written sweep file can no longer be ingested
early. (`engines/staging_ingest.py`)
