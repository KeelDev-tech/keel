"""Small versioned x86-64 syscall allowlist for isolated Python workers.

The profile is additive to bubblewrap's namespace/mount/UID policy. No arbitrary
filter input, architecture fallback, or service dependency is accepted.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import platform
import struct

from .resource_limits import ResourceBlocked

PROFILE = "python-worker-v1"
AUDIT_ARCH_X86_64 = 0xC000003E
ALLOW = 0x7FFF0000
KILL_PROCESS = 0x80000000
DENY = 0x00050000 | errno.EPERM
# Linux x86-64 native ABI only. Names are retained for review against UAPI
# arch/x86/entry/syscalls/syscall_64.tbl. New syscalls remain denied.
SYSCALLS = {
    "read": 0, "write": 1, "open": 2, "close": 3, "stat": 4, "fstat": 5, "lstat": 6,
    "poll": 7, "lseek": 8, "mmap": 9, "mprotect": 10, "munmap": 11, "brk": 12,
    "rt_sigaction": 13, "rt_sigprocmask": 14, "rt_sigreturn": 15,
    "pread64": 17, "pwrite64": 18, "readv": 19, "writev": 20, "access": 21,
    "pipe": 22, "select": 23, "sched_yield": 24, "mremap": 25, "msync": 26,
    "madvise": 28, "dup": 32, "dup2": 33, "pause": 34, "nanosleep": 35,
    "getitimer": 36, "alarm": 37, "setitimer": 38, "getpid": 39, "sendfile": 40,
    "connect": 42, "accept": 43, "sendto": 44, "recvfrom": 45, "sendmsg": 46,
    "recvmsg": 47, "shutdown": 48, "bind": 49, "listen": 50, "getsockname": 51,
    "getpeername": 52, "setsockopt": 54, "getsockopt": 55,
    "fork": 57, "vfork": 58, "execve": 59, "exit": 60, "wait4": 61, "kill": 62,
    "uname": 63, "fcntl": 72, "flock": 73, "fsync": 74, "fdatasync": 75,
    "truncate": 76, "ftruncate": 77, "getdents": 78, "getcwd": 79, "chdir": 80,
    "fchdir": 81, "rename": 82, "mkdir": 83, "rmdir": 84, "creat": 85,
    "link": 86, "unlink": 87, "symlink": 88, "readlink": 89, "chmod": 90,
    "fchmod": 91, "umask": 95, "gettimeofday": 96, "getrlimit": 97,
    "getrusage": 98, "sysinfo": 99, "times": 100, "getuid": 102, "getgid": 104,
    "geteuid": 107, "getegid": 108, "setpgid": 109, "getppid": 110,
    "getpgrp": 111, "setsid": 112, "getgroups": 115, "getresuid": 118,
    "getresgid": 120, "getpgid": 121, "getsid": 124, "capget": 125,
    "rt_sigpending": 127, "rt_sigtimedwait": 128, "rt_sigqueueinfo": 129,
    "rt_sigsuspend": 130, "sigaltstack": 131, "utime": 132, "statfs": 137,
    "fstatfs": 138, "getpriority": 140, "sched_getaffinity": 204,
    "arch_prctl": 158, "gettid": 186, "futex": 202, "getdents64": 217,
    "set_tid_address": 218, "restart_syscall": 219, "clock_gettime": 228,
    "clock_getres": 229, "clock_nanosleep": 230, "exit_group": 231,
    "epoll_wait": 232, "epoll_ctl": 233, "tgkill": 234,
    "openat": 257, "mkdirat": 258, "newfstatat": 262, "unlinkat": 263,
    "renameat": 264, "linkat": 265, "symlinkat": 266, "readlinkat": 267,
    "fchmodat": 268, "faccessat": 269, "pselect6": 270, "ppoll": 271,
    "set_robust_list": 273, "get_robust_list": 274, "utimensat": 280,
    "epoll_pwait": 281, "accept4": 288, "eventfd2": 290, "epoll_create1": 291,
    "dup3": 292, "pipe2": 293, "preadv": 295, "pwritev": 296,
    "prlimit64": 302, "getrandom": 318, "memfd_create": 319,
    "statx": 332, "rseq": 334, "close_range": 436, "faccessat2": 439,
}


def instructions(profile=PROFILE, *, machine=None):
    if profile != PROFILE or (machine or platform.machine()) != "x86_64":
        raise ResourceBlocked("unsupported seccomp profile or architecture")
    # Classic BPF: check architecture, then reject x32, then explicit cases.
    ins = [(0x20, 0, 0, 4), (0x15, 1, 0, AUDIT_ARCH_X86_64), (0x06, 0, 0, KILL_PROCESS),
           (0x20, 0, 0, 0), (0x45, 0, 1, 0x40000000), (0x06, 0, 0, KILL_PROCESS)]
    # socket/socketpair: only AF_UNIX (1); no IP or packet socket creation.
    for number in (41, 53):
        ins.extend([(0x15, 0, 4, number), (0x20, 0, 0, 16),
                    (0x15, 0, 1, 1), (0x06, 0, 0, ALLOW), (0x06, 0, 0, DENY)])
    # clone flags: forbid every namespace flag (including CLONE_NEWTIME), retain
    # ordinary fork/thread support, all of whose tasks stay in the limited leaf.
    ins.extend([(0x15, 0, 4, 56), (0x20, 0, 0, 16),
                (0x45, 0, 1, 0x7E020080), (0x06, 0, 0, DENY), (0x06, 0, 0, ALLOW)])
    # clone3 has pointer-valued flags, so return ENOSYS for libc's clone fallback.
    ins.extend([(0x15, 0, 1, 435), (0x06, 0, 0, 0x00050000 | errno.ENOSYS)])
    for number in sorted(SYSCALLS.values()):
        ins.extend([(0x15, 0, 1, number), (0x06, 0, 0, ALLOW)])
    ins.append((0x06, 0, 0, DENY))
    if len(ins) > 512:
        raise ResourceBlocked("seccomp profile exceeds reviewed instruction bound")
    return ins


def program(profile=PROFILE):
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions(profile))


def sealed_fd(profile=PROFILE):
    data = program(profile)
    if not hasattr(os, "memfd_create"):
        raise ResourceBlocked("sealed seccomp descriptors are unavailable")
    fd = os.memfd_create("keel-seccomp-v1", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        if os.write(fd, data) != len(data):
            raise ResourceBlocked("short seccomp filter write")
        os.lseek(fd, 0, os.SEEK_SET)
        # Linux UAPI constants; some embedded Python builds omit the symbols.
        seals = 0x0008 | 0x0004 | 0x0002 | 0x0001
        fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), seals)
        if fcntl.fcntl(fd, getattr(fcntl, "F_GET_SEALS", 1034)) & seals != seals:
            raise ResourceBlocked("seccomp filter seals could not be verified")
        return fd
    except BaseException:
        os.close(fd)
        raise


def doctor(profile=PROFILE):
    try:
        data = program(profile)
        fd = sealed_fd(profile)
        os.close(fd)
        return {"profile": profile, "status": "COMPILED_UNVERIFIED", "enforced": False,
                "sha256": hashlib.sha256(data).hexdigest(), "instructions": len(data) // 8}
    except (OSError, ResourceBlocked) as exc:
        return {"profile": profile, "status": "BLOCKED", "enforced": False, "reason": str(exc)}
