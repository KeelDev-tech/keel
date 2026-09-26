"""Linux worker isolation. No execution without a successfully constructed sandbox.

This is an operator-side launcher, never an agent tool. Configuration and mounted
code must be owned by a different host identity than the worker. No secrets,
browser profile, policy directory, host root or host writable directory is bound.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import signal
import socket
import stat
import subprocess
import time
from typing import Any

from .resource_limits import CgroupLease, ResourceBlocked, WorkerLimits, doctor as resource_doctor
from .seccomp_profile import PROFILE, doctor as seccomp_doctor, sealed_fd


class IsolationBlocked(ValueError):
    """The required isolation contract could not be established."""


_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CONFIG = 8 * 1024 * 1024
_MOUNTS = ("app", "work", "tmp", "run", "proc", "dev")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IsolationBlocked("duplicate JSON key")
        result[key] = value
    return result


def _json(data: bytes):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              IsolationBlocked("non-finite JSON number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IsolationBlocked("malformed configuration") from exc


def _absolute(value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise IsolationBlocked("path must be absolute")
    path = Path(value)
    if str(path) != value or ".." in path.parts or path == Path("/"):
        raise IsolationBlocked("noncanonical or root path")
    return path


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise IsolationBlocked("invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in (".", "..") for p in path.parts):
        raise IsolationBlocked("noncanonical relative path")
    return value


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise IsolationBlocked("invalid " + name)
    return value


def _inventory(value):
    if not isinstance(value, dict) or not value or len(value) > 50000:
        raise IsolationBlocked("inventory must be a nonempty bounded mapping")
    result = {}
    for name, digest in value.items():
        name = _relative(name)
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise IsolationBlocked("invalid inventory digest")
        result[name] = digest
    return result


@dataclass(frozen=True)
class IsolationConfig:
    runtime: str
    application: str
    runtime_inventory: dict[str, str]
    application_inventory: dict[str, str]
    entrypoint: str
    worker_uid: int
    broker_uid: int
    broker_socket: str | None = None
    arguments: tuple[str, ...] = ()
    wall_seconds: int = 60
    output_bytes: int = 1048576
    scratch_bytes: int = 16777216
    resource_limits: dict | None = None
    seccomp_profile: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "IsolationConfig":
        if not isinstance(raw, dict):
            raise IsolationBlocked("configuration must be an object")
        required = {"schema", "runtime", "application", "runtime_inventory",
                    "application_inventory", "entrypoint", "worker_uid", "broker_uid"}
        optional = {"broker_socket", "arguments", "wall_seconds", "output_bytes", "scratch_bytes",
                    "resource_limits", "seccomp_profile"}
        if not required <= raw.keys() or raw.keys() - required - optional:
            raise IsolationBlocked("missing or unknown configuration key")
        if type(raw["schema"]) is not int or raw["schema"] != 1:
            raise IsolationBlocked("unsupported configuration schema")
        runtime, application = _absolute(raw["runtime"]), _absolute(raw["application"])
        if runtime == application or runtime in application.parents or application in runtime.parents:
            raise IsolationBlocked("runtime/application overlap")
        forbidden = (Path("/proc"), Path("/sys"), Path("/dev"))
        if any(root == p or p in root.parents for root in (runtime, application) for p in forbidden):
            raise IsolationBlocked("special filesystem root")
        args = raw.get("arguments", [])
        if (not isinstance(args, list) or len(args) > 32 or
                any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 4096 for arg in args)):
            raise IsolationBlocked("invalid fixed application arguments")
        worker = _integer(raw["worker_uid"], "worker_uid", 0, 2**31 - 1)
        broker = _integer(raw["broker_uid"], "broker_uid", 0, 2**31 - 1)
        if worker == broker:
            raise IsolationBlocked("worker and executor must use different host UIDs")
        endpoint = raw.get("broker_socket")
        if endpoint is not None:
            endpoint = str(_absolute(endpoint))
            if any(Path(endpoint) == root or root in Path(endpoint).parents
                   for root in (runtime, application)):
                raise IsolationBlocked("broker endpoint overlaps mounted source")
        entrypoint = _relative(raw["entrypoint"])
        if not entrypoint.endswith(".py"):
            raise IsolationBlocked("entrypoint must be a reviewed Python file")
        app_inventory = _inventory(raw["application_inventory"])
        if entrypoint not in app_inventory:
            raise IsolationBlocked("entrypoint is absent from reviewed inventory")
        limits = raw.get("resource_limits")
        if limits is not None:
            limits = vars(WorkerLimits.from_dict(limits)).copy()
        profile = raw.get("seccomp_profile") or (PROFILE if limits is not None else None)
        if raw.get("seccomp_profile") not in (None, PROFILE) or profile not in (None, PROFILE):
            raise IsolationBlocked("unsupported seccomp profile")
        return cls(str(runtime), str(application), _inventory(raw["runtime_inventory"]),
                   app_inventory, entrypoint, worker, broker, endpoint, tuple(args),
                   _integer(raw.get("wall_seconds", 60), "wall_seconds", 1, 3600),
                   _integer(raw.get("output_bytes", 1048576), "output_bytes", 1024, 16777216),
                   _integer(raw.get("scratch_bytes", 16777216), "scratch_bytes", 1048576, 268435456),
                   limits, profile)


def _trusted(st, worker_uid, *, probe=False, directory=False):
    if stat.S_ISLNK(st.st_mode):
        raise IsolationBlocked("symlinks are not allowed")
    if st.st_mode & 0o022 and not (probe and directory and st.st_mode & stat.S_ISVTX):
        raise IsolationBlocked("group/world-writable source or ancestor")
    if not probe and st.st_uid == worker_uid:
        raise IsolationBlocked("source or ancestor is owned by worker")
    if st.st_mode & (stat.S_ISUID | stat.S_ISGID) and not directory:
        raise IsolationBlocked("set-ID source is forbidden")


def _open_path(path: str, worker_uid: int, *, probe=False, directory=False, endpoint=False):
    """Walk each component without following symlinks; pin final inode with a FD."""
    p = _absolute(path)
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _trusted(os.fstat(fd), worker_uid, probe=probe, directory=True)
        for i, part in enumerate(p.parts[1:]):
            last = i == len(p.parts[1:]) - 1
            flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
            if not last or directory:
                flags |= os.O_DIRECTORY
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
            st = os.fstat(fd)
            if last and endpoint and stat.S_ISSOCK(st.st_mode):
                if st.st_uid == worker_uid or st.st_mode & 0o007:
                    raise IsolationBlocked("untrusted executor socket")
            else:
                _trusted(st, worker_uid, probe=probe, directory=not last or directory)
                if not probe and os.access(f"/proc/self/fd/{fd}", os.W_OK, effective_ids=True):
                    raise IsolationBlocked("worker can modify a source or its ancestor")
        return fd
    except BaseException:
        os.close(fd)
        raise


def load_config(path: str) -> IsolationConfig:
    """Only operator-owned, non-writable config is accepted by the production CLI."""
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        raise IsolationBlocked("production launcher requires an ordinary nonroot worker UID")
    fd = _open_path(str(_absolute(path)), os.getuid())
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _MAX_CONFIG:
            raise IsolationBlocked("invalid configuration file")
        with open(f"/proc/self/fd/{fd}", "rb") as stream:
            data = stream.read(_MAX_CONFIG + 1)
        if len(data) > _MAX_CONFIG:
            raise IsolationBlocked("configuration too large")
        return IsolationConfig.from_dict(_json(data))
    finally:
        os.close(fd)


def _runtime_name(name):
    parts = PurePosixPath(name).parts
    if parts[-1] in ("sitecustomize.py", "usercustomize.py"):
        return False
    return (name == "usr/bin/python3" or
            (len(parts) >= 4 and parts[:2] == ("usr", "lib") and
             re.fullmatch(r"python3\.[0-9]+", parts[2]) is not None and
             not {"site-packages", "dist-packages"}.intersection(parts)) or
            (len(parts) >= 2 and parts[0] in ("lib", "lib64") and
             ".so" in parts[-1]))


def inventory_tree(path: str) -> dict[str, str]:
    """Operator utility: pin bytes, not a claim that content was security reviewed."""
    root = _absolute(path)
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise IsolationBlocked("inventory root must be a real directory without symlink ancestors")
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            target = Path(current) / name
            st = target.lstat()
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise IsolationBlocked("inventory contains symlink or special file")
            if stat.S_ISREG(st.st_mode):
                if st.st_nlink != 1:
                    raise IsolationBlocked("hard-linked file is forbidden")
                with target.open("rb") as stream:
                    result[target.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _check_tree(fd, expected, worker_uid, *, runtime=False, probe=False):
    root = Path(f"/proc/self/fd/{fd}")
    found = {}
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            st = path.lstat()
            _trusted(st, worker_uid, probe=probe, directory=stat.S_ISDIR(st.st_mode))
            if not probe and os.access(path, os.W_OK, effective_ids=True):
                raise IsolationBlocked("worker can modify a mounted source")
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise IsolationBlocked("special file in mounted tree")
            rel = path.relative_to(root).as_posix()
            if stat.S_ISDIR(st.st_mode):
                if runtime and not (rel in _MOUNTS or rel in ("usr", "usr/bin", "usr/lib", "lib", "lib64")
                                    or rel.startswith("lib/") or re.match(r"usr/lib/python3\.[0-9]+(?:/|$)", rel)):
                    raise IsolationBlocked("unexpected runtime directory")
                if runtime and any(rel.startswith(name + "/") for name in _MOUNTS):
                    raise IsolationBlocked("runtime mount placeholder is not empty")
                continue
            total += st.st_size
            if st.st_nlink != 1 or total > 1024 * 1024 * 1024 or len(found) >= 50000:
                raise IsolationBlocked("mounted tree exceeds bounds or contains hardlinks")
            if rel not in expected or (runtime and not _runtime_name(rel)):
                raise IsolationBlocked("unreviewed file in mounted tree")
            source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(source, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                    raise IsolationBlocked("source changed during review")
                found[rel] = hashlib.file_digest(stream, "sha256").hexdigest()
    if found != expected:
        raise IsolationBlocked("reviewed inventory does not match source bytes")
    if runtime:
        if "usr/bin/python3" not in found:
            raise IsolationBlocked("reviewed Python interpreter missing")
        for name in _MOUNTS:
            p = root / name
            if not p.is_dir() or any(p.iterdir()):
                raise IsolationBlocked("runtime requires empty mount placeholders")
        with (root / "usr/bin/python3").open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise IsolationBlocked("runtime interpreter is not an ELF binary")


def _command(config, runtime_fd, app_fd, broker_fd, seccomp_fd=None):
    command = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
               "--unshare-ipc", "--unshare-uts", "--unshare-cgroup", "--disable-userns",
               "--assert-userns-disabled", "--uid", str(config.worker_uid),
               "--gid", str(os.getgid()), "--new-session", "--die-with-parent",
               "--cap-drop", "ALL", "--clearenv", "--hostname", "keel-worker",
               "--ro-bind-fd", str(runtime_fd), "/", "--ro-bind-fd", str(app_fd), "/app",
               "--proc", "/proc", "--dev", "/dev",
               "--size", str(config.scratch_bytes), "--tmpfs", "/work",
               "--size", str(config.scratch_bytes), "--tmpfs", "/tmp",
               "--size", "1048576", "--tmpfs", "/run", "--dir", "/run/keel"]
    if broker_fd is not None:
        command += ["--ro-bind-fd", str(broker_fd), "/run/keel/executor.sock",
                    "--setenv", "KEEL_EXECUTOR_SOCKET", "/run/keel/executor.sock"]
    for name, value in (("PATH", "/usr/bin"), ("HOME", "/work"), ("TMPDIR", "/tmp"), ("LANG", "C.UTF-8")):
        command += ["--setenv", name, value]
    if seccomp_fd is not None:
        command += ["--seccomp", str(seccomp_fd)]
    command += ["--chdir", "/work", "--", "/usr/bin/python3", "-I", "-S", "-B",
                "/app/" + config.entrypoint, *config.arguments]
    return command


def _capture(command, fds, executable, config, lease=None):
    """Bound host-side output and wall time; killing bwrap tears down its PID tree."""
    child_fds = list(fds) + ([lease.procs_fd] if lease is not None else [])
    process = subprocess.Popen(command, executable=executable, env={}, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, pass_fds=tuple(child_fds), start_new_session=True,
                               preexec_fn=lease.attach_before_exec if lease is not None else None)
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    end = time.monotonic() + config.wall_seconds
    reason = None
    cleanup_error = None
    def kill():
        nonlocal cleanup_error
        if lease is not None:
            try:
                lease.kill()
            except (OSError, ResourceBlocked) as exc:
                cleanup_error = str(exc)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    with selectors.DefaultSelector() as selector:
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        try:
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    reason = "wall_deadline"
                    break
                for key, _ in selector.select(min(0.1, remaining)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    used = sum(len(value) for value in chunks.values())
                    room = config.output_bytes - used
                    chunks[key.data].extend(data[:max(0, room)])
                    if len(data) > room:
                        reason = "output_limit"
                        break
                if reason:
                    break
            if reason:
                kill()
            try:
                code = process.wait(timeout=max(0.1, end - time.monotonic()) if not reason else 5)
            except subprocess.TimeoutExpired:
                reason = "wall_deadline"
                kill()
                code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
    if cleanup_error is not None:
        raise ResourceBlocked("cgroup kill failed: " + cleanup_error)
    return {"status": "COMPLETED" if code == 0 and reason is None else "BLOCKED",
            "returncode": code, "reason": reason or ("worker_or_sandbox_failed" if code else None),
            **{k: bytes(v).decode("utf-8", "replace") for k, v in chunks.items()}}


def launch(config: IsolationConfig, *, execute=False, _synthetic_probe=False) -> dict[str, Any]:
    """Validate and plan by default. Execution never falls back outside bubblewrap.

    _synthetic_probe relaxes ownership solely for ephemeral conformance fixtures.
    It is absent from the production CLI, and cannot be mixed with broker access.
    This module must remain in the trusted supervisor, outside agent execution.
    """
    execution_attempted = False
    result = None
    try:
        # Reparse a private copy to reject mutated dictionaries or manual invalid dataclasses.
        config = IsolationConfig.from_dict({"schema": 1, **{
            name: (list(getattr(config, name)) if name == "arguments" else getattr(config, name))
            for name in config.__dataclass_fields__}})
        if platform.system() != "Linux" or not hasattr(os, "O_PATH"):
            raise IsolationBlocked("Linux O_PATH isolation is required")
        if config.worker_uid != os.getuid() or os.getuid() != os.geteuid():
            raise IsolationBlocked("launcher must run as the configured worker UID")
        if _synthetic_probe and config.broker_socket is not None:
            raise IsolationBlocked("synthetic probe cannot access an executor socket")
        if not _synthetic_probe and os.getuid() == 0:
            raise IsolationBlocked("production root worker is forbidden")
        enforcement = {"resources": resource_doctor(config.resource_limits),
                       "seccomp": seccomp_doctor(config.seccomp_profile) if config.seccomp_profile else
                           {"status": "NOT_CONFIGURED", "enforced": False}}
        for control in enforcement.values():
            if control["status"] == "BLOCKED":
                raise IsolationBlocked(control["reason"])
        with ExitStack() as stack:
            fds = []
            def pin(path, directory=False, endpoint=False):
                fd = _open_path(path, config.worker_uid, probe=_synthetic_probe, directory=directory, endpoint=endpoint)
                stack.callback(os.close, fd)
                fds.append(fd)
                return fd
            runtime_fd, app_fd = pin(config.runtime, True), pin(config.application, True)
            _check_tree(runtime_fd, config.runtime_inventory, config.worker_uid, runtime=True, probe=_synthetic_probe)
            _check_tree(app_fd, config.application_inventory, config.worker_uid, probe=_synthetic_probe)
            binary_fd = pin("/usr/bin/bwrap")
            binary_stat = os.fstat(binary_fd)
            if not stat.S_ISREG(binary_stat.st_mode) or binary_stat.st_uid != 0 or not binary_stat.st_mode & 0o111:
                raise IsolationBlocked("bubblewrap must be a root-owned executable regular file")
            broker_fd = None
            if config.broker_socket is not None:
                broker_fd = pin(config.broker_socket, endpoint=True)
                st = os.fstat(broker_fd)
                if not stat.S_ISSOCK(st.st_mode) or st.st_uid != config.broker_uid or st.st_gid != os.getgid() or st.st_mode & 0o007:
                    raise IsolationBlocked("executor socket ownership or permissions mismatch")
            filter_fd = None
            if config.seccomp_profile:
                filter_fd = sealed_fd(config.seccomp_profile)
                stack.callback(os.close, filter_fd)
                fds.append(filter_fd)
            command = _command(config, runtime_fd, app_fd, broker_fd, filter_fd)
            if not execute:
                return {"status": "PLANNED", "executed": False, "host_uid": os.getuid(),
                        "argv": command, "enforcement": enforcement, "descriptor_arguments": "valid only inside this call",
                        "runtime_sha256": hashlib.sha256(json.dumps(config.runtime_inventory, sort_keys=True).encode()).hexdigest(),
                        "application_sha256": hashlib.sha256(json.dumps(config.application_inventory, sort_keys=True).encode()).hexdigest()}
            lease = stack.enter_context(CgroupLease(WorkerLimits.from_dict(config.resource_limits))) if config.resource_limits else None
            execution_attempted = True
            result = _capture(command, fds, f"/proc/self/fd/{binary_fd}", config, lease)
            if lease is not None:
                enforcement["resources"].update(status="ENFORCED_FOR_LAUNCH", enforced=True,
                                                 measurements=lease.measurement())
                # Check cleanup before reporting success; this kills escaped sessions
                # and descendants even when the top-level process has already exited.
                lease.close()
            if config.seccomp_profile:
                # Nonzero bwrap exit might precede filter installation. Do not infer it.
                enforcement["seccomp"].update(status="ENFORCED" if result["status"] == "COMPLETED" else "INSTALLATION_UNCONFIRMED",
                                               enforced=result["status"] == "COMPLETED")
            result["enforcement"] = enforcement
            result.update({"executed": True, "host_uid": os.getuid(), "synthetic_probe": _synthetic_probe})
            return result
    except (IsolationBlocked, ResourceBlocked, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        return {"status": "BLOCKED", "executed": True if result is not None else (None if execution_attempted else False),
                "execution_attempted": execution_attempted, "reason": str(exc)}
