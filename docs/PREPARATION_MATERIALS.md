# Preparation packet expiry and retained materials

This describes the modern review packet in `engines/packet_contract.py` and
`pipeline_service.prepare_role`. It does not define retention for the legacy
buffer, Muse, source capture, backups or any host-owned store.

A packet has a two-hour validity window (`TTL_SECONDS = 7200`). At its expiry,
validation rejects it. Expiry is a check on use, not a deletion timer. A packet
remains preparation-only: `execution_authorized=false`, with status
`PREPARED_REVIEW_REQUIRED`. Validity does not mean READY, consent to submit, or
an executed application.

## What remains on disk

Preparation reads the selected source documents inside the workspace and copies
their reviewed bytes into `data/packet-materials/<sha256><extension>`. Packets
reference those copies; current source bytes and dependencies are also checked
at validation. Changing a source document invalidates its packet even when the
old copied bytes remain intact.

Copies with identical content and extension can be shared by multiple packets.
Expiring one packet does not remove its copy or original source, and another
still-current packet can continue to reference that same copy. Packet JSON also
contains applicant assertions and source paths. Hash-addressed filenames do
not anonymize or encrypt document contents.

These preparation and validation functions impose no attachment deletion
schedule. The two-hour validity window is **not a retention duration**. Protect
source documents, packet JSON, copies and backups as private workspace data;
do not publish them as diagnostic evidence. Any future retention policy needs
a separate decision that accounts for shared references and host-owned storage.
This document provides no deletion command or automatic cleanup policy.

## Failure boundaries

- `packet_contract.prepare` copies attachments sequentially. If a later copy
  fails, the function raises without returning a packet; earlier copies remain.
- If the guarded preparation/validation block in `prepare_role` fails after
  choosing its packet path, it removes that packet JSON when present and
  attempts to release the lease. Attachment copies and original documents
  remain. Earlier queue selection/material metadata is not rolled back.
- A lease-release failure is reported separately from preparation. It can leave
  the prepared packet JSON on disk despite the command reporting an error.
  That artifact still grants no execution authority; an error is not evidence
  that all files were removed or that preparation succeeded.

The synthetic tests in `tests/test_security_boundaries.py` cover expiry, shared
copies and partial-copy failure. `tests/test_preparation_identity_guards.py`
covers packet removal after a concurrent ledger change, retained material and
selection metadata, and the lease-release failure boundary. They characterize
existing behavior; they do not introduce a cleanup mechanism.
