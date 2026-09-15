## What changed
<!-- One paragraph. Link to the issue if there is one: `Fixes #123`. -->

## Why
<!-- The problem this fixes or the capability it adds. -->

## Tests run
<!-- e.g. `python3 -m unittest discover -s tests` — paste the result. -->
<!-- New tests added? Name them and what they cover. -->

## Checklist
- [ ] No personal data anywhere in the diff (no names, emails, phone
      numbers, addresses, social profile URLs, ZIPs, real employer names)
- [ ] No invented claims or numbers in any copy or docs — every figure
      verifiable (submission counts only from the ledger with explicit
      confirmation evidence; no projections, no reconstructed time
      windows like "in 48 hours")
- [ ] Tests green (`python3 -m unittest discover -s tests` passes)
- [ ] Docs updated if behavior changed (README / docs / CHANGELOG entry
      under `[Unreleased]`)
- [ ] Feature changes reviewed against the honest-automation contract
      (truthfulness gates intact, fail-closed on unverifiable input,
      respects the open-core boundary in SPLIT.md)
