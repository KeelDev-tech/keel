# Intel note — iFinder: agentic vulnerability discovery in 4G/5G cores
Filed: 2026-09-22. Source: this morning's Feed unit, independently verified.

## What happened
Researchers at Nanyang Technological University (Singapore) built **iFinder**, an
LLM-assisted multi-agent pipeline, and ran it against seven open-source 4G/5G
core-network implementations. It surfaced **84 previously unknown
vulnerabilities** — 83 confirmed by the affected maintainers, 81 assigned CVEs.
58 patched; 23 still open (maintainer responsiveness, not severity, drove the
split — OAI never responded, eUPF cited resource constraints).

## The pipeline (three agents, sequential)
1. **Hunter** — reads code for spots where incoming message data is used without
   validation.
2. **Spec cross-checker** — consults 3GPP standards docs to determine whether a
   missing check actually happens earlier in the procedure; this is the
   false-positive killer (the most common reason suspicious code is benign).
3. **Exploit builder** — writes a working PoC, runs it against a test network,
   reads the failure logs, rewrites until it lands or gives up.

Honest calibration from the researchers: on 22 previously known bugs it caught
15, and roughly **one quarter of its reports were wrong**. Useful, not magic —
the spec cross-check and the human/maintainer confirmation loop are load-bearing.

## Attack surface that matters
- Worst verified flaw: duplicate PDR IDs in PFCP Session Modification Requests
  let an attacker **hijack a subscriber's data session** and redirect traffic.
- Two routes in: misconfigured cloud-hosted cores exposing "internal"
  interfaces, and an ordinary phone with a valid SIM hiding control messages
  inside its own data tunnel (worked against 5 of 7 cores tested).
- "Internal" no longer means isolated — the core's oldest assumption is dead.

## Correction to the Feed unit
The feed summary said "four-agent pipeline." Verified sources consistently
describe **three agents in sequence**. Use three.

## Why we filed this — value extraction
1. **Keel launch positioning.** Third-party proof of the human-gated thesis:
   agents did the extraordinary hunting; humans (researchers + maintainers)
   decided what was real and what happened next. Draft caption filed at
   `keel/launch/instagram/caption-ifinder-DRAFT.txt`, awaiting Trent's approval.
2. **Signal Guardian methodology reference.** The hunter → spec cross-check →
   exploit-iteration loop is a clean pattern for any future automated
   assessment work: detection patterns derived from known flaws, spec-grounded
   false-positive elimination, log-driven exploit refinement.
3. **Defense-lane interview currency.** Agentic vuln discovery + human-gated
   disclosure is current, credible talking-point material for the active
   defense-contractor pursuit (Anduril, Palantir, Shield AI, etc.).

## Sources (verbatim)
- https://www.helpnetsecurity.com/2026/08/11/5g-core-network-vulnerabilities-research/
- https://thehackernews.com/2026/07/researchers-report-84-flaws-in-4g-and.html
- https://www.fierce-network.com/wireless/ai-agents-expose-5g-core-security-risks-telecom-operators
- https://www.computerweekly.com/news/366650777/NTU-uses-AI-agents-to-uncover-flaws-in-mobile-networks
