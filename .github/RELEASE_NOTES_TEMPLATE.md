# Release notes template

Copy this file to `docs/releases/vX.Y.Z.md` and fill it in. Every fragment
must be a verifiable fact — the release notes are where the project's
truthfulness contract meets the public. If a claim can't be checked against
the repo, the ledger, or a named source, it doesn't ship.

## vX.Y.Z — YYYY-MM-DD

### What this is

One paragraph: what this release is and who it's for.

### What's in it

- Point at the `CHANGELOG.md` entry for this version for the full list.
- Call out only the changes a newcomer would care about. Keep it short.

### What's verifiable

- Public repo: <link to the repo>, Apache-2.0 (see `LICENSE`).
- Open-core boundary: this release is the public half; see `SPLIT.md`
  for exactly what's public and what stays private (and why).
- Submission discipline: the public repo makes no submission claims.
  Any traction figure cited here must match the canonical source
  (private production ledger) and never a reconstructed time window
  or projection.

### How to get it

- Download the release zip from the GitHub Releases page, or
  `git clone` the repo, then follow the quickstart in the README.

### What changed since the last release

- Summarize in plain language; full detail lives in `CHANGELOG.md`.
