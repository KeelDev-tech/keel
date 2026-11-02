"""Stdlib qualification of the actual source archive, before installing deps.

Run with unittest, or use ``python -S -B -m tests.test_connector_package
--evidence-dir NEW_DIRECTORY`` to retain the archive and fresh JSON evidence.
Historical qualification remains historical; its five source bindings are
compared with the current manifest without qualifying the current artifact.
Runtime instrumentation is a regression guard, not an OS security sandbox.
"""
from collections import Counter
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import webbrowser
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
from tools import package

MAX_EVIDENCE_BYTES = 512 * 1024
EVIDENCE_DIRECTORY = None
HISTORICAL_EVIDENCE_PATH = "keel_connector/evidence/qualification.json"
HISTORICAL_EVIDENCE_SHA256 = "d4472f9412d1d065fd84992b3d18df4546260b94296a1f52c4233b9eee90741d"
HISTORICAL_BINDING_PATHS = frozenset({"keel_connector/__init__.py", "keel_connector/adapter.py",
                                    "keel_connector/demo.py", "keel_connector/synthetic.py",
                                    "tests/test_connector_readiness.py"})
OPTIONAL_AUTH_PATHS = frozenset({"keel_connector/access_tokens.py", "keel_connector/request_boundary.py",
                               "tests/test_connector_access_tokens.py", "tests/test_connector_request_boundary.py",
                               "requirements-connector-auth.txt", "docs/CONNECTOR_AUTH_BOUNDARY.md"})


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _historical_comparison(manifest, raw):
    """Pin preserved evidence and disclose drift; do not reuse its old verdict."""
    if _sha(raw) != HISTORICAL_EVIDENCE_SHA256:
        raise AssertionError("historical qualification evidence changed")
    bindings = json.loads(raw).get("source_binding")
    if type(bindings) is not dict or set(bindings) != HISTORICAL_BINDING_PATHS:
        raise AssertionError("historical qualification must retain its exact five source paths")
    comparison = {}
    for path, historical_digest in sorted(bindings.items()):
        current_digest = manifest["files"][path]["sha256"]
        if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
               for value in (historical_digest, current_digest)):
            raise AssertionError("source bindings require valid SHA-256 values")
        comparison[path] = {"historical_sha256": historical_digest,
                            "current_manifest_sha256": current_digest,
                            "matches": historical_digest == current_digest}
    return {"evidence_path": HISTORICAL_EVIDENCE_PATH, "evidence_sha256": HISTORICAL_EVIDENCE_SHA256,
            "qualifies_current_artifact": False, "source_comparison": comparison,
            "matched_paths": [path for path, row in comparison.items() if row["matches"]],
            "changed_paths": [path for path, row in comparison.items() if not row["matches"]],
            "basis": "Preserved historical measurements only; current qualification requires fresh tests and the complete current manifest."}


class _EffectDenied(RuntimeError):
    pass


class _GuardViolation(AssertionError):
    pass


class _EvaluationGuard:
    """Sticky counters survive errors caught by the adapter under test."""
    MUTATIONS = frozenset({"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.link", "os.symlink",
                           "os.chmod", "os.chown", "os.utime", "os.truncate", "os.chdir", "os.fchdir"})
    PROCESSES = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork",
                           "os.forkpty", "pty.spawn"})

    def __init__(self):
        self.active = 0
        self.phase = "qualification"
        self.violations = {"qualification": Counter(), "self_test": Counter()}
        self.calls = {"qualification": Counter(), "self_test": Counter()}
        self.stack = ExitStack()

    def deny(self, category):
        self.violations[self.phase][category] += 1
        raise _EffectDenied("forbidden evaluation effect")

    def audit(self, event, arguments):
        if not self.active:
            return
        if event == "open":
            _, mode, flags = arguments
            if ((isinstance(mode, str) and any(value in mode for value in "wax+"))
                    or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))):
                self.deny("file_write")
        elif event in self.MUTATIONS or event.startswith("shutil."):
            self.deny("filesystem_mutation")
        elif event.startswith("socket."):
            self.deny("network")
        elif event in self.PROCESSES:
            self.deny("process")

    def protect(self, owner, name, category):
        original = getattr(owner, name)

        def guarded(*args, **kwargs):
            if self.active:
                self.deny(category)
            return original(*args, **kwargs)
        self.stack.enter_context(patch.object(owner, name, guarded))

    def __enter__(self):
        from keel_connector.adapter import ReadinessAdapter
        from keel_agent import browser, models
        from keel_live.review import ReviewService
        from keel_workbench.service import Workbench
        import threading

        for name in ("write", "writev", "pwrite", "pwritev", "ftruncate", "sendfile"):
            if hasattr(os, name):
                self.protect(os, name, "file_write")
        for name in ("send", "sendall", "sendfile", "sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                self.protect(socket.socket, name, "network")
        self.protect(threading.Thread, "start", "process")
        for name in ("open", "open_new", "open_new_tab"):
            self.protect(webbrowser, name, "browser")
        for name in ("run", "prepare", "submit_local"):
            self.protect(browser.BrowserAdapter, name, "browser")
        for name in ("run_blind_review", "loopback_transport", "_loopback_transport_raw"):
            self.protect(models, name, "model")
        self.protect(Workbench, "run", "workbench_run")
        self.protect(ReviewService, "inspect", "review_inspect")
        original = ReadinessAdapter.call

        def checked(adapter, operation, arguments=b"{}"):
            bucket = self.violations[self.phase]
            before = sum(bucket.values())
            label = operation if type(operation) is str and operation in {
                "list_application_blockers", "get_application_readiness"} else "invalid_request"
            self.calls[self.phase][label] += 1
            self.active += 1
            try:
                return original(adapter, operation, arguments)
            finally:
                self.active -= 1
                if sum(bucket.values()) != before:
                    raise _GuardViolation("sticky evaluation guard detected a forbidden effect") from None

        self.stack.enter_context(patch.object(ReadinessAdapter, "call", checked))
        sys.addaudithook(self.audit)
        return self

    def __exit__(self, *args):
        self.active = 0
        return self.stack.__exit__(*args)


def _self_test_guard(guard, directory):
    """Every denial happens before the attempted operation, inside real call()."""
    from keel_connector.demo import _adapter
    from keel_connector.synthetic import make_workspace
    from keel_agent import browser, models
    from keel_live.review import ReviewService
    from keel_workbench.service import Workbench

    root = directory / "guard-probe"
    document = make_workspace(root)
    adapter = _adapter(document, root)
    descriptor = os.open(root / "preopened-probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    connection = socket.socket()  # Unconnected: no packets even if a probe fails.
    probes = {
        "file_write": lambda: (root / "forbidden-write").write_bytes(b"synthetic"),
        "preopened_write": lambda: os.write(descriptor, b"synthetic"),
        "filesystem_mutation": lambda: (root / "forbidden-directory").mkdir(),
        "network": lambda: socket.getaddrinfo("synthetic.invalid", 443),
        "preopened_socket_send": lambda: connection.send(b"synthetic"),
        "preopened_socket_sendall": lambda: connection.sendall(b"synthetic"),
        "preopened_socket_sendfile": lambda: connection.sendfile(io.BytesIO(b"synthetic")),
        "process": lambda: subprocess.run([sys.executable, "-c", "raise SystemExit(91)"], check=True),
        "browser": lambda: webbrowser.open("https://synthetic.invalid"),
        "browser_adapter": lambda: browser.BrowserAdapter.run(None, {}),
        "model": lambda: models.run_blind_review(None),
        "workbench_run": lambda: Workbench.run(None, {}),
        "review_inspect": lambda: ReviewService.inspect(None, None, {}),
    }
    observed = []
    guard.phase = "self_test"
    try:
        for name, probe in probes.items():
            adapter.snapshot_provider = probe
            before = sum(guard.violations["self_test"].values())
            try:
                adapter.call("list_application_blockers")
            except _GuardViolation:
                observed.append(name)
            else:
                raise AssertionError("guard failed its synthetic denial probe")
            if sum(guard.violations["self_test"].values()) != before + 1:
                raise AssertionError("guard did not retain exactly one denied effect")
    finally:
        guard.phase = "qualification"
        os.close(descriptor)
        connection.close()
    if (root / "forbidden-write").exists() or (root / "forbidden-directory").exists() or (root / "preopened-probe").stat().st_size:
        raise AssertionError("guard probe caused a filesystem effect")
    return observed


def isolated_probe(root, artifact_sha256, source_inventory_sha256):
    """Executed only by the -I -S -B child, from the verified extraction."""
    root = Path(root).resolve()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise AssertionError("isolated stdlib-only interpreter required")
    if any("site-packages" in value or "dist-packages" in value for value in sys.path):
        raise AssertionError("external dependency search path present")
    fresh = {name: Path(os.environ[name]) for name in ("HOME", "KEEL_HOME", "TMPDIR")}
    if any(not path.is_dir() or any(path.iterdir()) for path in fresh.values()):
        raise AssertionError("fresh empty host directories required")
    manifest = json.loads((root / "MANIFEST.json").read_bytes())
    if _sha(_json(manifest["files"])) != source_inventory_sha256:
        raise AssertionError("current source inventory digest differs from the shipped manifest")
    if OPTIONAL_AUTH_PATHS.intersection(manifest["files"]):
        raise AssertionError("optional authentication paths must remain outside the source archive")
    historical = _historical_comparison(manifest, (root / HISTORICAL_EVIDENCE_PATH).read_bytes())
    from keel_connector.demo import run_demo

    with _EvaluationGuard() as guard:
        with tempfile.TemporaryDirectory(prefix="guard-self-test-") as temporary:
            probes = _self_test_guard(guard, Path(temporary))
        namespace = runpy.run_path(str(root / "tests/test_connector_readiness.py"), run_name="packaged_readiness_tests")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["ConnectorReadinessTests"])
        if suite.countTestCases() != 37:
            raise AssertionError("expected all 37 connector readiness tests")
        output = io.StringIO()
        tests = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
        if not tests.wasSuccessful() or tests.testsRun != 37 or tests.skipped:
            raise AssertionError("packaged readiness tests failed: " + output.getvalue()[-4000:])
        demo = run_demo(benchmark=True, samples=21)
        if not demo["passed"] or len(demo["scenarios"]) != 9:
            raise AssertionError("packaged synthetic demonstration failed")
        benchmark = demo["benchmark"]
        expected = {(count, profile) for count in (20, 30) for profile in ("complete", "missing_sources")}
        if ({(row["roles"], row["profile"]) for row in benchmark["measurements"]} != expected
                or any(row["samples"] != 21 or len(row["sample_ms"]) != 21 or row["p95_ms"] > 2000
                       or row["output_max_bytes"] > 256 * 1024 for row in benchmark["measurements"])
                or benchmark["headroom_ratio"] != 1.5 or not benchmark["overload"]["passed"]
                or benchmark["overload"]["roles"] != 31
                or benchmark["overload"]["report"]["error"]["code"] != "WORKLOAD_EXCEEDED"):
            raise AssertionError("packaged capacity contract failed")
        if guard.violations["qualification"]:
            raise AssertionError("qualification had forbidden evaluation effects")
        if any(not guard.calls["qualification"][name] for name in ("list_application_blockers", "get_application_readiness")):
            raise AssertionError("both packaged adapter operations must execute")
        counters = {"qualification_calls": dict(guard.calls["qualification"]),
                    "qualification_violations": dict(guard.violations["qualification"]),
                    "self_test_denials": dict(guard.violations["self_test"]), "self_test_probes": probes,
                    "coverage": {"scope": "Each actual ReadinessAdapter.call, including host callbacks.",
                                 "audit": ["write-capable open", "filesystem mutations", "socket events", "process creation"],
                                 "explicit": ["os descriptor writes/truncation/sendfile", "socket send/sendall/sendfile/sendto/sendmsg",
                                              "thread start", "known model and browser entry points", "Workbench.run", "ReviewService.inspect"],
                                 "limitations": ["Regression instrumentation, not an OS security sandbox.",
                                                 "Arbitrary native code and writes through pre-opened Python file objects are not universally intercepted.",
                                                 "These synthetic cases pass no held writable streams; canonical data and complete source/file inventories are checked separately."]}}
    imported = {}
    stdlib_root = Path(os.__file__).resolve().parent
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue  # Built-in and frozen modules need no filesystem source.
        location = Path(origin).resolve()
        if any(part in {"site-packages", "dist-packages"} for part in location.parts):
            raise AssertionError("module imported from an external dependency directory")
        if location.is_relative_to(root):
            imported[name] = location.relative_to(root).as_posix()
        elif not location.is_relative_to(stdlib_root):
            raise AssertionError("module imported outside extraction and interpreter stdlib")
    if "keel_connector.adapter" not in imported or "keel_live.proof" not in imported:
        raise AssertionError("actual packaged qualification path was not imported")
    if list(root.rglob("__pycache__")):
        raise AssertionError("isolated child wrote bytecode into extracted source")
    report = {"schema": "keel.connector.packaged_qualification.v1", "passed": True, "synthetic": True,
              "artifact_sha256": artifact_sha256, "source_inventory_sha256": source_inventory_sha256,
              "historical_qualification": historical,
              "optional_auth_paths_excluded": sorted(OPTIONAL_AUTH_PATHS),
              "toolchain": {"python": sys.version.split()[0], "zlib_build": zlib.ZLIB_VERSION,
                            "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
                            "reproducibility_scope": "Identical source with the same Python/zlib toolchain only."},
              "isolation": {"isolated": True, "no_site": True, "no_bytecode": True,
                            "fresh_home": True, "fresh_keel_home": True, "fresh_tmpdir": True,
                            "project_imports_from_extraction": True, "project_modules": len(imported)},
              "readiness_tests": {"run": tests.testsRun, "failures": len(tests.failures),
                                  "errors": len(tests.errors), "skipped": len(tests.skipped)},
              "guard": counters, "demo": demo, "execution_authorized": False,
              "publication_authorized": False}
    raw = _json(report)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise AssertionError("qualification evidence exceeds bounded output")
    print(raw.decode())


class ConnectorPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="keel-connector-package-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.archive = cls.directory / "keel-source.zip"
        cls.second = cls.directory / "keel-source-repeat.zip"
        cls.first_build = package.build(cls.archive, root=ROOT)
        cls.second_build = package.build(cls.second, root=ROOT)
        with zipfile.ZipFile(cls.archive) as archive:
            cls.manifest = json.loads(archive.read("MANIFEST.json"))
            cls.historical = _historical_comparison(cls.manifest, archive.read(HISTORICAL_EVIDENCE_PATH))
        cls.source_digest = _sha(_json(cls.manifest["files"]))

    def test_real_build_is_reproducible_and_manifest_covers_inventory(self):
        self.assertEqual(self.archive.read_bytes(), self.second.read_bytes())
        self.assertEqual(self.first_build["sha256"], self.second_build["sha256"])
        self.assertFalse(self.first_build["publication_authorized"])
        self.assertIs(self.manifest["publication_authorized"], False)
        self.assertTrue(package.verify(self.archive)["verified_integrity"])
        names = json.loads((ROOT / "release-files.json").read_text())
        for path in sorted(OPTIONAL_AUTH_PATHS):
            with self.subTest(excluded_optional_auth_path=path):
                self.assertNotIn(path, names)
        payload = package.payload(root=ROOT)
        self.assertEqual(set(names), set(payload))
        self.assertEqual(set(names), set(self.manifest["files"]))
        required = {"keel_connector/adapter.py", "keel_connector/demo.py", "keel_connector/synthetic.py",
                    "keel_connector/__init__.py", "tests/test_connector_readiness.py", "tests/test_connector_package.py",
                    "keel_connector/evidence/qualification.json", "keel_live/proof.py", "keel_live/surface.py",
                    "LICENSE", "maintenance_workbench/LICENSE", "maintenance_workbench/NOTICE",
                    "docs/MUSE_CONNECTOR.md", "docs/RELEASE_PROFILE.md"}
        self.assertTrue(required <= set(names), "connector source closure missing from real release allowlist")
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(archive.namelist(), sorted(names + ["MANIFEST.json"]))
            for item in archive.infolist():
                self.assertEqual(item.date_time, (2020, 1, 1, 0, 0, 0))
                self.assertEqual(stat.S_IFMT(item.external_attr >> 16), stat.S_IFREG)
            historical = _historical_comparison(self.manifest, archive.read(HISTORICAL_EVIDENCE_PATH))
            self.assertIs(historical["qualifies_current_artifact"], False)
            self.assertEqual(set(historical["matched_paths"] + historical["changed_paths"]), HISTORICAL_BINDING_PATHS)
            for name, body in payload.items():
                self.assertEqual(archive.read(name), body)
                self.assertEqual(self.manifest["files"][name], {"bytes": len(body), "sha256": _sha(body)})

    def test_tampered_payload_and_unsafe_members_are_rejected(self):
        def valid_manifest(name, body):
            return _json({"schema_version": 1, "version": "synthetic-archive-test",
                          "publication_authorized": False,
                          "files": {name: {"bytes": len(body), "sha256": _sha(body)}}})

        with zipfile.ZipFile(self.archive) as original:
            entries = [(item, original.read(item.filename)) for item in original.infolist()]
        tampered = self.directory / "tampered.zip"
        with zipfile.ZipFile(tampered, "w") as archive:
            for item, body in entries:
                archive.writestr(item, body + b"\n# synthetic tamper\n" if item.filename == "keel_connector/adapter.py" else body)
        with self.assertRaisesRegex(ValueError, "manifest mismatch"):
            package.verify(tampered)
        for index, name in enumerate(("../escape", "/absolute", "back\\slash")):
            with self.subTest(member=name):
                invalid = self.directory / ("unsafe-" + str(index) + ".zip")
                with zipfile.ZipFile(invalid, "w") as archive:
                    archive.writestr("MANIFEST.json", valid_manifest(name, b"synthetic"))
                    archive.writestr(name, b"synthetic")
                with self.assertRaisesRegex(ValueError, "^unsafe source path$"):
                    package.verify(invalid)
        duplicate = self.directory / "duplicate.zip"
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("MANIFEST.json", valid_manifest("payload.txt", b"synthetic"))
            archive.writestr("payload.txt", b"synthetic")
            with self.assertWarnsRegex(UserWarning, "Duplicate name"):
                archive.writestr("payload.txt", b"synthetic")
        with self.assertRaisesRegex(ValueError, "^duplicate or case-conflicting source path$"):
            package.verify(duplicate)
        symlink = self.directory / "symlink.zip"
        with zipfile.ZipFile(symlink, "w") as archive:
            archive.writestr("MANIFEST.json", valid_manifest("link", b"synthetic-target"))
            item = zipfile.ZipInfo("link")
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, b"synthetic-target")
        with self.assertRaisesRegex(ValueError, "non-regular"):
            package.verify(symlink)

    def test_extracted_source_qualifies_without_site_packages_or_checkout(self):
        package.verify(self.archive)
        extracted = self.directory / "extracted"
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(extracted)
        directories = {name: self.directory / name.lower() for name in ("HOME", "KEEL_HOME", "TMPDIR")}
        for directory in directories.values():
            directory.mkdir()
        unrelated = self.directory / "unrelated-cwd"
        unrelated.mkdir()
        bootstrap = ("import runpy,sys; from pathlib import Path; "
                     "root=Path(sys.argv[1]); sys.path.insert(0,str(root)); "
                     "runpy.run_path(str(root/'tests/test_connector_package.py'),run_name='packaged_probe')"
                     "['isolated_probe'](root,sys.argv[2],sys.argv[3])")
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", bootstrap, str(extracted),
                                 self.first_build["sha256"], self.source_digest], cwd=unrelated,
                                env={**{name: str(path) for name, path in directories.items()}, "LC_ALL": "C.UTF-8"},
                                capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, (result.stderr + result.stdout)[-6000:].decode(errors="replace"))
        self.assertLessEqual(len(result.stdout), MAX_EVIDENCE_BYTES + 1)
        report = json.loads(result.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual(report["artifact_sha256"], self.first_build["sha256"])
        self.assertEqual(report["source_inventory_sha256"], self.source_digest)
        self.assertEqual(report["historical_qualification"], self.historical)
        self.assertEqual(report["optional_auth_paths_excluded"], sorted(OPTIONAL_AUTH_PATHS))
        with zipfile.ZipFile(self.archive) as archive:
            expected_files = {item.filename: archive.read(item) for item in archive.infolist()}
        expected_directories = {str(parent) for name in expected_files for parent in Path(name).parents
                                if str(parent) != "."}
        observed_files, observed_directories = {}, set()
        for path in extracted.rglob("*"):
            self.assertFalse(path.is_symlink(), "child created a source symlink")
            relative = path.relative_to(extracted).as_posix()
            if path.is_dir():
                observed_directories.add(relative)
            else:
                self.assertTrue(path.is_file(), "child created non-regular source material")
                observed_files[relative] = path.read_bytes()
        self.assertEqual(observed_files, expected_files, "extracted source differs from verified archive")
        self.assertEqual(observed_directories, expected_directories, "child created extra source directories")
        for directory in (*directories.values(), unrelated):
            self.assertEqual(list(directory.iterdir()), [], "child left state outside cleaned synthetic setup")
        report["post_run_inventory"] = {"source_files_identical": True, "source_directories_identical": True,
                                        "fresh_homes_tmpdir_and_cwd_empty": True}
        if EVIDENCE_DIRECTORY is not None:
            EVIDENCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
            for name, body in (("keel-connector-source.zip", self.archive.read_bytes()),
                               ("qualification.json", _json(report) + b"\n")):
                with (EVIDENCE_DIRECTORY / name).open("xb") as stream:
                    stream.write(body)
        summary = {key: report[key] for key in ("schema", "passed", "artifact_sha256", "source_inventory_sha256",
                                               "toolchain", "isolation", "post_run_inventory", "readiness_tests", "guard",
                                               "historical_qualification", "optional_auth_paths_excluded",
                                               "execution_authorized", "publication_authorized")}
        summary["benchmark"] = report["demo"]["benchmark"]
        summary["demo_scenarios"] = len(report["demo"]["scenarios"])
        print(_json(summary).decode())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    args, remaining = parser.parse_known_args()
    EVIDENCE_DIRECTORY = args.evidence_dir
    unittest.main(argv=[sys.argv[0], *remaining])
