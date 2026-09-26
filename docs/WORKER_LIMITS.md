# Bounded local worker execution

`security.execution.isolation.launch` now accepts an optional resource policy and
an explicit syscall profile. Everything runs locally using Python, Linux cgroup
v2, and the existing bubblewrap installation. No service or paid API is used.

The resource policy is added to the existing schema-1 isolation configuration:

```json
{
  "resource_limits": {
    "cgroup_root": "/sys/fs/cgroup/keel-supervisor/jobs",
    "memory_bytes": 268435456,
    "pids": 32,
    "cpu_quota_us": 50000
  },
  "seccomp_profile": "python-worker-v1"
}
```

This is a fragment, not a complete launch configuration. Existing reviewed
runtime/application inventories, separate worker/broker identities, and the
protected operator configuration remain required. The CPU period is fixed at
100,000 microseconds; a quota of 50,000 permits approximately half of one CPU
across all tasks. `pids` counts tasks, including threads and bubblewrap helpers.
Memory is limited for the whole group, swap is disabled for the job, and group
OOM termination is enabled. Existing wall-time, output, and tmpfs limits remain.

Resource limits implicitly require `python-worker-v1`, even if the profile field
is omitted or null. An unknown profile, invalid limit, unavailable controller,
readback mismatch, failed placement, or failed cleanup blocks the operation.
There is no unconstrained retry. Old configurations remain loadable and clearly
report `NOT_CONFIGURED`; they do not acquire or claim these new protections.

## Host contract and placement

A trusted host administrator must provision a dedicated cgroup v2 delegation
beneath `/sys/fs/cgroup`, with `cpu memory pids` already enabled for children. The
parent must contain no processes. The supervisor needs permission to create a
child, set its controls, and place its own child there. The supervisor itself
normally resides in a sibling leaf under the same delegation, so Linux's common
ancestor permission check allows movement. Keep that delegation and launch
configuration outside guest mounts and untrusted process access.

Keel never enables a controller, changes a parent limit, moves an existing PID,
changes host permissions, selects a writable cgroup automatically, or requests
privilege escalation. It rejects the mount root, aliases of that root,
noncanonical paths, symlinks, unrelated filesystem directories, and untrusted
owners or writable ancestors. All job writes use a pinned descriptor to one new
randomly named leaf.

Before a child is spawned, Keel sets and reads back every limit. The trusted
child-side pre-exec hook writes `0` to the leaf's `cgroup.procs`; Linux interprets
that as the calling process. Only then can it execute bubblewrap and guest code.
Descendants inherit membership. This avoids launching a guest and then racing
to attach its PID. The writable placement descriptor is explicitly closed before
exec, and the cgroup tree is absent from guest mounts.

This pre-exec hook requires a **dedicated single-threaded supervisor process**.
The launcher checks both Python and native task counts and blocks otherwise.
Do not call it from a threaded web server. This code belongs in the trusted host,
not in an agent-controlled interpreter. A hostile host process with the same UID
is outside this boundary; separate service identities and operator-owned source
remain part of deployment qualification.

At completion, cancellation, or deadline, the host uses the new leaf's
`cgroup.kill`, waits for `populated 0`, and removes the leaf. Process-group killing
also remains available if cgroup killing fails. Cleanup failure is a blocked
result requiring host inspection; there is no recursive kill of a shared parent.
A supervisor crash can leave a job leaf behind. Existing bubblewrap parent-death
behavior helps terminate the worker, but a host service should inspect orphaned
leaves after restart. Keel does not automatically delete or kill unknown leaves.

## Syscall profile

`security/execution/seccomp_profile.py` contains the versioned, reviewable allowlist
and fixed numeric Linux x86-64 syscall map. It generates fewer than 512 classic
BPF instructions and seals the in-memory filter against modification before
passing its descriptor to bubblewrap's `--seccomp` option. There is no arbitrary
filter upload or runtime rule learning.

The profile verifies the architecture and rejects x32 calls. It allows ordinary
Python file, memory, process, timing, and local IPC operations. Socket creation is
restricted to AF_UNIX; clone namespace flags are denied. `clone3` returns ENOSYS
so libc can fall back to the filtered clone call. Mounts, namespace changes,
ptrace, BPF loading, perf events, keyrings, process-memory access, userfaultfd,
io_uring, and unlisted syscalls are denied. New syscalls are denied by default.
AArch64 and other architectures are currently **blocked**, not silently mapped to
x86 numbers. This profile may block libraries requiring extra syscalls; qualify
such workloads before adding a reviewed profile version.

Seccomp reduces exposed syscall surface; it does not provide filesystem policy,
application authorization, or an independent kernel security boundary. It is
combined with the existing namespaces, restricted mounts, capability dropping,
different broker UID, approval gate, and the new quotas.

## Read-only diagnosis and validation status

```bash
python3 -B -m security.execution.resource_limits \
  --cgroup-root /sys/fs/cgroup/keel-supervisor/jobs
```

The preflight never creates a leaf or runs a guest. `AVAILABLE_UNVERIFIED` means
read-only checks passed. A compiled filter is `COMPILED_UNVERIFIED`.
`enforced: false` remains explicit until an actual launch. Successful resource
launches include kernel counter snapshots; failed bubblewrap startup cannot be
reported as a confirmed seccomp installation.

Tests cover invalid/boolean/out-of-range limits, missing delegation, normal-file
rejection, single-thread checks, mutation confined to fresh leaves, limit
readback mismatch, pre-exec ordering, placement failure without guest execution,
cleanup calls, architecture/x32 guards, namespace/socket filtering, deny-by-default
syscalls, sealed descriptors, and a real disposable-child seccomp installation.
Pure filesystem fixtures and wiring checks **do not qualify cgroup enforcement**.

The current audit host exposes a read-only cgroup v2 mount and no trusted writable
delegation. CPU/memory/PID enforcement is therefore not qualified on this host.
The optional live quota test only runs when the operator explicitly supplies
`KEEL_TEST_CGROUP_ROOT`; it never discovers or enables a writable delegation.
Deployment also needs actual bubblewrap/runtime conformance on its own host.

Authoritative references used for the implementation:

- [Linux cgroup v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [Linux seccomp filter documentation](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
- [Linux no-new-privileges documentation](https://www.kernel.org/doc/html/latest/userspace-api/no_new_privs.html)
- [Bubblewrap manual](https://manpages.debian.org/trixie/bubblewrap/bwrap.1.en.html)
