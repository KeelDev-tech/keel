# Keel Security Authority — Phase 1

A **separate deterministic authority** over the Keel agent ecosystem, not a
peer agent. It answers four questions before anything executes:

1. **Who is acting?** (identity)
2. **What are they allowed to do?** (capabilities)
3. **What data are they touching?** (data/secret classification)
4. **Is the resulting action trustworthy enough to execute?** (policy, risk)

## The non-negotiable rule

> **The security LLM must not be the security boundary.**

Capabilities, permissions, secret isolation, approval requirements,
signature/evidence verification, kill switches, and Blocker Resolution
policies are **deterministic code**. LLMs may analyze or summarize but can
never override enforcement. There is no LLM in the decision path.

## Enforcement flow

```
AGENT → ACTION REQUEST → IDENTITY/CAPABILITY → INJECTION CHECK
      → DATA/SECRET CHECK → POLICY ENGINE → RISK
      → ALLOW / DENY / QUARANTINE / APPROVAL
      → EXECUTION → RESULT VALIDATION → LEDGER
```

Every decision is recorded in the tamper-evident security ledger before it
takes effect. If the durable ledger cannot be written, execution is refused
(fail closed).

## Package layout

| Package | Responsibility |
|---|---|
| `identity/` | Agent identities, capability manifests (default deny), delegation (subset-of-grantor, expiry, revocation) |
| `injection/` | Scanner for prompt-injection / jailbreak patterns in untrusted content; trust-boundary model (`Origin`) |
| `actions/` | Action classification (8 classes), approval gate (single-use, expiring, resource-digest-bound), enforcement interceptor |
| `data/` | Data classification (PUBLIC→CREDENTIAL), redaction, DLP inspection |
| `secrets/` | Secret handles (references, never values) + committed-secret scanning |
| `memory/` | Memory provenance, hash verification, quarantine store |
| `policy/` | Base rules, deterministic risk scoring, Blocker Resolution layer |
| `ledger/` | SHA-256 hash-chained, file-backed security event ledger |
| `response/` | Revocation, agent-tree freeze, safe mode, containment |

## Action classes and approval semantics

| Class | Examples | Decision path |
|---|---|---|
| READ_ONLY | read_leads, read_telemetry | ALLOW if capability held |
| REVERSIBLE_WRITE | write_packet, manage_queue | ALLOW if capability held |
| EXTERNAL_COMMUNICATION | send_email, post | Blocker gate (G1) → approval |
| CREDENTIAL | read_secrets, use_password | Approval; high risk base |
| FINANCIAL | charge, process_payment | Approval |
| SUBMISSION | browser_submit, api_submit | Evidence + approval |
| SYSTEM_CHANGE | modify_policy, deploy, keel_0_8_promotion | Blocker gate → approval |
| DESTRUCTIVE | delete_queue, wipe | Deny unless approval; risk ceiling applies |

**Risk ceiling:** no evaluation path may approve an action scoring at or
above the threshold (85). The ceiling runs before any class-level approval
semantics — a destructive action with credential sensitivity denies even
though its class would otherwise allow human approval. The ceiling never
*permits* anything on its own; it only denies.

## Blocker Resolution Directive (§9)

`policy/rules/blocker_resolution.json` encodes the Blocker Resolution
Directive as data, evaluated deterministically by `policy/engine.py`. This
layer runs after safe mode and before capabilities/base policy, and it can
only **deny or abstain — never grant**. If the directive file is missing
for a covered action, the engine fails closed.

The six policies:

1. `api_direct_write` → unconditional DENY.
2. External communication / egress → DENY unless the exact-G1
   authorization condition holds.
3. `synthetic_to_live` → unconditional DENY (synthetic data never
   promotes to live).
4. `human_decision_impersonation` → unconditional DENY.
5. `publication` → DENY unless release authorization holds (G1 egress
   rule still applies independently).
6. `keel_0_8_promotion` → DENY unless **all six** predicates hold:
   `authentic_sources == 6`, `cross_record_validation == PASS`,
   `genuine_operator_decision == PRESENT`, `assurance == PASS`,
   `trust == PASS`, `consent == PASS`.

Conditions are a small deterministic mini-language: equality and numeric
comparison against fact values, with type-strict booleans (`1` ≠ `True`)
and exact string/hash equality for authorization evidence. Per Trent's
requirement, G1 and release authorization are expressed as **exact-hash
equality conditions**, not boolean flags — the production rules file must
carry the hashes (see note below). Effects: `DENY`, `DENY_ALWAYS`,
`CONDITIONAL_DENY`. Malformed effects, unknown condition operators, or
duplicate policy IDs are rejected at load (fail closed).

> **Production rules note (2026-09-18):** the sibling-delivered production
> rules file represents G1 / release authorization as boolean facts, while
> the requirement is exact-hash matching. The engine supports exact hash
> equality and synthetic regression cases cover it, but the sibling-owned
> file was not overwritten. Reconcile (or document the deviation) before
> relying on these policies in production.

## The submission choke point (wired, real)

`engines/record_outcome.py` — the real Keel writer — routes every
`outcome == "submitted"` through `security.actions.interceptor.
request_submission_authorization()` **before** any telemetry mutation:

- The ATS note/technique/company are treated as ATS-derived (UNTRUSTED)
  and injection-scanned.
- The caller must hold the `record_submission` capability.
- The note must carry confirmation evidence (non-empty, not a
  placeholder literal); empty/placeholder notes refuse.
- `worker-charter/ingest_envelope.py` already validates the confirmation
  quote via `evidence_gate` and passes it as the structured evidence note.
- If the security authority cannot load, recording refuses (fail closed).
- `PolicyDenied` remains a `ValueError` subclass, so existing callers'
  `except ValueError` fail-closed paths are unchanged.
- Non-submission outcomes (`blocked`, etc.) bypass the gate and keep
  their existing behavior exactly.

## Identity, capabilities, delegation

- Identities are registered (`register_system_identity`); unregistered
  actors cannot act.
- Capabilities are explicit grants; **anything not granted is denied**.
  There is no wildcard escalation: delegation can only grant a subset of
  the grantor's own capabilities, expires, and is revocable.
- Revocation (`response/revoke.py`) supports identity revocation, token
  revocation, delegation revocation, and freezing a whole agent tree.
- Safe mode (`response/safe_mode.py`) is a file-flag kill switch: when
  engaged, the policy engine restricts to `safe_mode_allowed_classes`.
  Engaging requires a reason; disengaging requires a reason AND a named
  approver.

## Data and secrets

- `data/classify.py`: PUBLIC / INTERNAL / PII / FINANCIAL / CREDENTIAL.
- `data/redact.py` + `data/dlp.py`: redaction and DLP inspection of
  destinations (UPPERCASE canonical names).
- `secrets/handles.py`: code holds opaque handles, never secret values.
  `secrets/committed.py` scans for committed secrets.
- Interceptor DLP violations deny the action deterministically (not
  merely reported).

## Memory provenance

`memory/` validates provenance, verifies content hashes, and quarantines
suspect entries (`QuarantineStore`). Quarantined memory is invisible to
normal reads until cleared.

## Ledger and evidence

- `ledger/events.py`: file-backed JSONL security ledger, default
  `~/workspace/keel/hidden_files/security/security_events.jsonl`
  (`KEEL_SECURITY_LEDGER` overrides for tests).
- `ledger/hashchain.py`: SHA-256 chain (`seq`, `prev_hash`, `hash`) —
  tampering is detectable via `verify()`.
- Every interceptor decision (ALLOW/DENY/QUARANTINE/APPROVAL) is ledgered
  with agent, action, reasons, risk score/factors, and a UTC timestamp.

## Boundaries (what this does NOT govern)

- **Host-runtime tools and platform approval cards are out of scope.**
  This authority governs Keel's engine layer only; it does not bind the
  host runtime or platform-level approval cards, and it provides no path
  to bypass them.
- Privacy-counsel gate G1 remains absolute before publication/offering/
  pricing/sale (Trent's action: engaging counsel).
- No network calls in enforcement, no scheduler writes, no outreach, no
  spending, no real credentials in code or tests.

## Phase 2 (explicitly deferred)

- Behavioral detection
- Automated red teaming
- SBOM / supply-chain monitoring
- Adaptive risk scoring

## Running the tests

```bash
cd ~/workspace/keel/security/tests && python3 run_all.py
```

217 regression tests, all green as of 2026-09-18. The suite caught and
fixed two real implementation defects during the build: a risk-ceiling
ordering bug (class-level early returns could skip the hard ceiling) and
a scanner crash (`str.isprint` does not exist — `isprintable` does).
