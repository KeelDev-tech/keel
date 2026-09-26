# Durable execution authority

This addition supplies an **opt-in local SQLite backend** for Keel's protected
execution interface. The default `Boundary()` still uses the unbound host and
cannot authorize dispatch. No transport, account connection, production migration,
external authentication, or live submission is enabled by installing this code.

## Implemented behavior

- `security.execution.durable.SQLiteAuthority` holds approvals, canonical attempt
  state, budgets, durable revocations and audit events in a **single SQLite
  transaction domain**. A caller must explicitly name its canonical store.
- `DurableHostAdapter` implements `HostAdapter.reserve`, `begin_dispatch`,
  `record_outcome` and `note_unknown`; bind it explicitly to `Boundary`.
- `security.actions.approval_gate.PersistentApprovalStore` is an opt-in compatible
  store for the existing execution-policy interceptor. Its policy approvals are
  distinct from exact-envelope approvals. Neither substitutes for the other.
- Operator grant, revocation, budget changes, creation and cancellation require a
  host authentication callback. An approver string alone cannot grant approval.
- Approval consumption and attempt reservation commit together. A given approval,
  nonce or attempt cannot authorize a second reservation, including after restart
  or in another process. A role with RESERVED, UNKNOWN or SUBMITTED state cannot
  acquire a new attempt through this backend.
- Before dispatch, mutable host facts are checked again and the dispatch budget
  and UNKNOWN state commit synchronously. A crash after this point cannot make
  an attempt automatically retryable. This provides one dispatch admission; it
  does **not** make external side effects exactly once.
- The host must supply current consent, actor/session authentication, exact role,
  lease, target, content, policy and authority revisions, and no-AI/hold/429 facts.
  A snapshot is bounded to 30 seconds (configurable downward). Time is read after
  database lock waits and callback execution; expiry and clock regression fail
  closed. An observed clock watermark is retained even after a denied operation.
- Identity, delegation, credential-handle and approval revocations persist in the
  same backend. No credential secret is stored here. This does not convert the
  old in-process identity or delegation registries into external authentication.
- A verified HTTP 429 creates a sticky absolute hold. Elapsed time does not reset
  it. This module deliberately provides no generic API to clear the hold.
- Outcomes require a trusted evidence validator. A not-submitted outcome requires
  proof of no side effect, not a missing receipt. The exact same terminal outcome
  is idempotent; conflicting evidence cannot overwrite a terminal attempt.
  Late bound facts may be recorded after approval expiry or revocation.

## Configuration and callbacks

All callbacks below are **trusted host code** outside worker/model control.
They are constructor arguments, not JSON flags or model-provided assertions.

`SQLiteAuthority.create(path, canonical_store_id=..., operator_validator=...,
operator_context=..., clock=...)` is the explicit provisioning operation. The
parent directory must already exist, belong to the current host user, and have
mode `0700`; the database is created with mode `0600`. Opening an existing
`SQLiteAuthority(...)` never creates missing tables or a new database. Schema
and store identity must match. Do not use `:memory:` or a separate database per
worker. Every participating worker opens the same selected canonical store.

`operator_validator(request, operator_context, now) -> principal | None`
checks a real host-authenticated session and the exact operation in `request`.
For granting an envelope it must verify the human approved its exact digest and
expiry, not merely that somebody logged in. Context is never persisted. A
nonempty principal is the callback's attestation; the module cannot authenticate
it independently. Do not expose this callback or provisioning to worker code.

`host_validator(db, envelope, phase, now) -> HostValidation` checks current facts
for phases `grant`, `reserve` and `dispatch`. It executes inside the canonical
writer transaction. It must read canonical records from that transaction or
participate in the existing host's serialized authority. Bind `authority_revision`
to the **complete authorization context** including role/source/form/lease and
revocation generation, not an arbitrary static version. Include every delegation
and credential handle used in `authority_subjects`; identity and approval subjects
are added automatically. Check any existing `OperationalSession`/`StopLedger`
restrictions here using a host-owned serialization boundary. Snapshot JSON and
workflow simulation receipts are never authentication or authority.

`outcome_validator(db, envelope, outcome, now) -> True` verifies actual evidence
against the exact attempt, account/destination, approved content and outcome.
Return exactly `True` only on verified evidence. Approval expiry is intentionally
not a barrier to recording an already-observed real effect. The envelope omits a
separate account field: the host must bind account and session identity in its
canonical authorization revision and evidence verification.

`PersistentApprovalStore(authority, current_validator=...)` accepts a callback
`current_validator(db, approval, phase, now) -> authority_revision`. It must check
real identity, consent and policy on `grant`, `find` and `consume`; changes to the
returned revision invalidate previous grants. Its `grant()` additionally requires
`operator_context=` and checks the authenticated principal equals `approver`.
The returned mutable Approval is an inspection copy, never a source of authority.

Callbacks must not invoke external transports, commit/rollback the provided
connection, or start nested authority transactions. The trusted fixed handler
must enforce current host permissions **at actual native tool invocation**,
including after a process pause or a slow commit. Database checks cannot revoke
a request already issued to another system. The adapter does not claim an atomic
transaction with a remote service, a different SQLite database, or a JSON ledger.

Budgets must be explicitly configured with
`authority.set_budget(scope, maximum, operator_context=...)` before reservation.
They count committed dispatch admissions; uncertain dispatches still consume
budget. Raising a budget requires operator authorization and does not reset its
counter. A changed budget scope requires fresh approved authorization context.

## Recovery

- RESERVED with no dispatch timestamp: `adapter.cancel_reserved(reservation,
  envelope, reason=..., operator_context=...)` authenticates the operator, verifies
  the exact canonical binding and records a predispatch cancellation. It does not
  reuse the old approval or nonce. A subsequent action needs a new attempt and
  human approval.
- UNKNOWN with no dispatch timestamp: an ambiguous commit cannot use the ordinary
  cancellation path. Optionally configure `no_dispatch_validator` on the adapter,
  then use `resolve_pending_not_dispatched(...)` with an authenticated operator,
  reason and evidence ID. This **separate independent verifier** must prove the
  trusted host never dispatched the exact action. Missing receipts, elapsed time,
  and model assertions are insufficient. The default verifier is unbound.
- UNKNOWN with a dispatch timestamp: reconcile through `record_outcome` using
  real verified evidence. Do not resend. The predispatch proof path refuses this
  state. A verified NOT_SUBMITTED result permits a separately approved new attempt.
- Terminal outcome: identical acknowledgement is harmless; a contradiction must
  be investigated outside this API and cannot silently overwrite the record.
- `note_unknown` never clears a terminal record or releases an unresolved role.
  No cleanup timer turns UNKNOWN into retryable work.

There is no registry-wide `clear()` or automatic unrevocation. Revoked subjects
remain revoked; use fresh host credentials/identities and new approved grants.
SQLite backups restored to older authority state can revive consumed approvals:
restore offline, invalidate old grants, account for every unresolved attempt,
rotate host authority revisions, and requalify before resuming any worker.

## Canonical migration boundary

Do **not** run this backend alongside a legacy writer for the same dispatches.
Select one of two integration routes:

1. Adopt this SQLite store as the sole canonical approval and attempt authority
   for the participating workers. Pause those workers first; inventory legacy
   approvals, attempts, 429 stops and unresolved intents; reconcile legacy UNKNOWN
   outcomes; invalidate existing grants; then bind all workers and handlers to the
   selected store and issue fresh host-authenticated approvals. Preserve old
   records as read-only history. Resume only after the host callbacks are verified.
2. If the existing host must retain its ledger/queue as canonical, implement
   `HostAdapter` **inside that host's existing transaction/lock domain** using
   this module's invariants and tests. Do not mirror approvals to a second live
   SQLite database and call the dual write atomic.

`keel_workflow.DeliveryStore` is expressly a simulation store. No migration or
adapter promotes its `SIMULATED_CONFIRMED` records into real approval or evidence.
`keel_operational.runtime` already owns native-session and canonical-stop logic;
its schemas are not overwritten or guessed by this addition. Integration with
private host state remains a host task, not a completed deployment claim.

## Filesystem boundary and verification

Storage checks reject symlink database files/ancestors, multiply-linked files,
nonprivate storage, wrong owner, replaced store identities and regressions of the
trusted clock. SQLite uses immediate writer transactions, full synchronization,
and foreign keys; creation synchronizes the parent directory. The DB is not
application-encrypted, and the event table is an operational audit trail, not a
cryptographically signed external ledger. An OS-isolated trusted host must own
all ancestor directories and protect the store, validators and transport code.
These checks do not defend against a hostile process with identical OS privilege,
root, a malicious callback, whole-store rollback or arbitrary in-process execution.

Run the independent offline suite:

```sh
python3 -m unittest security.tests.test_durable_host -v
```

The suite uses only synthetic validators and temporary stores. It exercises real
SQLite commit behavior, spawned-process contention, replay/restart, mutable
handles, expiry under waiting/slow validation, current-authority changes,
revocations, sticky 429, budgets, cancellation and terminal evidence immutability.
It sends no requests and proves no external service integration.
