# Security and assurance boundaries

## Actual enforcement in this extension

Strict JSON rejects duplicate keys, non-finite numbers, unsupported versions,
unknown protocol fields, oversized inputs, deep nesting, and Boolean-as-integer
substitution. Source selection is an explicit allowlist. POSIX reads traverse
components using directory descriptors and O_NOFOLLOW; symlinks, hardlinks, pipes,
nonregular files, oversized files and observed concurrent file changes are refused.

All outputs are new files/directories, with private file modes. Staging verifies
workspace, base snapshot and old file hashes and cannot alter configured protected
paths or silently add a new path. Config/scope changes require a separate review;
this release does not compare snapshots with different configurations.

Recipes have a finite operation set, typed arguments, bounded prerequisite graphs,
mandatory checks, visible failure states, and no shell/eval/target-import operation.
Only the shipped static parser is launched as a child. It uses Python isolated/no-site
mode, stripped environment, CPU/address-space/file-size limits and a timeout. It
parses source into syntax metadata; it does NOT execute source. Missing limits or
parser failure produce UNAVAILABLE, never PASS. Parsing uses a 30-second aggregate
budget by default; a recipe checks a 90-second budget between steps. Kernel waits
and in-progress bounded steps are not hard real-time guarantees.

## What these controls do not establish

This is not a hostile-code OS sandbox, authenticated multi-tenant service, digital
signature system, immutable ledger, production credential manager or live authority
resolver. A principal that can edit this process's source, files, configuration or
memory can change its behavior. Another agent prompt is not an independent security
identity. The host must enforce OS separation and authenticated permissions.

Hashes detect byte mismatch against a given manifest. They do not prove publisher
identity, truthful provenance, user consent, or provider acceptance. A file manifest
and objects can be rewritten together by their owner. The same applies to local
reports. Never use an extension report as an authorization token or provider receipt.

Two-pass source capture is not an atomic repository snapshot. It detects many
concurrent changes but not every interleaving or change-and-revert race. Use a
quiescent checkout or established exact revision for consequential review. It does
not prove that the deployed application matches that checkout.

New-tree creation is not a multi-file transaction or a validated power-loss recovery
mechanism. A crash can leave a partial output. Verify a snapshot or restored text
bundle before using it; failed restoration must not trigger code execution.
Concurrent malicious edits by the same OS principal are outside this local design.

## Evidence and data handling

A source copy and any excerpt derived from it remain UNTRUSTED_SOURCE_COPY. Embedded
instructions do not alter the finite recipe operation table. This is mechanical
separation, not a claim of general prompt-injection immunity.

The FTS index is built in memory AFTER workspace/path filtering. No embedding model,
remote search API or persistent answer database is used. The optional in-process
cache stores informational search results only; it cannot satisfy authority/runtime
checks. A snapshot, report or diff may contain sensitive source material. The explicit
allowlist is not a semantic secret scanner. Review it; use private storage; do not
publish artifacts without separate disclosure review. No source is automatically
uploaded or transmitted by this package.

Normalized event exports are caller-supplied observations. Conformance flags are
investigation leads, not adjudications. A record marked COMPLETED is not verified
acceptance. Missing history and sequence gaps remain visible. No native live event
schema is guessed or automatically migrated.

## Standing project exclusions preserved

No paid service, model download, paid fallback, replacement gateway, kill switch,
second receipt database, scheduler replacement, live queue write, browser-lane
operation, outreach, reference contact, submission, publication, consent inference
or answer-bank update exists in this package. Disputed/quarantined consent remains
quarantined. An untrusted draft cannot become a user decision here.

The supply classifier reports the ready=5/actionable=0 condition but does not patch
or operate the live pool guardian. The retrieved public review archive lacks the
actual private pool_guardian.py implementation. Preserve the existing cadence,
throttle, floor, spawn command and browser-lane ownership when a live maintainer
later integrates a reviewed helper through the sanctioned code path.
