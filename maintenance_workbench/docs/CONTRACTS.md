# Data contracts and operation semantics

## Explicit source configuration

```json
{
  "schema_version": 1,
  "workspace": "project-review",
  "files": [
    {"path": "adapter.py", "kind": "python"},
    {"path": "docs/adapter.md", "kind": "text"}
  ],
  "protected_paths": ["tests/", "policies/"],
  "dependencies": [["adapter.py", "docs/adapter.md"]]
}
```

Kinds are python/json/text. A dependency is [consumer, dependency]. Paths are
explicit relative POSIX names; no globs, absolute paths, traversal, backslashes or
shell expansion. Protected entries ending in '/' protect a prefix; others match
exactly. There are at most 1,000 source files, 512 KiB per file and 32 MiB total.
Sources must be UTF-8. This configuration is a trusted-host input, not an agent's
self-asserted permission grant.

## KeelContract/1 subset

Supported validation keywords: type, properties, required, additionalProperties,
items, enum, minLength, minimum, maximum. Types are object, array, string, integer,
number, boolean, null. Boolean values do not satisfy integer/number. Unknown keys
and schema dialect features are refused, including $ref, regex patterns, unions,
format and arbitrary remote references. additionalProperties defaults to false
in this dialect. Maximum schema depth is 20 and data depth 40.

This is intentionally not full JSON Schema/OpenAPI compatibility. Contract-diff
findings identify selected possible narrowing of a new request validator accepting
old inputs. Any changed contract is REVIEW_REQUIRED even when no specific narrowing
is found. No result proves response compatibility or arbitrary program semantics.

## Recipe envelope

A recipe has schema_version, name, steps and required_checks. Every step has id,
operation, needs and args. At most 32 steps; prerequisites must exist and form a
DAG. At least one required check is mandatory. An informational operation cannot
be substituted for a required check.

| Operation | Exact args | Meaning |
|---|---|---|
| syntax | {} | Parse all configured Python files in the bounded parser; no execution |
| impact | {paths: [...]} | Traverse static/declared reverse dependencies |
| search | {paths: [...], query: text} | Retrieve scoped literal evidence |
| validate_contract | {schema_path, value_path} | Validate a fixed JSON fixture against the supported schema subset |
| adapter_contract | {schema_path, mapping_path, input_path} | Apply a flat output-field → input-field map and validate its real generated JSON output |
| supply_health | {observation_path, evaluated_at} | Read-only buffer/refill classification |
| conformance | {events_path} | Read-only normalized event diagnostics |

PASS applies only to the named local check. DONE is informational. BLOCKED and
NOT_APPLICABLE cannot satisfy required checks. Every result states that target
runtime tests were not run and deployment is not authorized. Supply/conformance
findings do not disappear merely because independent fixture checks passed.

## Replacement proposal

```json
{
  "schema_version": 1,
  "workspace": "project-review",
  "base_snapshot": "<64-character snapshot SHA-256>",
  "reason": "Evidence-backed description of the proposed change",
  "changes": [
    {"path": "adapter.py", "expected_sha256": "<old file SHA-256>",
     "replacement": "complete proposed UTF-8 file content\n"}
  ]
}
```

Up to 30 distinct existing unprotected paths. No-op changes, wrong hashes, new
paths, extra authorization flags and mismatched scopes are refused. Candidate
source is a review artifact only. There is no deployment or patch-application
operation against the original checkout.

## Supply observation

Use schema_version, ready, actionable and observed_at. Counts are nonnegative
integers or null. evaluated_at is an explicit aware timestamp; observations older
than 300 seconds or from the future are UNVERIFIED by default. The ready floor is
5 in this read-only classifier. Zero actionable supply is visible even when the
buffer is full. Null is never zero; a locally present buffer does not establish
whole-system health. Counts/clock authenticity remain the host's responsibility.

## Normalized conformance input

An array of records with schema_version, event_id, attempt_id, sequence, state,
evidence_revision, observed_at. Up to 10,000 records. States supported in this
normalized adapter: PREPARED, AUTHORIZED, QUEUED, IN_FLIGHT, UNKNOWN, COMPLETED,
HELD. Unknown values fail instead of being silently mapped. Host integration must
map its native schema explicitly. Original telemetry is never rewritten.

Exact duplicate event IDs are ignored; conflicting duplicates or duplicate attempt
sequence numbers fail. Findings include repeated holds with unchanged evidence,
retry after unknown, work after completion and nonmonotonic observation times.
Authoritative-absence evidence can make a retry legitimate; the report asks the
canonical system to reconcile rather than declaring every flagged retry a defect.
