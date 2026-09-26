# Privacy Architecture Spec (CI-11)

**Owner:** Keel Competitive Intel. **Status:** DRAFT — design spec, not evidence.
**Evidence discipline:** competitor facts come only from `intel/comparator-design-teardowns.md` (public pages, 2026-09-19) and are ADVERTISED, never measured. Every Keel-side perimeter item is **OPEN** unless Build closes it with tests + evidence ref. No privacy claim is published until the §3 conditions all hold.

## 1. Extension-permission comparison — table spec

Build one table with exactly these columns: **Product | Permission requested | Stated purpose | Source URL**. Fill only what the teardowns source; leave everything else UNMEASURED — never invent a permission set.

Known rows from the teardowns (ADVERTISED / third-party-attributed):
- **Simplify Copilot** — extension requests `debugger` and all-host access per the chrome-stats.com extension record cited in the teardown (third-party record, not Simplify's own page). Stated purpose on vendor pages: autofill on 100+ ATS portals with user review-and-submit. Source: https://simplify.jobs/copilot, chrome-stats.com record link in teardown sources.
- **JobCopilot** — Chrome extension exists (jobcopilot.com/chrome-extension/); advertised as fill-only with user clicking submit. Exact permission set: **UNMEASURED** (no store-listing permission data collected).
- **Jobright** — free 1-click Chrome extension, autofill only (jobright.ai). Exact permission set: **UNMEASURED**.
- **LoopCV** — no extension permission data collected; exact permission set: **UNMEASURED**.
- **Keel** — no user-installed extension by design; execution via ATS API paths and a server-side browser path (teardown G-10: OPEN — design position, not shipped, not measured).

The comparison's claim power comes from *their* declared permissions vs *our* declared architecture — but only after Keel's perimeter is actually closed and measured (§2).

## 2. Trust-perimeter closure inventory — template

One row per item. Columns: **Item | Closed / Open | Evidence ref**. Initial items (from teardown G-10's ties to the limitations register):

| Item | Closed / Open | Evidence ref |
|---|---|---|
| Transactional boundary around submission attempts (register F27) | OPEN | — |
| Audit chain from attempt to receipt (register F28) | OPEN | — |
| Tenant scoping for all execution paths (register F29) | OPEN | — |
| Recoverable setup / deletion-and-export accounting (register F30–F33) | OPEN | — |
| Credential scoping for API routes (register F38/F39) | OPEN | — |
| Server-side browser path runs without user-side privileged access | OPEN | — |
| No silent user-host permission requests anywhere in the product | OPEN | — |

An item flips to Closed only when Build ships it with tests and records the evidence ref (test file + run date). "Closed in design" does not count.

**What the standing measurement (§3, condition 2) looks like:** a monthly audit that (a) enumerates every network/credential/permission boundary in the shipped product, (b) re-runs the closure tests for each Closed item and records pass/fail, (c) logs any new boundary introduced since the last audit. The audit output is an internal artifact; the published claim cites only that a standing measurement exists — it never publishes internal topology.

## 3. Claim spec for LG-03 / LG-08 — when a privacy claim may be published

A privacy claim (e.g. "no browser extension required", "scoped credentials", "your data never grants us user-host access") may be published only when ALL three hold:

1. **Closure:** every row in the §2 inventory reads Closed with an evidence ref.
2. **Measurement:** the closed perimeter has a standing measurement (what is measured, how often, where the results are visible internally) — a claim without a meter is a one-time assertion, not an architecture.
3. **G1 sign-off:** privacy-counsel review recorded for the exact claim wording.

Until then, the ONLY permitted public-adjacent statements are: "Keel is designed to run without a user-installed browser extension" (design intent, not a shipped fact) and "privacy architecture under review" (status). Launch & Growth owns wording; Competitive Intel owns the evidence behind it; Build owns the closure.
