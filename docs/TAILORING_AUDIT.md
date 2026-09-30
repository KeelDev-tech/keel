# Seeded tailoring audit

Run `python3 -S tools/run_tailoring_audit.py --out /tmp/keel-tailoring-audit-new`
from the repository. Output must be a new directory. Root CI runs the audit and
retains `audit.json` separately for Python 3.11 and 3.12.

The audit constructs 30 distinct synthetic profiles across three role scopes:
90 packets with four source-selected, approved statements (employer, title,
skill, metric). Every pair first passes a clean control through the production
`keel_grounding.packet.verify_packet` verifier. The scenario matrix contains 15
cases each of clean, wrong employer, wrong title, inflated skill, unsupported
metric, and expired source evidence. Altered packets receive matching file and
manifest hashes so hashing alone cannot satisfy the approved wording contract.

Pass requires all 90 clean controls and 15 clean scenarios to verify, all 75
negative scenarios to block, and no authority upgrade. False verification uses
75 negative packets as its denominator; clean-control failures are counted
separately. Tests deliberately replace the verifier with always-accept and
always-block implementations and introduce an authority upgrade to prove the
audit rejects broken gates. The CLI denies and counts network and child-process
attempts, including caught denials. It is a Python audit guard, not an OS sandbox.

These are generated, synthetic evidence and approval labels. The audit does not
prove the applicant facts true, authenticate their reviewer, analyze arbitrary
resume prose or PDF semantics, run the resume renderer or apply loop, measure
READY status, or qualify the private Muse executor. This matrix measures exact
wording and evidence freshness at the existing grounded packet boundary. It
cannot detect false facts consistently inserted into both approved evidence
and output. It does not measure human correction time or market demand.
