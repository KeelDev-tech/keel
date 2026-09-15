---
name: Feature request
about: Propose a new capability for Keel
title: "[feature] "
labels: enhancement
---

## Problem
<!-- What real problem does this solve? Who hits it? -->

## Proposed solution
<!-- How should it work? Be specific about behavior, not just intent. -->

## Alternatives considered
<!-- Other approaches, and why they're worse. -->

## Fit with the honest-automation contract
Keel automates only what it can do truthfully: truthfulness gates and
fail-closed behavior are load-bearing, not optional. Explain how your
proposal keeps them intact:

- **Truthfulness gates:** Does this change add any path that could present
  unverified claims (about a candidate, a posting, an outcome) as fact?
  If so, where is the gate that stops it?
- **Fail-closed:** If the new feature can't verify something it needs, what
  does it do? (Acceptable answer: stop, park, or degrade loudly — never
  guess silently.)
- **Open-core boundary:** Does this belong in the public repo, or would it
  touch submission-behavior methods that live in the private execution
  layer per SPLIT.md? If the latter, say so explicitly.

## Additional context
<!-- Mockups, references, examples from other tools. No personal data. -->
