"""Launch-boundary regressions; these tests never start Chrome."""
import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
from tools.sandboxed_chrome import (
    FORBIDDEN_FLAGS, SandboxedChrome, SandboxError, verify_sandbox_evidence,
)


class LauncherRegressionTests(unittest.TestCase):
    def test_workbench_does_not_delegate_chrome_launch_to_agent_browser(self):
        source = (ROOT / 'tools/check_workbench_ui.py').read_text()
        tree = ast.parse(source)
        unsafe = [node for node in ast.walk(tree) if isinstance(node, ast.List)
                  and any(isinstance(item, ast.Constant)
                          and item.value == '--executable-path' for item in node.elts)]
        self.assertEqual(unsafe, [], 'agent-browser local launch injects --no-sandbox when CI exists')
        self.assertIn('SandboxedChrome(', source)


class AttachBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='keel-helper-test-')
        self.out = Path(self.directory.name)
        self.runner = SandboxedChrome('/bin/true', '/bin/true', self.out)
        self.runner._prepare()
        self.endpoint = 'ws://127.0.0.1:9222/devtools/browser/synthetic'
        self.runner._attach_options(self.endpoint)
        self.runner._active = True

    def tearDown(self):
        self.runner.close()
        self.directory.cleanup()

    def test_ci_presence_and_value_preserved_and_inherited_agent_modes_removed(self):
        for ci in ('', 'false', 'true'):
            with self.subTest(ci=ci), patch.dict(os.environ, {
                'CI': ci, 'AGENT_BROWSER_PROVIDER': 'foreign-provider',
                'AGENT_BROWSER_CDP': 'ws://foreign', 'AGENT_BROWSER_DAEMON': '1',
            }):
                runner = SandboxedChrome('/bin/true', '/bin/true', self.out)
                runner._prepare()
                runner._attach_options(self.endpoint)
                try:
                    self.assertEqual(runner.env['CI'], ci)
                    self.assertEqual(runner.env['AGENT_BROWSER_CDP'], self.endpoint)
                    self.assertNotIn('AGENT_BROWSER_PROVIDER', runner.env)
                    self.assertNotIn('AGENT_BROWSER_DAEMON', runner.env)
                    self.assertNotEqual(runner.socket_dir, self.runner.socket_dir)
                    self.assertEqual(runner.config.read_text(), '{}\n')
                finally:
                    runner.close()

    def test_every_call_retains_cdp_and_denies_local_executable(self):
        response = subprocess.CompletedProcess([], 0, '{"success":true,"data":{"snapshot":"ok"}}', '')
        with patch('tools.sandboxed_chrome.subprocess.run', return_value=response) as run:
            self.runner.call('snapshot', '-i')
            command = run.call_args.args[0]
            environment = run.call_args.kwargs['env']
            self.assertEqual(command[command.index('--cdp') + 1], self.endpoint)
            self.assertEqual(environment['AGENT_BROWSER_CDP'], self.endpoint)
            self.assertEqual(command[command.index('--executable-path') + 1], str(self.runner.sentinel))
            self.assertFalse(self.runner.sentinel.exists())
            self.assertEqual(environment['AGENT_BROWSER_EXECUTABLE_PATH'], str(self.runner.sentinel))

    def test_failed_attach_never_retries_or_launches_browser(self):
        response = subprocess.CompletedProcess([], 1, '', 'stale CDP endpoint')
        with patch('tools.sandboxed_chrome.subprocess.run', return_value=response) as run, \
                patch('tools.sandboxed_chrome.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(SandboxError, 'no fallback'):
                self.runner.call('snapshot', '-i')
            run.assert_called_once()
            spawn.assert_not_called()
            self.assertEqual(run.call_args.kwargs['env']['AGENT_BROWSER_CDP'], self.endpoint)

    def test_global_options_cannot_hide_in_positionals(self):
        cases = [(), ('open',), ('launch',), ('connect', '9223'), ('batch', 'open'),
                 ('open', 'http://127.0.0.1/', '--cdp', '9223'),
                 ('click', '--headed'), ('fill', '#name', '--executable-path'),
                 ('click', '--restore=foreign'), ('wait', '--fn', '--config'),
                 ('network', 'route', '**', '--body', 'x'),
                 ('open', 'https://example.com'), ('open', 'file:///etc/passwd'),
                 ('screenshot', '/tmp/outside-evidence.png'),
                 ('download', '#download', '/tmp/outside-evidence.json')]
        with patch('tools.sandboxed_chrome.subprocess.run') as run:
            for args in cases:
                with self.subTest(args=args), self.assertRaises(SandboxError):
                    self.runner.call(*args)
            run.assert_not_called()

    def test_only_expected_shapes_are_accepted(self):
        for args in [('open', 'http://127.0.0.1:8080/#token=synthetic'),
                     ('open', (self.out / 'fixture.html').as_uri()),
                     ('wait', '--fn', 'document.readyState === "complete"'),
                     ('snapshot', '-i'), ('set', 'viewport', '1024', '768'),
                     ('network', 'requests'), ('download', '#export', str(self.out / 'export.json'))]:
            with self.subTest(args=args):
                self.runner._validate(args)

    def test_large_scripts_and_global_flag_text_go_only_to_stdin(self):
        script = '/* --cdp --no-sandbox */' + 'x' * 600000
        response = subprocess.CompletedProcess([], 0, '{"success":true,"data":{"result":true}}', '')
        with patch('tools.sandboxed_chrome.subprocess.run', return_value=response) as run:
            self.assertTrue(self.runner.evaluate(script))
            self.assertEqual(run.call_args.args[0][-2:], ['eval', '--stdin'])
            self.assertEqual(run.call_args.kwargs['input'], script)
            self.assertNotIn(script, run.call_args.args[0])

    def test_reconnect_configuration_changes_fail_before_any_subprocess(self):
        self.runner.env['AGENT_BROWSER_CDP'] = 'ws://foreign'
        with patch('tools.sandboxed_chrome.subprocess.run') as run:
            with self.assertRaisesRegex(SandboxError, 'CDP configuration changed'):
                self.runner.call('snapshot')
            run.assert_not_called()

    def test_command_and_other_daemon_environment_cannot_change(self):
        for target in ('command', 'environment'):
            with self.subTest(target=target):
                original_command, original_env = list(self.runner.command), dict(self.runner.env)
                if target == 'command':
                    self.runner.command.remove('--cdp')
                else:
                    self.runner.env['AGENT_BROWSER_EXECUTABLE_PATH'] = '/bin/true'
                with patch('tools.sandboxed_chrome.subprocess.run') as run:
                    with self.assertRaisesRegex(SandboxError, 'Fixed attach'):
                        self.runner.call('snapshot')
                    run.assert_not_called()
                self.runner.command, self.runner.env = original_command, original_env

    def test_download_cannot_reuse_existing_file(self):
        output = self.out / 'export.json'
        output.write_text('old result')
        with self.assertRaisesRegex(SandboxError, 'must be new'):
            self.runner._validate(('download', '#export', str(output)))

    def test_init_script_bytes_are_fixed_before_every_command(self):
        script = self.out / 'hook.js'
        script.write_text('window.synthetic = true;')
        runner = SandboxedChrome('/bin/true', '/bin/true', self.out, init_scripts=(script,))
        runner._prepare()
        runner._attach_options(self.endpoint)
        runner._active = True
        script.write_text('window.synthetic = false;')
        try:
            with patch('tools.sandboxed_chrome.subprocess.run') as run:
                with self.assertRaisesRegex(SandboxError, 'init script changed'):
                    runner.call('snapshot')
                run.assert_not_called()
        finally:
            runner.close()

    def test_dangling_sentinel_symlink_fails_before_subprocess(self):
        self.runner.sentinel.symlink_to(self.out / 'missing')
        with patch('tools.sandboxed_chrome.subprocess.run') as run:
            with self.assertRaisesRegex(SandboxError, 'launch-denial configuration'):
                self.runner.call('snapshot')
            run.assert_not_called()

    def test_output_symlink_cannot_escape_evidence_directory(self):
        (self.out / 'escape').symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(SandboxError):
            self.runner._validate(('screenshot', str(self.out / 'escape' / 'outside.png')))

    def test_failed_normal_chrome_launch_has_no_alternate_launch(self):
        runner = SandboxedChrome('/bin/true', '/bin/true', self.out)
        process = SimpleNamespace(pid=987654321, poll=lambda: 1)
        version = subprocess.CompletedProcess([], 0, 'agent-browser 0.38.2\n', '')
        with patch('tools.sandboxed_chrome.os.geteuid', return_value=1000), \
                patch('tools.sandboxed_chrome.subprocess.run', return_value=version) as run, \
                patch('tools.sandboxed_chrome.subprocess.Popen', return_value=process) as spawn:
            with self.assertRaisesRegex(SandboxError, 'Normal-sandbox Chrome exited'):
                runner.__enter__()
            spawn.assert_called_once()
            run.assert_called_once()  # Version check only, never agent launch.
            flags = {arg.split('=', 1)[0] for arg in spawn.call_args.args[0]}
            self.assertFalse(flags & FORBIDDEN_FLAGS)
            self.assertFalse(runner.metadata['sandbox_verified'])

    def test_acceptance_calls_require_runtime_sandbox_proof(self):
        self.runner._active = False
        with patch('tools.sandboxed_chrome.subprocess.run') as run:
            with self.assertRaises(SandboxError):
                self.runner.call('open', 'http://127.0.0.1/')
            run.assert_not_called()

    def test_stale_probe_wire_is_fixed_and_socket_peer_verified(self):
        response = {'id': 'keel-stale-cdp-proof', 'success': False,
                    'error': 'CDP connection failed: CDP WebSocket connect failed: refused'}
        client = MagicMock()
        client.getsockopt.return_value = struct.pack('3i', 12345, os.geteuid(), os.getegid())
        client.recv.return_value = json.dumps(response).encode() + b'\n'
        sock = MagicMock()
        sock.__enter__.return_value = client
        info = SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.geteuid())
        with patch('tools.sandboxed_chrome.socket.socket', return_value=sock), \
                patch('tools.sandboxed_chrome.Path.lstat', return_value=info):
            self.assertEqual(self.runner._fixed_stale_snapshot({'pid': 12345}), response)
            client.sendall.assert_called_once_with(b'{"id":"keel-stale-cdp-proof","action":"snapshot"}\n')
            client.sendall.reset_mock()
            with self.assertRaisesRegex(SandboxError, 'socket peer'):
                self.runner._fixed_stale_snapshot({'pid': 67890})
            client.sendall.assert_not_called()

    def test_disconnect_proof_requires_real_sandbox_precondition(self):
        with patch.object(self.runner, '_terminate_chrome') as terminate:
            with self.assertRaisesRegex(SandboxError, 'verified live sandbox'):
                self.runner.prove_disconnect_fails_closed()
            terminate.assert_not_called()

    def test_disconnect_proof_exercises_both_daemon_recovery_and_cli_preamble(self):
        self.runner.metadata['sandbox_verified'] = True
        response = {'id': 'keel-stale-cdp-proof', 'success': False,
                    'error': 'CDP connection failed: CDP WebSocket connect failed: refused'}
        daemon = {'pid': 12345, 'starttime': '100', 'retained_environment': {'cdp': self.endpoint}}
        calls = []
        def call(*args):
            calls.append(args)
            if len(calls) == 1:
                return {}
            self.runner._last_result = subprocess.CompletedProcess([], 1, json.dumps({
                'success': False, 'error': 'CDP WebSocket connect failed: refused'}), '')
            raise SandboxError('Attached browser command failed: snapshot')
        with patch.object(self.runner, 'process', SimpleNamespace(pid=987654321)), \
                patch.object(self.runner, 'call', side_effect=call) as mocked_call, \
                patch.object(self.runner, '_daemon_evidence', return_value=daemon), \
                patch.object(self.runner, '_terminate_chrome') as terminate, \
                patch.object(self.runner, '_chrome_group_members', return_value=[]), \
                patch.object(self.runner, '_prior_chrome_states', return_value=[{'state': 'Z', 'owned_live': False}]), \
                patch.object(self.runner, '_fixed_stale_snapshot', return_value=response) as probe, \
                patch('tools.sandboxed_chrome._process_tree', side_effect=[[{'pid': 987654321}], [{'pid': 12345}]]):
            proof = self.runner.prove_disconnect_fails_closed()
            self.assertTrue(proof['passed'])
            self.assertTrue(proof['cli_attach_failed'])
            self.assertFalse(self.runner._active)
            self.assertEqual(mocked_call.call_count, 2)
            terminate.assert_called_once()
            probe.assert_called_once_with(daemon)

    def test_sentinel_failure_cannot_pass_as_retained_cdp_recovery(self):
        self.runner.metadata['sandbox_verified'] = True
        response = {'id': 'keel-stale-cdp-proof', 'success': False,
                    'error': 'Auto-launch failed: LOCAL_BROWSER_LAUNCH_FORBIDDEN not found'}
        with patch.object(self.runner, 'process', SimpleNamespace(pid=987654321)), \
                patch.object(self.runner, 'call', return_value={}), \
                patch.object(self.runner, '_daemon_evidence', return_value={'pid': 12345}), \
                patch.object(self.runner, '_terminate_chrome'), \
                patch.object(self.runner, '_chrome_group_members', return_value=[]), \
                patch.object(self.runner, '_prior_chrome_states', return_value=[]), \
                patch.object(self.runner, '_fixed_stale_snapshot', return_value=response), \
                patch('tools.sandboxed_chrome._process_tree', return_value=[]):
            with self.assertRaisesRegex(SandboxError, 'retained-CDP reconnection'):
                self.runner.prove_disconnect_fails_closed()
            self.assertFalse(self.runner.metadata['disconnect_proof']['passed'])


class SandboxEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.diagnostic = {'rows': [['Seccomp-BPF sandbox', 'Yes'], ['PID namespaces', 'Yes']]}
        self.processes = [
            {'pid': 100, 'args': ['chrome'], 'namespaces': {'pid': 'pid:[1]'},
             'seccomp': '2', 'no_new_privs': '1'},
            {'pid': 101, 'args': ['chrome', '--type=renderer'], 'namespaces': {'pid': 'pid:[2]'},
             'seccomp': '2', 'no_new_privs': '1'},
        ]

    def test_combined_browser_and_kernel_evidence(self):
        self.assertTrue(verify_sandbox_evidence(self.diagnostic, self.processes)['verified'])

    def test_inherited_seccomp_alone_does_not_prove_chrome_sandbox(self):
        self.processes[1]['namespaces']['pid'] = 'pid:[1]'
        with self.assertRaisesRegex(SandboxError, 'PID namespace'):
            verify_sandbox_evidence(self.diagnostic, self.processes)

    def test_missing_or_negative_browser_diagnostics_fail_closed(self):
        for diagnostic in ({}, {'rows': [['Seccomp-BPF sandbox', 'No'], ['PID namespaces', 'Yes']]}):
            with self.subTest(diagnostic=diagnostic), self.assertRaises(SandboxError):
                verify_sandbox_evidence(diagnostic, self.processes)

    def test_missing_renderer_unsafe_flags_and_kernel_failures(self):
        for change in ('missing', 'flags', 'seccomp', 'no_new_privs'):
            processes = copy.deepcopy(self.processes)
            if change == 'missing':
                processes.pop()
            elif change == 'flags':
                processes[1]['args'].append('--no-sandbox')
            else:
                processes[1][change] = '0'
            with self.subTest(change=change), self.assertRaises(SandboxError):
                verify_sandbox_evidence(self.diagnostic, processes)


if __name__ == '__main__':
    unittest.main()
