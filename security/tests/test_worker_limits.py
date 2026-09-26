"""Scope/ordering fixtures are not substitutes for Linux quota qualification."""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from security.execution import isolation as iso
from security.execution import resource_limits as rl
from security.execution import seccomp_profile as sc


@pytest.fixture
def guarded_python_probe(monkeypatch):
    """Keep the audit hook in these direct-Python test probes only.

    Production _capture deliberately starts bubblewrap with env={}. The probes
    below exercise pre-exec ordering using a synthetic Python child, so the
    suite's audit guard must also reach that child. No production launcher or
    guard behavior is changed.
    """
    if not os.environ.get("KEEL_AUDIT_TEST_ROOT"):
        return
    original = iso.subprocess.Popen
    def guarded_popen(command, *args, **kwargs):
        assert command[0] == sys.executable and "-c" in command
        assert kwargs.get("env") == {}
        kwargs["env"] = {key: os.environ[key] for key in
                         ("KEEL_AUDIT_TEST_ROOT", "KEEL_AUDIT_REPORT", "KEEL_AUDIT_CODE", "PYTHONPATH")
                         if key in os.environ}
        kwargs["env"]["PYTHONDONTWRITEBYTECODE"] = "1"
        return original(command, *args, **kwargs)
    monkeypatch.setattr(iso.subprocess, "Popen", guarded_popen)


def limits(**changes):
    return {"cgroup_root": "/sys/fs/cgroup/keel-test-delegation", "memory_bytes": 32 * 1024**2,
            "pids": 4, "cpu_quota_us": 1000, **changes}


def config(**changes):
    return iso.IsolationConfig.from_dict({"schema": 1, "runtime": "/opt/keel-runtime",
        "application": "/opt/keel-application", "runtime_inventory": {"usr/bin/python3": "a" * 64},
        "application_inventory": {"worker.py": "b" * 64}, "entrypoint": "worker.py",
        "worker_uid": os.getuid(), "broker_uid": os.getuid() + 1, **changes})


@pytest.mark.parametrize("field,value", [
    ("cgroup_root", "/sys/fs/cgroup"), ("cgroup_root", "/sys/fs/cgroup/a/../b"),
    ("cgroup_root", "/sys/fs/cgroup/a/"), ("cgroup_root", "/tmp/fake"),
    ("cgroup_root", "/sys/fs/cgroup//a"), ("pids", True), ("pids", 3),
    ("memory_bytes", 0), ("memory_bytes", 1e9), ("cpu_quota_us", 0),
    ("cpu_quota_us", 6400001), ("extra", "value")])
def test_limits_strictly_bounded(field, value):
    with pytest.raises(rl.ResourceBlocked):
        rl.WorkerLimits.from_dict(limits(**{field: value}))


def test_legacy_config_reports_no_requested_enforcement():
    c = config()
    assert c.resource_limits is None and c.seccomp_profile is None
    assert rl.doctor() == {"configured": False, "enforced": False,
                           "status": "NOT_CONFIGURED", "scope": "read_only_preflight"}


def test_requested_limits_require_versioned_seccomp():
    c = config(resource_limits=limits())
    assert c.seccomp_profile == sc.PROFILE
    assert config(resource_limits=limits(), seccomp_profile=None).seccomp_profile == sc.PROFILE
    with pytest.raises(iso.IsolationBlocked):
        config(resource_limits=limits(), seccomp_profile="arbitrary-profile")


def test_missing_delegation_blocks_before_capture(monkeypatch):
    monkeypatch.setattr(iso, "resource_doctor", lambda _: {"status": "BLOCKED", "reason": "no delegation"})
    monkeypatch.setattr(iso, "_capture", lambda *a, **k: pytest.fail("must not dispatch"))
    result = iso.launch(config(resource_limits=limits()), execute=True, _synthetic_probe=True)
    assert result["status"] == "BLOCKED" and result["executed"] is False
    assert result["reason"] == "no delegation"


def test_normal_filesystem_never_qualifies(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert rl._cgroup_filesystem(fd) is False
    finally:
        os.close(fd)


def test_multithreaded_supervisor_refused(monkeypatch):
    monkeypatch.setattr(rl.threading, "active_count", lambda: 2)
    with pytest.raises(rl.ResourceBlocked, match="single-threaded"):
        rl.single_threaded_supervisor()


def test_native_threads_are_also_refused(monkeypatch):
    monkeypatch.setattr(rl.threading, "active_count", lambda: 1)
    monkeypatch.setattr(rl.os, "listdir", lambda _: ["100", "101"])
    with pytest.raises(rl.ResourceBlocked, match="single-threaded"):
        rl.single_threaded_supervisor()


def fake_leaf(tmp_path, monkeypatch, *, mismatch=False):
    """Intercept kernel operations solely to check scope and ordering."""
    rootfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(rl, "_root", lambda _: os.dup(rootfd))
    monkeypatch.setattr(rl, "single_threaded_supervisor", lambda: None)
    monkeypatch.setattr(rl, "_cgroup_filesystem", lambda _: True)
    writes, values = [], {}
    def write(fd, name, value):
        writes.append((os.readlink(f"/proc/self/fd/{fd}"), name, value))
        values[name] = value
    def read(fd, name):
        if name == "cgroup.procs": return ""
        if name == "cgroup.events": return "populated 0\nfrozen 0"
        if name in values: return "wrong" if mismatch and name == "pids.max" else values[name]
        return ""
    monkeypatch.setattr(rl, "_write", write)
    monkeypatch.setattr(rl, "_read", read)
    original_open = os.open
    def fake_open(name, flags, *args, **kwargs):
        if name in ("cgroup.kill", "cgroup.procs"):
            return original_open(os.devnull, os.O_WRONLY)
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(rl.os, "open", fake_open)
    return rootfd, writes


def test_only_fresh_leaf_is_mutated_and_killed(tmp_path, monkeypatch):
    rootfd, writes = fake_leaf(tmp_path, monkeypatch)
    try:
        with rl.CgroupLease(rl.WorkerLimits.from_dict(limits())) as lease:
            assert lease.procs_fd is not None
            assert [row[1] for row in writes] == list(lease.limits.values())
            assert lease.name.startswith("keel-job-")
            name = lease.name
        assert writes[-1][1:] == ("cgroup.kill", "1")
        assert all(Path(row[0]) == tmp_path / name for row in writes)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(rootfd)


def test_readback_mismatch_cleans_up_before_dispatch(tmp_path, monkeypatch):
    rootfd, writes = fake_leaf(tmp_path, monkeypatch, mismatch=True)
    try:
        with pytest.raises(rl.ResourceBlocked, match="readback"):
            with rl.CgroupLease(rl.WorkerLimits.from_dict(limits())):
                pytest.fail("must not become available")
        assert writes[-1][1] == "cgroup.kill"
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(rootfd)


def test_placement_hook_uses_self_and_closes_fd(monkeypatch):
    lease = rl.CgroupLease(rl.WorkerLimits.from_dict(limits()))
    lease.procs_fd = 91
    monkeypatch.setattr(rl.os, "getpid", lambda: lease.supervisor_pid + 1)
    monkeypatch.setattr(rl.os, "getppid", lambda: lease.supervisor_pid)
    calls = []
    monkeypatch.setattr(rl.os, "write", lambda fd, data: calls.append((fd, data)) or len(data))
    monkeypatch.setattr(rl.os, "close", lambda fd: calls.append(("close", fd)))
    lease.attach_before_exec()
    assert calls == [(91, b"0\n"), ("close", 91)]


def test_placement_failure_never_executes_guest(tmp_path, guarded_python_probe):
    marker = tmp_path / "executed"
    lease = SimpleNamespace(procs_fd=os.open(os.devnull, os.O_WRONLY))
    def fail(): raise RuntimeError("placement failed")
    lease.attach_before_exec = fail
    try:
        with pytest.raises(subprocess.SubprocessError):
            iso._capture([sys.executable, "-c", "from pathlib import Path; Path(__import__('sys').argv[1]).touch()", str(marker)],
                         [], sys.executable, SimpleNamespace(wall_seconds=2, output_bytes=1024), lease)
        assert not marker.exists()
    finally:
        os.close(lease.procs_fd)


def test_guest_code_runs_after_placement_hook(tmp_path, guarded_python_probe):
    order = tmp_path / "order"
    lease = SimpleNamespace(procs_fd=os.open(os.devnull, os.O_WRONLY))
    def attach():
        with order.open("w") as stream: stream.write("placed")
        os.close(lease.procs_fd)
    lease.attach_before_exec = attach
    try:
        result = iso._capture([sys.executable, "-c", "from pathlib import Path; p=Path(__import__('sys').argv[1]); assert p.read_text()=='placed'; print('observed')", str(order)],
                            [], sys.executable, SimpleNamespace(wall_seconds=2, output_bytes=1024), lease)
        assert result["status"] == "COMPLETED" and "observed" in result["stdout"]
    finally:
        os.close(lease.procs_fd)


def evaluate(instructions, number, *, arch=sc.AUDIT_ARCH_X86_64, arg0=0):
    data = {0: number, 4: arch, 16: arg0}
    index = accumulator = 0
    while index < len(instructions):
        code, yes, no, k = instructions[index]
        if code == 0x20: accumulator = data[k]
        elif code == 0x15: index += yes if accumulator == k else no
        elif code == 0x45: index += yes if accumulator & k else no
        elif code == 0x06: return k
        else: raise AssertionError("unknown BPF")
        index += 1
    raise AssertionError("no BPF return")


@pytest.mark.parametrize("number", [101, 165, 166, 250, 272, 298, 304, 308, 310, 311, 321, 323, 425, 426, 427, 999])
def test_sensitive_and_unknown_syscalls_denied(number):
    assert evaluate(sc.instructions(machine="x86_64"), number) == sc.DENY


def test_architecture_and_x32_bypass_rejected():
    ins = sc.instructions(machine="x86_64")
    assert evaluate(ins, 0, arch=0x40000003) == sc.KILL_PROCESS
    assert evaluate(ins, 0x40000000) == sc.KILL_PROCESS
    with pytest.raises(rl.ResourceBlocked): sc.instructions(machine="aarch64")


def test_socket_and_clone_argument_guards():
    ins = sc.instructions(machine="x86_64")
    for number in (41, 53):
        assert evaluate(ins, number, arg0=1) == sc.ALLOW
        for domain in (2, 10, 16, 17):
            assert evaluate(ins, number, arg0=domain) == sc.DENY
    assert evaluate(ins, 56, arg0=17) == sc.ALLOW
    for flag in (0x80, 0x20000, 0x2000000, 0x4000000, 0x8000000, 0x10000000, 0x20000000, 0x40000000):
        assert evaluate(ins, 56, arg0=flag) == sc.DENY
    assert evaluate(ins, 435) == 0x50000 | errno.ENOSYS


def test_every_reviewed_allowlist_number_allowed():
    ins = sc.instructions(machine="x86_64")
    assert len(set(sc.SYSCALLS.values())) == len(sc.SYSCALLS)
    assert len(ins) <= 512
    for number in sc.SYSCALLS.values():
        assert evaluate(ins, number) == sc.ALLOW


def test_filter_is_sealed_and_doctor_is_not_enforcement():
    if platform.machine() != "x86_64": pytest.skip("x86-64-only reviewed profile")
    fd = sc.sealed_fd()
    try:
        with pytest.raises(OSError): os.write(fd, b"bad")
        assert os.read(fd, 8192) == sc.program()
    finally:
        os.close(fd)
    result = sc.doctor()
    assert result["status"] == "COMPILED_UNVERIFIED" and result["enforced"] is False


def test_filter_passed_to_bwrap_before_guest_separator():
    command = iso._command(config(), 10, 11, None, 12)
    assert command[command.index("--seccomp") + 1] == "12"
    assert command.index("--seccomp") < command.index("--")


def test_real_kernel_seccomp_child():
    """Install only in a disposable child; no parent filter or host mutation."""
    if platform.machine() != "x86_64": pytest.skip("x86-64-only reviewed profile")
    source = r'''
import ctypes, errno, json, os, socket, struct
from security.execution.seccomp_profile import program
class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]
raw = program()
filters = (Filter * (len(raw) // 8)).from_buffer_copy(raw)
prog = Program(len(filters), filters)
lib = ctypes.CDLL(None, use_errno=True)
lib.prctl.restype = ctypes.c_int
# A harmless syscall is permitted before installation and denied afterwards.
before = lib.prctl(3, 0, 0, 0, 0)  # PR_GET_DUMPABLE
if before < 0: raise SystemExit(77)
if lib.prctl(38, 1, 0, 0, 0) != 0: raise SystemExit(77)
if lib.prctl(22, 2, ctypes.byref(prog), 0, 0) != 0: raise SystemExit(77)
assert lib.prctl(3, 0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
assert os.getpid() > 0
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError as error:
    assert error.errno == errno.EPERM
else: raise AssertionError("network socket unexpectedly allowed")
a, b = socket.socketpair(socket.AF_UNIX)
a.send(b"ok"); assert b.recv(2) == b"ok"
a.close(); b.close()
print(json.dumps({"filter_installed": True, "forbidden_prctl_denied": True,
                  "network_socket_denied": True, "unix_socket_allowed": True}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", source], capture_output=True, text=True, timeout=5)
    if result.returncode == 77:
        pytest.skip("kernel disallows disposable-child seccomp installation")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["filter_installed"] is True


def test_real_kernel_quotas_require_explicit_delegation():
    """Never discover or enable a writable cgroup on behalf of a test."""
    delegation = os.environ.get("KEEL_TEST_CGROUP_ROOT")
    if not delegation:
        pytest.skip("no explicit trusted writable cgroup v2 delegation; quota enforcement unqualified")
    requested = rl.WorkerLimits.from_dict(limits(cgroup_root=delegation))
    readiness = rl.doctor(requested)
    assert readiness["status"] == "AVAILABLE_UNVERIFIED", readiness
    with rl.CgroupLease(requested) as lease:
        process = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c",
                                    "import time; end=time.monotonic()+0.4\nwhile time.monotonic()<end: pass"],
                                   pass_fds=(lease.procs_fd,), preexec_fn=lease.attach_before_exec,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.communicate(timeout=5)
        assert process.returncode == 0
        counters = dict(line.split() for line in lease.measurement()["cpu.stat"].splitlines())
        assert int(counters["nr_throttled"]) > 0
        pids_source = """import errno, os, time
children=[]
blocked=False
try:
    for _ in range(8):
        try: child=os.fork()
        except OSError as error:
            if error.errno != errno.EAGAIN: raise
            blocked=True
            break
        if child == 0:
            time.sleep(0.5)
            os._exit(0)
        children.append(child)
finally:
    for child in children: os.waitpid(child, 0)
assert blocked
"""
        pids_hog = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", pids_source],
                                    pass_fds=(lease.procs_fd,), preexec_fn=lease.attach_before_exec,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, error = pids_hog.communicate(timeout=5)
        assert pids_hog.returncode == 0, error
        counters = dict(line.split() for line in lease.measurement()["pids.events"].splitlines())
        assert int(counters["max"]) > 0
        memory_hog = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c",
                                       "x=bytearray(128*1024*1024)"],
                                      pass_fds=(lease.procs_fd,), preexec_fn=lease.attach_before_exec,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        memory_hog.communicate(timeout=5)
        assert memory_hog.returncode != 0
        counters = dict(line.split() for line in lease.measurement()["memory.events"].splitlines())
        assert int(counters["oom_kill"]) > 0


def test_cgroup_kill_failure_still_kills_process_group(monkeypatch, guarded_python_probe):
    killed = []
    lease = SimpleNamespace(procs_fd=os.open(os.devnull, os.O_WRONLY))
    lease.attach_before_exec = lambda: os.close(lease.procs_fd)
    def failure(): raise rl.ResourceBlocked("cannot kill cgroup")
    lease.kill = failure
    original = iso.os.killpg
    def killpg(pid, sig):
        killed.append(pid)
        return original(pid, sig)
    monkeypatch.setattr(iso.os, "killpg", killpg)
    try:
        with pytest.raises(rl.ResourceBlocked, match="cgroup kill failed"):
            iso._capture([sys.executable, "-c", "import time; time.sleep(10)"], [], sys.executable,
                         SimpleNamespace(wall_seconds=0.05, output_bytes=1024), lease)
        assert killed
    finally:
        os.close(lease.procs_fd)


def test_supervisor_cannot_move_itself_into_worker_leaf():
    lease = rl.CgroupLease(rl.WorkerLimits.from_dict(limits()))
    lease.procs_fd = -1
    with pytest.raises(rl.ResourceBlocked, match="direct child"):
        lease.attach_before_exec()
