# Promotion gap validation — 2026-09-30

Baseline: current main e753dda420fa22b64fa00412ac56a8cc6a1375e3, after merged PR #45. Local source tree was matched to upstream tree 0120220183197742b53b112040b6f9f29218152a before changes. No production workspace, applicant record or live provider was used.

## Reproduced and repaired

Three new regression tests initially failed on this baseline:

1. A first-name answer saved through initialize/confirm_answer did not reach verify_retry.screen_promotion_form. The adapter still reported First Name as missing.
2. A required dropdown with no choices returned CLEAN.
3. Stored benign posting text labelled for another URL returned CLEAN.

Default pre-promotion screening now reads the current workspace bank and validates value-bound receipts for the exact role/employer. Stored posting text must carry an exact posting_text_url binding in promotion and packet screening. Already mismatched form source_url values are preserved and rejected rather than overwritten. Required choice controls with absent or blank options remain incomplete.

The journey suite covers supported correction, tampered values, missing receipts, expired assertions, wrong role/employer scope, malformed bank recovery, independent no-AI holds, positive/negative source binding, complete/empty dropdown options and mismatched form sources. These tests pass as ordinary regressions, with no XFAIL. Correction alone never changes READY state or authorizes submission.

## Verification

Python 3.12, pinned validation environment, synthetic/mocked provider inputs:

- Broad focused prescreen/readiness/verification/recovery/release/MCP/security run: 365 passed, 24 subtests passed, 4 skipped. Eight recovery fork deprecation warnings were reported.
- Final journey/property run after adding the wrong-posting-URL property: 24 passed.
- Standard-library discovery: 1,475 tests; OK, 19 skipped.
- git diff --check and python -S runtime imports passed; Hypothesis was not imported at runtime.

The runs overlap; do not add their counts. Python 3.11 and hosted CI results are not established by local Python 3.12 validation. No live ATS compatibility or private executor qualification is claimed.

## Residual gaps

Separate bounded local probes on this baseline/follow-up reproduced:

- An email in an untrusted required form label remains in a gate reason, and log_event.scrub retains the email in free-form evaluator notes. This establishes local text retention; no external disclosure was tested.
- A planted archive symlink redirects _archive_vetoed_packet retirement into a sibling temporary directory. This requires local setup/write access.
- The MCP triage_role prompt embeds caller-controlled multiline text before trusted tool steps. Construction was tested via the extracted function; no model or MCP transport was invoked.

Source inspection also confirms Live and MUSE main targets lack tabindex=-1; this run did not repeat browser/screen-reader tests. File-mode, retention, contrast and freshness-authentication findings in the uploaded reviews remain leads for further reproduction, not newly established production findings.

Keep privacy/logging, path containment and UI/MCP changes in separate bounded repairs. Do not replace complete screening evidence with redacted summaries before screening, retroactively stamp unrelated stored text as verified, or treat local hashes as independent authenticity evidence.
