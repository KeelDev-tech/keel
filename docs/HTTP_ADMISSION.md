# Shared HTTP admission in 0.3.1

A stable per-host POSIX advisory lock covers admission, public DNS resolution, HTTPS exchange and any HTTP 429 write. Cooperating workers sharing KEEL_HOME therefore observe a recorded cooldown before issuing their next request. Redirects release one host lock before checking the next; different hosts remain independent.

Cooldown files use hashed host filenames, strict schema/host checks, private modes and atomic durable replacement. The lock wait, DNS wait and exchange consume one monotonic deadline. DNS has an additional five-second ceiling and eight-worker bound; system resolver work can outlive its caller. Filesystem and kernel scheduling are not hard realtime guarantees.

Retry-After accepts integer seconds and parses HTTP date alternatives, including implicit UTC for asctime dates and relative two-digit years. Invalid values use a 60-second minimum. Unrepresentably large delays persist as an indefinite hold (until: null), requiring operator reconciliation. Date semantics follow [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after). Full protocol conformance is not claimed by these fixtures.

## Boundaries

- Local workspace coordination is not a distributed fleet limit, cross-domain provider budget, or application-submission quota.
- Same-host serialization trades throughput for predictable admission. Waiting can exhaust the caller timeout before DNS or networking.
- Persistent expiry assumes a reasonably synchronized wall clock. Clock rollback can extend a hold; a forward jump can release it early after restart. Monotonic memory protects only the process still running.
- Corrupt, wrong-host and wrong-version state denies admission; read and lock errors raise.
- A failed 429 write closes the response, raises and retains the current process's hold. Another process cannot inherit a hold that storage could not persist. Disk-full/crash recovery is still an open cross-process boundary.
- Noncooperating code, hostile same-user edits, other workspaces and other machines are outside advisory locking.
- Expired records and stable lock files remain on disk; retention/rotation is not solved. Never unlink a live lock file. Review actual provider evidence before reconciling corrupt or indefinite holds.

Four simultaneous-worker fixtures recorded only one fake exchange when its result was 429. A separate exited-worker test verifies persistence. Tests make no network requests. No production cooldown, queue, scheduler or provider state was changed. This feature requires no new service, account, API key or third-party runtime dependency.
