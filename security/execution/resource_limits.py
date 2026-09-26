"""Explicit cgroup v2 delegation, scoped to fresh job leaves.

This is trusted supervisor code. It never enables controllers, edits a parent
limit, moves an existing PID, or discovers a writable delegation automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import stat
import threading
import time
import uuid


class ResourceBlocked(ValueError):
    """Requested kernel enforcement is unavailable or cannot be verified."""


_CGROUP2_SUPER_MAGIC = 0x63677270
_CONTROLLERS = {"cpu", "memory", "pids"}
_MAX_READ = 4096


@dataclass(frozen=True)
class WorkerLimits:
    cgroup_root: str
    memory_bytes: int = 268435456
    pids: int = 32
    cpu_quota_us: int = 50000

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict) or set(raw) != {"cgroup_root", "memory_bytes", "pids", "cpu_quota_us"}:
            raise ResourceBlocked("limits require exactly cgroup_root, memory_bytes, pids, cpu_quota_us")
        path = raw["cgroup_root"]
        if (not isinstance(path, str) or not path.startswith("/sys/fs/cgroup/") or
                "\x00" in path or str(Path(path)) != path or ".." in Path(path).parts):
            raise ResourceBlocked("explicit canonical delegation below /sys/fs/cgroup is required")
        for key, low, high in (("memory_bytes", 16 * 1024**2, 64 * 1024**3),
                               ("pids", 4, 4096), ("cpu_quota_us", 1000, 6400000)):
            if type(raw[key]) is not int or not low <= raw[key] <= high:
                raise ResourceBlocked("invalid " + key)
        return cls(**raw)

    def values(self):
        return {"cpu.max": f"{self.cpu_quota_us} 100000", "memory.max": str(self.memory_bytes),
                "memory.swap.max": "0", "memory.oom.group": "1", "pids.max": str(self.pids)}


def _cgroup_filesystem(fd):
    # A generously sized output buffer avoids dependence on libc's statfs layout;
    # Linux f_type is the first native long. No paths are passed to libc.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fstatfs.argtypes = (ctypes.c_int, ctypes.c_void_p)
    libc.fstatfs.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(256)
    if libc.fstatfs(fd, buf) != 0:
        raise OSError(ctypes.get_errno(), "fstatfs failed")
    return ctypes.c_long.from_buffer(buf).value == _CGROUP2_SUPER_MAGIC


def _read(fd, name):
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        data = os.read(child, _MAX_READ + 1)
        if len(data) > _MAX_READ:
            raise ResourceBlocked("oversized cgroup control file")
        return data.decode("ascii").strip()
    finally:
        os.close(child)


def _write(fd, name, value):
    child = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        data = (value + "\n").encode("ascii")
        if os.write(child, data) != len(data):
            raise ResourceBlocked("short cgroup control write")
    finally:
        os.close(child)


def _root(limits):
    """Pin every path component; never accept a normal-directory cgroup mock."""
    limits = WorkerLimits.from_dict(vars(limits))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(limits.cgroup_root).parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                raise ResourceBlocked("untrusted delegation owner or writable ancestor")
        if not _cgroup_filesystem(fd):
            raise ResourceBlocked("delegation is not on cgroup v2")
        mount = os.stat("/sys/fs/cgroup", follow_symlinks=False)
        selected = os.fstat(fd)
        if selected.st_dev != mount.st_dev or selected.st_ino == mount.st_ino:
            raise ResourceBlocked("delegation must be a descendant, never the cgroup mount root")
        if _read(fd, "cgroup.type") != "domain":
            raise ResourceBlocked("domain delegation required")
        if not _CONTROLLERS <= set(_read(fd, "cgroup.controllers").split()):
            raise ResourceBlocked("required cgroup controllers unavailable")
        if not _CONTROLLERS <= set(_read(fd, "cgroup.subtree_control").split()):
            raise ResourceBlocked("host must enable cpu, memory and pids before launch")
        if _read(fd, "cgroup.procs"):
            raise ResourceBlocked("delegation parent must not contain processes")
        if not os.access(f"/proc/self/fd/{fd}", os.W_OK, effective_ids=True):
            raise ResourceBlocked("delegation is not writable by supervisor")
        return fd
    except BaseException:
        os.close(fd)
        raise


def single_threaded_supervisor():
    # preexec_fn is deliberately restricted to a dedicated, single-threaded
    # supervisor. Both Python threads and native threads must be absent.
    if threading.active_count() != 1 or len(os.listdir("/proc/self/task")) != 1:
        raise ResourceBlocked("resource launcher requires a dedicated single-threaded supervisor")


class CgroupLease:
    """One fresh leaf. Its writable descriptors never reach guest code."""
    def __init__(self, limits):
        self.limits = WorkerLimits.from_dict(vars(limits))
        self.root_fd = self.fd = self.procs_fd = None
        self.name = "keel-job-" + uuid.uuid4().hex
        self.created = False
        self.closed = False
        self.supervisor_pid = os.getpid()

    def __enter__(self):
        if os.getpid() != self.supervisor_pid:
            raise ResourceBlocked("cgroup lease belongs to another supervisor")
        single_threaded_supervisor()
        self.root_fd = _root(self.limits)
        try:
            os.mkdir(self.name, mode=0o700, dir_fd=self.root_fd)
            self.created = True
            self.fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=self.root_fd)
            if not _cgroup_filesystem(self.fd) or _read(self.fd, "cgroup.procs"):
                raise ResourceBlocked("new cgroup leaf is invalid or populated")
            # All writes are limited to the newly created, descriptor-pinned leaf.
            for name, value in self.limits.values().items():
                _write(self.fd, name, value)
                if _read(self.fd, name) != value:
                    raise ResourceBlocked("cgroup limit readback mismatch: " + name)
            kill_fd = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
            os.close(kill_fd)
            if "populated 0" not in _read(self.fd, "cgroup.events").splitlines():
                raise ResourceBlocked("new cgroup leaf must be empty")
            self.procs_fd = os.open("cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
            return self
        except BaseException:
            self.close()
            raise

    def attach_before_exec(self):
        """Child-only hook: kernel interprets 0 as the calling process.

        No guest bytes run, no guest child can fork, and no PID is guessed before
        placement. Popen reports any failure through its exec-error pipe.
        """
        if os.getpid() == self.supervisor_pid or os.getppid() != self.supervisor_pid:
            raise ResourceBlocked("cgroup placement is restricted to the supervisor's direct child")
        if os.write(self.procs_fd, b"0\n") != 2:
            raise ResourceBlocked("cgroup self-placement failed")
        os.close(self.procs_fd)

    def kill(self):
        if self.fd is not None:
            _write(self.fd, "cgroup.kill", "1")

    def measurement(self):
        if self.fd is None:
            raise ResourceBlocked("cgroup is not active")
        return {name: _read(self.fd, name) for name in
                ("cpu.stat", "memory.events", "pids.events")}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.fd is not None:
                self.kill()
                deadline = time.monotonic() + 2
                while "populated 0" not in _read(self.fd, "cgroup.events").splitlines():
                    if time.monotonic() >= deadline:
                        raise ResourceBlocked("cgroup cleanup timed out; host inspection required")
                    time.sleep(0.01)
            if self.created:
                os.rmdir(self.name, dir_fd=self.root_fd)
        finally:
            for name in ("procs_fd", "fd", "root_fd"):
                value = getattr(self, name)
                if value is not None:
                    os.close(value)
                    setattr(self, name, None)

    def __exit__(self, exc_type, exc, tb):
        self.close()


def doctor(limits=None):
    """Read-only preflight, never a claim that a process has been constrained."""
    result = {"configured": limits is not None, "enforced": False,
              "status": "NOT_CONFIGURED", "scope": "read_only_preflight"}
    if limits is None:
        return result
    try:
        if isinstance(limits, dict):
            limits = WorkerLimits.from_dict(limits)
        single_threaded_supervisor()
        fd = _root(limits)
        os.close(fd)
        result.update(status="AVAILABLE_UNVERIFIED", requested=limits.values())
    except (OSError, ResourceBlocked, ValueError, TypeError) as exc:
        result.update(status="BLOCKED", reason=str(exc))
    return result


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Read-only worker cgroup delegation preflight")
    parser.add_argument("--cgroup-root")
    options = parser.parse_args()
    requested = None if options.cgroup_root is None else WorkerLimits(options.cgroup_root)
    output = doctor(requested)
    print(json.dumps(output, sort_keys=True))
    raise SystemExit(0 if output["status"] == "AVAILABLE_UNVERIFIED" else 2)
