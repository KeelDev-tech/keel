"""Real isolated CLI checks; synthetic state, no provider patches or browser calls.

These require the production ownership guard to admit the test directory. An
unsupported executor must fail visibly; it is not converted to skipped coverage.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "current-runtime-v1"
WORKSPACE = "runtime-source-cli-fixture"
TIMEOUT = 60
BOOTSTRAP = """import runpy, sys
source, module = sys.argv[1:3]
sys.path.insert(0, source)
sys.argv = [module, *sys.argv[3:]]
runpy.run_module(module, run_name='__main__', alter_sys=True)
"""
INVENTORY = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.runtime_source_inventory import current_runtime_provider
print(json.dumps(current_runtime_provider(Path(sys.argv[1]))(), sort_keys=True))
"""
RUNTIME_FIXTURE = """import json, sys, time
from pathlib import Path
root, home = map(Path, sys.argv[1:3])
sys.path.insert(0, str(root))
from tools.runtime_source_inventory import current_runtime_provider
from keel_loki.common import atomic_json, digest
from keel_loki.forms import bind_fixture
from keel_muse.browser import InjectedFixture
from keel_operational import qualification
from keel_operational.control import StopLedger
source = current_runtime_provider(root)()['sha256']
now = int(time.time())
inputs = qualification.fixture_inputs()
inputs['fixture'] = bind_fixture(inputs['fixture'], now=now)
inputs.update(source_sha256=source, now=now)
plan = qualification.freeze_plan(**inputs)
fixture = InjectedFixture(plan['fixture'])
def capture(request):
    observed = fixture(request)
    return {'raw': b'SYNTHETIC injected observation: ' + json.dumps(observed).encode(),
            'normalized': observed,
            'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
report = qualification.run(home / 'qualification', plan,
    expected_plan_sha256=digest(plan), capture=capture, clock=lambda: now,
    **{k: inputs[k] for k in ('manifest', 'tool_schemas', 'host_config', 'source_sha256', 'now')})
def document(name, value):
    path = home / (name + '.json')
    atomic_json(path, value)
    return str(path)
refs = {k: document(k, inputs[k]) for k in ('manifest', 'tool_schemas', 'host_config')}
refs.update(plan=document('plan', plan), expected_plan_sha256=digest(plan),
            report=document('qualification-report', report), expected_report_sha256=digest(report))
control = home / 'control'
StopLedger.create(control, 'runtime-source-cli-fixture')
config = {'schema': 'keel.operational.runtime-cli.v1',
    'workspace_id': 'runtime-source-cli-fixture', 'control_home': str(control),
    'runtime_home': str(home / 'runtime'), 'native_home': str(home / 'native'),
    'session_id': 'fixture-session', 'scope_id': 'fixture-role',
    'transport_mode': 'INJECTED', 'native_gate': None, 'lease_seconds': 30,
    'expires_at': None, 'qualification': refs,
    'application': document('application', {k: fixture.fixture[k]
        for k in ('contract', 'values', 'attachments')}),
    'host_approvals': document('approvals', fixture.fixture['approvals']),
    'host_snapshot': document('host-snapshot', fixture.host_snapshot())}
gate = {'schema': 'keel.operational.cli-gate.v1', 'workspace_id': config['workspace_id'],
    'account_id': fixture.contract['account_id'], 'role_id': config['scope_id'],
    'resource_id': fixture.contract['form_id'], 'source_sha256': source,
    'consent': True, 'no_ai': False, 'approval_current': True, 'holds': [],
    'unknown_attempt': False, 'rate_limited': False, 'issued_at': now, 'expires_at': now + 90}
config['control_gate'] = document('gate', gate)
print(document('runtime-config', config))
"""


class RuntimeSourceCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keel-runtime-source-cli-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / "source"
        self.source.mkdir(mode=0o700)
        self.serial = 0
        # The profile supplies the complete reviewed byte set; it is not reduced
        # to the imports exercised by one happy-path command.
        self.profile_names = set(json.loads((ROOT / "runtime-source-files.json").read_text())["files"])
        self.assertEqual(len(self.profile_names), 629)
        self.assertIn("tools/sandboxed_chrome.py", self.profile_names)
        names = set(self.profile_names)
        names.update((".gitignore", "muse-base-manifest.json", "muse-release-files.json",
                      "operational-base-manifest.json", "operational-release-files.json"))
        for name in sorted(names):
            original, target = ROOT / name, self.source / name
            self.assertTrue(original.is_file(), "missing reviewed source: " + name)
            self.assertFalse(original.is_symlink(), "linked reviewed source: " + name)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        self.assertFalse(any(self.source.rglob("*.pyc")))

    def command(self, module, args):
        return [sys.executable, "-I", "-S", "-B", "-c", BOOTSTRAP,
                str(self.source), module, *map(str, args)]

    def run_child(self, command):
        return subprocess.run(command, cwd=self.home, capture_output=True,
                              text=True, timeout=TIMEOUT, check=False)

    def inventory(self):
        child = self.run_child([sys.executable, "-I", "-S", "-B", "-c", INVENTORY,
                                str(self.source)])
        self.assertEqual(child.returncode, 0, child.stderr + child.stdout)
        result = json.loads(child.stdout)
        self.assertEqual(result["profile"], PROFILE)
        self.assertEqual(result["file_count"], 629)
        self.assertRegex(result["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(result["files"]), self.profile_names)
        return result

    def document(self, value):
        self.serial += 1
        path = self.home / ("input-%d.json" % self.serial)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def invoke(self, module, command, *, current=True, request=None, flags=()):
        self.serial += 1
        out = self.home / ("result-%d.json" % self.serial)
        args = [command, "--out", out, *flags]
        if current:
            args += ["--source-profile", PROFILE]
        if request is not None:
            args += ["--input", self.document(request)]
        child = self.run_child(self.command(module, args))
        report = json.loads(out.read_text()) if out.is_file() else None
        return child, report, out

    def assert_unprivileged(self, report):
        self.assertIs(report["execution_authorized"], False)
        self.assertIs(report["production_deployed"], False)
        self.assertIs(report["task_success_inferred"], False)
        self.assertEqual(report["actual_native_browser"], "NOT_RUN")

    def control_config(self, name="control"):
        home = self.home / name
        return home, self.document({"schema": "keel.operational.control-cli.v1",
            "workspace_id": WORKSPACE, "control_home": str(home)})

    def control(self, operation, config, **kwargs):
        return self.invoke("keel_operational", "control",
            request={"operation": operation, "arguments": kwargs.pop("arguments", {})},
            flags=("--host-config", config), **kwargs)

    def test_current_profile_runs_actual_inspection_without_claiming_mapping(self):
        expected = self.inventory()
        child, report, out = self.invoke("keel_muse", "inspect-host")
        self.assertEqual(child.returncode, 3, child.stderr + child.stdout)
        self.assertEqual(report["status"], "HOST_MAPPING_REQUIRED")
        self.assertEqual(report["source_sha256"], expected["sha256"])
        self.assertIs(report["source_unchanged"], True)
        self.assertIs(report["result_valid_for_source"], True)
        self.assert_unprivileged(report)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertFalse(any(self.source.rglob("*.pyc")))

    def test_default_keeps_actual_historical_baseline_failure(self):
        home, config = self.control_config()
        for module, command, request, flags in (
            ("keel_muse", "inspect-host", None, ()),
            ("keel_muse", "forms-plan", {"operation": "build_plan",
                "arguments": {"source_profile": PROFILE}}, ()),
            ("keel_operational", "control", {"operation": "create", "arguments": {}},
             ("--host-config", config)),
            ("keel_operational", "control", {"operation": "create",
                "arguments": {"source_profile": PROFILE}}, ("--host-config", config)),
        ):
            with self.subTest(module=module):
                child, report, _ = self.invoke(module, command, current=False,
                                               request=request, flags=flags)
                self.assertEqual(child.returncode, 2, child.stderr + child.stdout)
                self.assertEqual(report["status"], "ERROR")
                self.assertEqual(report["error_type"], "ValueError")
                self.assertIsNone(report["source_sha256"])
                self.assertIs(report["result_valid_for_source"], False)
                self.assert_unprivileged(report)
        self.assertFalse(home.exists())

    def test_actual_control_create_and_readonly_status_bind_current_source(self):
        pin = self.inventory()["sha256"]
        home, config = self.control_config()
        created, report, _ = self.control("create", config)
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        self.assertEqual(report["source_sha256"], pin)
        self.assert_unprivileged(report)
        database = home / "control.sqlite3"
        before = database.read_bytes(), database.stat().st_mtime_ns
        for _ in range(2):
            child, report, _ = self.control("status", config)
            self.assertEqual(child.returncode, 0, child.stderr + child.stdout)
            self.assertEqual(report["source_sha256"], pin)
            self.assertIs(report["source_unchanged"], True)
            self.assertEqual(report["result"]["canonical_delivery"], "NOT_ATTEMPTED")
            self.assert_unprivileged(report)
        self.assertEqual(before, (database.read_bytes(), database.stat().st_mtime_ns))

    def test_wrong_pin_and_request_selected_profile_cannot_create_state(self):
        self.inventory()  # Positive control: rejection must reach request validation.
        for key, value in (("expected_running_source_sha256", "0" * 64),
                           ("source_profile", PROFILE), ("source_provider", "invented"),
                           ("principal", {"authenticated": True}),
                           ("execution_authorized", True)):
            with self.subTest(key=key):
                home, config = self.control_config("rejected-" + key)
                child, report, _ = self.control("create", config, arguments={key: value})
                self.assertEqual(child.returncode, 2, child.stderr + child.stdout)
                self.assertFalse(home.exists())
                self.assertIsNotNone(report["source_sha256"])
                self.assertIs(report["result_valid_for_source"], False)
                self.assert_unprivileged(report)
                child, report, _ = self.invoke("keel_muse", "forms-plan",
                    request={"operation": "build_plan", "arguments": {key: value}})
                self.assertEqual(child.returncode, 2, child.stderr + child.stdout)
                self.assertIsNotNone(report["source_sha256"])
                self.assertIs(report["result_valid_for_source"], False)
                self.assert_unprivileged(report)

    def test_muse_bridge_uses_selected_provider_for_nested_checks(self):
        pin = self.inventory()["sha256"]
        request = {"schema": "keel.muse.bridge-request.v1", "request_id": "fixture-probe",
            "workspace_id": WORKSPACE, "operation": "capabilities.probe",
            "expected_source_sha256": pin, "expected_state_sha256": None,
            "expires_at": int(time.time()) + 300, "arguments": {}}
        child, report, _ = self.invoke("keel_muse", "bridge", request=request,
                                       flags=("--workspace-id", WORKSPACE))
        self.assertEqual(child.returncode, 3, child.stderr + child.stdout)
        bridge = report["result"]
        self.assertEqual(bridge["status"], "RECORDED")
        self.assertEqual(bridge["source_sha256"], pin)
        self.assertIs(bridge["source_unchanged"], True)
        self.assertEqual(bridge["quarantine_reasons"], [])
        self.assertIs(bridge["execution_authorized"], False)
        self.assert_unprivileged(report)
        child, rejected, _ = self.invoke("keel_muse", "bridge",
            request={**request, "expected_source_sha256": "0" * 64},
            flags=("--workspace-id", WORKSPACE))
        self.assertEqual(child.returncode, 2, child.stderr + child.stdout)
        self.assertEqual(rejected["source_sha256"], pin)
        self.assertIs(rejected["result_valid_for_source"], False)
        self.assert_unprivileged(rejected)

    def test_operational_runtime_nested_checks_use_selected_provider(self):
        self.inventory()
        fixture = self.run_child([sys.executable, "-I", "-S", "-B", "-c", RUNTIME_FIXTURE,
                                  str(self.source), str(self.home)])
        self.assertEqual(fixture.returncode, 0, fixture.stderr + fixture.stdout)
        config = Path(fixture.stdout.strip())
        for operation, expected in (("create", "READY"), ("next", "PROPOSED")):
            child, report, _ = self.invoke("keel_operational", "runtime",
                request={"operation": operation, "arguments": {}}, flags=("--host-config", config))
            self.assertEqual(child.returncode, 0, child.stderr + child.stdout)
            self.assertEqual(report["result"]["status"], expected)
            self.assertIs(report["source_unchanged"], True)
            self.assert_unprivileged(report)
        self.assertIs(report["result"]["native"]["dispatch_issued_once"], False)

    def _source_change_during_status(self, *, remove):
        home, config = self.control_config()
        created, initial, _ = self.control("create", config)
        self.assertEqual(created.returncode, 0,
                         created.stderr + created.stdout + json.dumps(initial, sort_keys=True))
        database = home / "control.sqlite3"
        request = self.document({"operation": "status", "arguments": {}})
        out = self.home / "quarantined.json"
        command = self.command("keel_operational", ["control", "--source-profile", PROFILE,
            "--host-config", config, "--input", request, "--out", out])
        # The real database lock guarantees the status operation cannot finish
        # before the mutation. Seeing this child's DB descriptor establishes
        # that its initial inventory succeeded and dispatch reached the ledger.
        lock = sqlite3.connect(database, isolation_level=None)
        child = None
        try:
            lock.execute("BEGIN EXCLUSIVE")
            child = subprocess.Popen(command, cwd=self.home, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 10
            reached = False
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    descriptors = list((Path("/proc") / str(child.pid) / "fd").iterdir())
                except FileNotFoundError:
                    break
                for fd in descriptors:
                    try:
                        reached = os.readlink(fd) == str(database)
                    except FileNotFoundError:
                        continue
                    if reached:
                        break
                if reached:
                    break
                time.sleep(0.01)
            self.assertTrue(reached, "child did not reach locked synthetic database")
            target = self.source / "keel_muse/dashboard.py"
            if remove:
                target.unlink()
            else:
                with target.open("ab") as stream:
                    stream.write(b"\n# synthetic source change during CLI operation\n")
            lock.execute("ROLLBACK")
            stdout, stderr = child.communicate(timeout=TIMEOUT)
            self.assertEqual(child.returncode, 4, stderr + stdout)
            report = json.loads(out.read_text())
            self.assertEqual(report["status"], "QUARANTINED")
            self.assertIs(report["source_unchanged"], False)
            self.assertIs(report["result_valid_for_source"], False)
            self.assert_unprivileged(report)
        finally:
            if lock.in_transaction:
                lock.rollback()
            lock.close()
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=TIMEOUT)

    def test_actual_source_mutation_during_command_is_quarantined(self):
        self._source_change_during_status(remove=False)

    def test_actual_source_disappearance_during_command_is_quarantined(self):
        self._source_change_during_status(remove=True)


if __name__ == "__main__":
    unittest.main()
