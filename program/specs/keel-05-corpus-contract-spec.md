# Corpus Contract Spec (CI-10)

**Owner:** Keel Competitive Intel. **Status:** DRAFT — design spec, not evidence.
**Evidence discipline:** every coverage, freshness, and scale target below is **UNMEASURED** unless it cites a 30-day probe run. Competitor facts come only from `intel/comparator-design-teardowns.md` (public pages, 2026-09-19); Keel-side facts are OPEN unless labeled TESTED/CODE.
**Standing gate:** nothing leaves the workspace as a public claim before G1 privacy-counsel sign-off.

## 1. What a corpus declaration must contain

A corpus declaration is the user-readable contract for discovery breadth. One declaration per segment (occupation × geography). It MUST contain:

1. **Source list** — named sources only (e.g. "Greenhouse boards", not "job boards"). Each source row: source name, route type (API / server-side browser / other), operator of record.
2. **Coverage definition** — what "covered" means per source, stated as an inclusion rule the user could audit: which employers/boards are in, which are excluded, and why. Exclusions are declared, not hidden (account requirements, CAPTCHA-blocked portals, no-clean-API hosts).
3. **Refresh SLA per source** — stated in hours (e.g. "re-scanned at least every N hours"), **UNMEASURED** until the freshness probe has run ≥30 days for that source.
4. **Staleness threshold** — the age (in hours/days) at which a listing is considered stale and excluded from matching, per source. **UNMEASURED** until probes validate actual staleness rates.

What a declaration MUST NOT contain: unmeasured totals ("500K career pages", "8M jobs", "30+ boards" — the teardowns show all comparators publish these without methodology; Keel must not mirror the format). Named-and-shaped beats large-and-unverified.

## 2. The 30-day freshness-probe design

**What gets probed:** for each declared source, a fixed probe sample (proposed: ≥100 listings per source, sampled uniformly across the segment) is re-fetched on a fixed cadence (proposed: every 24h). Each probe event records: listing URL, first-seen timestamp, last-confirmed-live timestamp, verdict (LIVE / DEAD / AMBIGUOUS), and the evidence for the verdict.

**Pass/fail bar (per source, per 30-day window):**
- PASS: ≥95% of probe listings have a confirmed-live verdict within the declared refresh SLA, and the measured staleness rate is at or below the declared staleness threshold. **Targets UNMEASURED — these are acceptance numbers, not results.**
- FAIL: any source below the bar loses its refresh-SLA claim and is re-labeled "coverage UNMEASURED" until it passes a fresh 30-day window.
- Probe events are append-only; a FAILED window is never edited, only superseded by a new window.

**Probe cadence:** continuous once launched; windows evaluated on calendar months. Results feed CI-03 (provider-and-form support matrix) and the segment promise in §3.

## 3. Probe ownership and log schema

**Ownership:** Pipeline Performance runs the probes; Competitive Intel owns the declaration they feed. Neither may weaken the other's bar: Pipeline Performance cannot relax the pass/fail numbers to make a source pass, and Competitive Intel cannot publish a claim on a FAILED window.

**Probe log schema (append-only, one row per probe event):** `probe_id`, `source`, `listing_url`, `first_seen_ts`, `checked_ts`, `verdict` (LIVE / DEAD / AMBIGUOUS), `evidence_ref` (fetched artifact or fetcher log line), `segment`. The log is the evidence behind every published number; a claim whose log is missing is treated as UNMEASURED, no exceptions.

**What a PASS unlocks:** the source's refresh SLA and staleness threshold become publishable facts (still cited to the probe window ID). A FAIL demotes the source to "coverage UNMEASURED" and restarts its 30-day clock; two consecutive FAILs trigger a Competitive Intel review of whether the source stays in the segment's declared set at all.

## 4. Segment-specific source promise

The market plan requires a focused segment before this ships (pilot plan shelved — contract is built, not launched). When the segment is chosen, the promise reads: "For [segment], Keel monitors [named sources], refreshed at least every [N] hours per source, with stale listings excluded after [M]." Every bracketed value comes from a PASSED probe window, or the whole sentence stays unpublished.

## 5. Publication rule (hard)

No corpus, coverage, freshness, or source-count claim is published — on the site, in marketing, in sales material, or in user onboarding — until:
- the source's probe has run ≥30 days AND passed the §2 bar, AND
- G1 privacy-counsel sign-off is recorded.

A source with <30 days of probes is labeled "coverage UNMEASURED" everywhere, including internal dashboards. Failed windows are reported honestly ("freshness not yet established"), never dropped.
