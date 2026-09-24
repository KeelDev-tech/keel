# Distribution Re-entry Spec (LG-09)

**Owner:** Keel Launch & Growth. **Status:** DRAFT — strategic deferral with named re-entry bars.
**Standing position:** distribution stays **DEFERRED** until the evidence bars below pass. Competitor facts come only from `intel/comparator-design-teardowns.md` (public pages, 2026-09-19) and are ADVERTISED, never measured.

## The bars that un-defer (made concrete)

An option is re-evaluated only when ALL three hold — measured, not asserted:

1. **Integrity:** the adversarial receipt gates are green (F17 provider-correlated confirmation contract, F18 final evidence writer, F22 one durable attempt) AND the receipt-writer adversarial re-verification (B-01/B-02) has passed on test gates. No distribution channel may multiply submissions while a single submission's truth is unresolved.
2. **READY reliability:** T1 targets are measured on a mature window — <1% avoidable idle time and <1% preventable rejection after READY — from live telemetry, not projections. A distribution partner that inherits our supply gets our supply's reliability record with it.
3. **Demand evidence:** named, written interest from the option's specific audience (e.g. ≥10 coaches requesting white-label by name, ≥25 developers on an API waitlist, an employer naming a paid role they would post through a Keel marketplace) — counted from real inbound, never projected. **All demand targets UNMEASURED until observed.**

**How demand evidence is recorded (template):** one log row per inbound — `date`, `audience`, `name/org`, `option requested`, `verbatim request`, `source` (where they asked), `follow-up status`. The log is append-only and owned by Launch & Growth; a bar is "met" only when the log contains the threshold count of verifiable rows. Vague interest ("maybe later") does not count.

## Option 1 — Coach dashboard

**What it is:** Per the teardowns, LoopCV advertises white-label for coaches plus multi-client dashboards on loopcv.pro/career-advisors/ — a coach manages client job searches from one pane, presumably under their own brand. The Keel analog: a coach-facing dashboard over the same integrity-gated engine, where the coach sees receipts, blocker inboxes, and outcome cohorts for their clients.

**Re-entry bar:** §1–§3 hold, plus the blocker-inbox surface (B-24) is a finished product — a coach dashboard built on operator tooling is a support contract, not a product.

## Option 2 — API / webhooks / MCP

**What it is:** Per the teardowns, LoopCV advertises a public REST API, webhooks, and an MCP server exposing `search_jobs` / `apply_to_job` to AI agents (loopcv.pro/developers/) — they monetize the engine, not just the seat. The Keel analog: exposing the integrity-gated engine (attempt identity, receipts, constraint enforcement) as the differentiator — "the application engine you can prove."

**Re-entry bar:** §1–§3 hold, plus the receipt contract (F17/F18) and attempt identity (F22) are shipped and test-gated — an API that can be audited is the only API worth exposing.

## Option 3 — White-label

**What it is:** Per the teardowns, LoopCV's white-label product lets partners (coaches, agencies) offer the engine under their brand. The Keel analog: a tenant-scoped, audited white-label where every partner action inherits the full provenance/audit chain — no brand may weaken the evidence guarantees.

**Re-entry bar:** §1–§3 hold, plus tenant scoping (F27–F33 trust-perimeter items) closed with tests — white-label multiplies principals, and every principal's scope must be provable.

## Option 4 — Two-sided marketplace

**What it is:** Per the teardowns, Jobright runs an employer product ("The AI recruiter for tech companies", advertised $499/month per active role at jobright.ai/employers) sourcing from the same 2M-professional network the jobseeker product builds — the candidate's profile is simultaneously application material and employer sourcing inventory, with an undisclosed consent/scope tension the teardowns flag. The Keel analog would be the hardest variant: employer-side access to a verified-eligible candidate pool.

**Re-entry bar:** §1–§3 hold, plus a consent-and-scope model that resolves the dual-use tension *before* any employer sees a profile — written consent flows, per-employer scope limits, and G1 review of the entire data-use contract. This option carries the highest privacy risk; it is evaluated last, deliberately.

## Standing rule (non-negotiable)

Nothing under any option is offered, priced, sold, or publicly promised before its re-entry bars pass **and** G1 privacy-counsel sign-off is recorded. Waitlists and inbound interest may be *counted* as demand evidence; they may not be *promised* a product. The deferral is the strategy — re-entry is earned, not scheduled.
