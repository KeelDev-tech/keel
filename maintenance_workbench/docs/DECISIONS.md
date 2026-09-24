# Architectural decisions for this review extension

## ADR-001: complement rather than replace canonical authority

Use a separate namespace and derived review artifacts. Never introduce a second
answer bank, receipt ledger, scheduler or approval mechanism. Rationale: the user's
standing boundaries and the existing public/private execution split.

## ADR-002: do not run arbitrary target code without an actual isolated adapter

Use deterministic contract/mapping fixtures plus a bounded static-parser process.
A directory copy, Python exception guard, role name or recipe JSON is not a sandbox.
Runtime behavior and real-provider validation remain explicitly unrun.

## ADR-003: explicit, bounded source scope

Read a trusted-host allowlist with no network fetching or ambient repository scan.
Index only requested context paths and preserve hashes/line references. Local
workspace names are selectors, not authenticated identities.

## ADR-004: text transfer, no overwrite

Deliver readable UTF-8 source with declared lengths and hashes, plus a standalone
extractor. Validate the entire transfer before writing into a fresh private tree.
Provide verify-tree so interrupted or modified restores can be detected.

## ADR-005: narrow the first change to one demonstrable maintenance workflow

Deliver the contract-change → failure → impact → staged mapping repair → fixed
fixture loop. Do not present deferred libraries/services as working integrations.
Future changes must preserve known constraints and show measured improvement.
