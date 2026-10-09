"""Launch-boundary regressions; these tests never start Chrome."""
import ast
import copy
from contextlib import redirect_stderr
import io
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
    FORBIDDEN_FLAGS, SandboxedChrome, SandboxError, _proc_record, _process_tree, verify_sandbox_evidence,
)
from tools.check_muse_review_ui import Acceptance, failure_diagnostic, measured_diagnostic


class MuseReviewLayoutTests(unittest.TestCase):
    """Exercise the layout acceptance gate without starting a browser."""

    def desktop_metrics(self, label='review'):
        # Observed in hosted PR138 push 330e7a4: a 15px classic scrollbar,
        # no page overflow or clipped nodes, and a fully visible tab strip.
        selected = {'id': 'tab-review' if label == 'review' else 'tab-evidence', 'issues': []}
        return {'innerWidth': 1440, 'viewport': 1425, 'pageWidth': 1425,
                'scrollbarWidth': 15, 'bad': [],
                'tabs': {'clientWidth': 745, 'scrollWidth': 745, 'scrollLeft': 0,
                         'clientHeight': 46, 'scrollHeight': 46,
                         'selected': selected, 'focused': copy.deepcopy(selected)}}

    def layout_checks(self, metrics, width=1440, label='review'):
        acceptance = Acceptance(None, Path('/synthetic-muse-layout'), {}, {}, '')
        acceptance.case = 'complete'
        with patch.object(acceptance, 'call'), \
                patch.object(acceptance, 'evaluate', return_value=metrics), \
                patch.object(acceptance, 'screenshot'):
            acceptance.layout(width, label)
        return [row['passed'] for row in acceptance.checks]

    def test_hosted_desktop_scrollbar_does_not_fail_review_or_evidence(self):
        for label in ('review', 'expanded'):
            with self.subTest(label=label):
                self.assertEqual(self.layout_checks(self.desktop_metrics(label), label=label),
                                 [True, True, True])

    def test_viewport_without_a_classic_scrollbar_remains_accepted(self):
        metrics = self.desktop_metrics()
        metrics.update(viewport=1440, pageWidth=1440, scrollbarWidth=0)
        self.assertEqual(self.layout_checks(metrics), [True, True, True])

    def test_page_overflow_into_scrollbar_space_still_fails(self):
        metrics = self.desktop_metrics()
        metrics['pageWidth'] = 1430  # Smaller than innerWidth, larger than usable width.
        self.assertEqual(self.layout_checks(metrics), [False, True, True])

    def test_wrong_window_width_or_invalid_usable_width_still_fails(self):
        for change in ({'innerWidth': 1400, 'viewport': 1385, 'pageWidth': 1385},
                       {'viewport': 0, 'pageWidth': 0},
                       {'viewport': 1441, 'pageWidth': 1441}):
            metrics = self.desktop_metrics()
            metrics.update(change)
            with self.subTest(change=change):
                self.assertFalse(self.layout_checks(metrics)[0])

    def test_text_range_issue_still_fails_with_equal_client_and_scroll_width(self):
        metrics = self.desktop_metrics()
        metrics['bad'] = [{'tag': 'P', 'class': 'item-value', 'clientWidth': 205,
                           'scrollWidth': 205, 'issues': ['text exceeds horizontal box']}]
        self.assertEqual(self.layout_checks(metrics), [False, True, True])

    def test_real_mobile_tab_overflow_and_selected_clipping_remain_failures(self):
        metrics = self.desktop_metrics('expanded')
        # Hosted 320px Evidence geometry remains outside the usable 305px width.
        selected = {'id': 'tab-evidence', 'issues': ['clipped horizontally by tabs'],
                    'rect': {'right': 313.453}}
        metrics.update(innerWidth=320, viewport=305, pageWidth=305, bad=[selected])
        metrics['tabs'].update(clientWidth=275, scrollWidth=392,
                               selected=selected, focused=copy.deepcopy(selected))
        self.assertEqual(self.layout_checks(metrics, width=320, label='expanded'),
                         [False, False, False])

    def test_tab_strip_overflow_is_an_independent_failure(self):
        metrics = self.desktop_metrics()
        metrics.update(innerWidth=390, viewport=375, pageWidth=375)
        metrics['tabs'].update(clientWidth=345, scrollWidth=392)
        self.assertEqual(self.layout_checks(metrics, width=390), [True, False, True])


class MuseReviewMeasuredEvidenceTests(unittest.TestCase):
    def keyboard_samples(self, width):
        return [{'innerWidth': width, 'viewport': width - 15, 'focused': 'tab-' + target,
                 'selected': 'tab-' + target, 'labelledby': 'tab-' + target, 'selectedCount': 1,
                 'tabIndex': 0, 'focusVisible': True, 'outlineStyle': 'solid', 'outlineWidth': 3,
                 'outlineOffset': 4, 'outlineExtent': 7, 'opaque': True, 'contrast': 3.4, 'issues': []}
                for target in ('review', 'packet', 'evidence', 'timeline', 'review', 'timeline', 'review')]

    def keyboard_check(self, samples, width=320):
        acceptance = Acceptance(None, Path('/synthetic-muse-keyboard'), {}, {}, '')
        acceptance.case = 'complete'
        with patch.object(acceptance, 'call') as call, \
                patch.object(acceptance, 'evaluate', side_effect=samples), \
                patch.object(acceptance, 'screenshot') as screenshot:
            acceptance.mobile_keyboard(width)
        self.assertEqual(call.call_args_list[-1].args, ('press', 'Home'))
        self.assertEqual(screenshot.call_count, 7)
        self.assertEqual(acceptance.checks[0]['detail']['samples'][-1]['target'], 'tab-review')
        return acceptance.checks[0]

    def test_real_key_sequence_checks_each_sample_and_finishes_on_review(self):
        for width in (390, 320):
            with self.subTest(width=width):
                self.assertTrue(self.keyboard_check(self.keyboard_samples(width), width)['passed'])

    def test_outline_clipping_wrong_binding_or_invisible_indicator_cannot_pass(self):
        for change in ({'issues': ['outline clipped vertically by tabs']}, {'focused': 'tab-review'},
                       {'labelledby': 'tab-review'}, {'focusVisible': False}, {'contrast': 2.9},
                       {'opaque': False}, {'outlineWidth': 0}, {'selectedCount': 2}):
            samples = self.keyboard_samples(320)
            samples[2].update(change)
            with self.subTest(change=change):
                self.assertFalse(self.keyboard_check(samples)['passed'])

    def test_selected_queue_observed_contrast_requires_both_opaque_passing_nodes(self):
        for ratio, opaque, expected in ((4.46, True, False), (4.7, True, True), (4.7, False, False)):
            acceptance = Acceptance(None, Path('/synthetic-muse-contrast'), {}, {}, '')
            observed = {'nodes': [{'opaque': True, 'ratio': 4.7}, {'opaque': opaque, 'ratio': ratio}]}
            with self.subTest(ratio=ratio, opaque=opaque), \
                    patch.object(acceptance, 'evaluate', return_value=observed):
                acceptance.selected_queue_contrast()
                self.assertEqual(acceptance.checks[0]['passed'], expected)

    def test_range_logging_caps_rects_and_omits_text(self):
        rect = {'left': 1, 'right': 8, 'top': 3, 'bottom': 4, 'width': 7, 'height': 1, 'text': 'secret'}
        probe = {'path': '#detail-body > section:nth-of-type(1) > p:nth-of-type(2)',
                 'whiteSpace': 'pre-wrap', 'overflowWrap': 'anywhere', 'scanComplete': False,
                 'scannedUnits': 4096, 'scannedRuns': 256, 'scannedTextNodes': 1,
                 'wholeOverflowRects': [rect] * 9, 'whitespaceOverflowRects': [rect] * 9,
                 'nonWhitespaceOverflowRects': [], 'rawText': 'secret'}
        row = {'name': '320px page, controls and expanded avoid overflow and clipping',
               'detail': {'bad': [{'tag': 'P', 'class': 'item-value', 'rangeProbe': probe, 'text': 'secret'}]}}
        clean = failure_diagnostic(row)
        self.assertNotIn('secret', json.dumps(clean))
        result = clean['bad'][0]['rangeProbe']
        self.assertEqual(len(result['wholeOverflowRects']), 3)
        self.assertEqual(len(result['whitespaceOverflowRects']), 3)
        self.assertFalse(result['scanComplete'])

    def test_positive_keyboard_log_omits_arbitrary_fields_and_caps_samples(self):
        row = self.keyboard_check(self.keyboard_samples(320))
        row['detail']['samples'][0].update(rawText='secret', url='https://secret', rect={'left': 0})
        row['detail']['samples'] *= 3
        clean = measured_diagnostic(row)
        self.assertEqual(len(clean['samples']), 7)
        self.assertNotIn('secret', json.dumps(clean))


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
                patch('tools.sandboxed_chrome.subprocess.Popen', return_value=process) as spawn, \
                redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SandboxError, 'Normal-sandbox Chrome exited'):
                runner.__enter__()
            spawn.assert_called_once()
            run.assert_called_once()  # Version check only, never agent launch.
            flags = {arg.split('=', 1)[0] for arg in spawn.call_args.args[0]}
            self.assertFalse(flags & FORBIDDEN_FLAGS)
            self.assertFalse(runner.metadata['sandbox_verified'])

    def test_missing_renderer_failure_emits_bounded_owned_diagnostics(self):
        runner = SandboxedChrome('/bin/true', '/bin/true', self.out)
        prepare = runner._prepare
        def prepared_profile():
            prepare()
            (runner.profile / 'DevToolsActivePort').write_text('9222\n/devtools/browser/synthetic\n')
            runner.metadata['private_extra'] = 'DO_NOT_LOG_PRIVATE_METADATA'
        diagnostic = {'rows': [['Seccomp-BPF sandbox', 'Yes'], ['PID namespaces', 'Yes']],
                      'text': 'DO_NOT_LOG_PAGE_TEXT'}
        processes = [{'pid': 987654321, 'ppid': 100, 'starttime': '1234', 'state': 'S',
                      'executable': runner.chrome,
                      'args': [runner.chrome, '--type=zygote', '--user-data-dir=/private/SECRET_PROFILE',
                               'http://127.0.0.1/#token=SECRET_TOKEN'],
                      'namespaces': {'pid': 'pid:[1]', 'user': 'user:[2]', 'net': 'net:[3]'},
                      'seccomp': '2', 'seccomp_filters': '1', 'no_new_privs': '1'}]
        chrome_version = {'Browser': 'Chrome/150.0.0.1', 'Protocol-Version': '1.3',
                          'webSocketDebuggerUrl': self.endpoint, 'other': 'DO_NOT_LOG_VERSION_EXTRA'}
        opener = MagicMock()
        opener.open.return_value = io.BytesIO(json.dumps(chrome_version).encode())
        output = io.StringIO()
        try:
            with patch.object(runner, '_prepare', side_effect=prepared_profile), \
                    patch('tools.sandboxed_chrome.os.geteuid', return_value=1000), \
                    patch('tools.sandboxed_chrome.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'agent-browser 0.38.2\n', '')), \
                    patch('tools.sandboxed_chrome.subprocess.Popen', return_value=SimpleNamespace(pid=987654321)), \
                    patch('tools.sandboxed_chrome.build_opener', return_value=opener), \
                    patch.object(runner, '_execute', side_effect=[{}, {'result': diagnostic}]) as execute, \
                    patch('tools.sandboxed_chrome._process_tree', return_value=processes), \
                    patch.object(runner, 'close') as close, redirect_stderr(output):
                with self.assertRaisesRegex(SandboxError, 'No owned renderer'):
                    runner.__enter__()
                close.assert_called_once()
                self.assertEqual(execute.call_count, 2)
            self.assertTrue(output.getvalue(), 'Missing owned-process failure diagnostics in CI stderr')
            event = json.loads(output.getvalue())
            self.assertEqual(event['event'], 'sandbox-startup-failure')
            self.assertEqual(event['stage'], 'sandbox-verification')
            self.assertEqual(event['sandbox_rows'], diagnostic['rows'])
            self.assertEqual(event['processes'][0]['type_flags'], ['--type=zygote'])
            self.assertEqual(event['processes'][0]['ppid'], 100)
            self.assertTrue(event['processes'][0]['ancestry'])
            self.assertFalse(runner.metadata['sandbox_verified'])
            for private in ('SECRET_PROFILE', 'SECRET_TOKEN', 'DO_NOT_LOG_', 'webSocketDebuggerUrl'):
                self.assertNotIn(private, output.getvalue())
            self.assertLessEqual(len(output.getvalue().encode()), 32768)
        finally:
            runner.process = None
            runner.close()

    def test_diagnostics_preserve_subtree_and_report_only_owned_group_extras(self):
        proc = self.out / 'synthetic-proc'
        for pid, parent, group, session in ((10, 1, 10, 10), (11, 10, 10, 10),
                                            (12, 10, 10, 10), (13, 1, 10, 10), (99, 1, 99, 99)):
            path = proc / str(pid)
            path.mkdir(parents=True)
            (path / 'stat').write_text(f'{pid} (chrome) S {parent} {group} {session}')
        records = {pid: {'pid': pid, 'ppid': parent, 'args': ['chrome'], 'namespaces': {}}
                   for pid, parent in ((10, 1), (11, 10), (13, 1))}
        def read_process(pid):
            if pid == 12:
                raise FileNotFoundError(2, 'synthetic process exited')
            return records[pid]
        capture = {}
        with patch('tools.sandboxed_chrome.Path', side_effect=lambda value: proc if value == '/proc' else Path(value)), \
                patch('tools.sandboxed_chrome._proc_record', side_effect=read_process) as read:
            actual = _process_tree(10, diagnostics=capture)
        self.assertEqual(actual, [records[10], records[11]])
        self.assertEqual(capture['group_only_records'], [records[13]])
        self.assertEqual(capture['owned_read_failures'], [
            {'pid': 12, 'ppid': 10, 'error_type': 'FileNotFoundError', 'errno': 2}])
        self.assertEqual([call.args[0] for call in read.call_args_list], [10, 11, 12, 13])
        self.runner.metadata['process_capture'] = capture
        output = io.StringIO()
        with redirect_stderr(output):
            self.runner._emit_startup_failure('sandbox-verification', SandboxError('synthetic'))
        event = json.loads(output.getvalue())
        self.assertFalse(event['group_only_processes'][0]['ancestry'])
        self.assertEqual(event['counts']['owned_candidates'], 3)
        self.assertEqual(event['counts']['owned_read_failures'], 1)

    def test_diagnostic_size_is_bounded_and_preserves_embedded_type_evidence(self):
        process = {'pid': 10, 'ppid': 1, 'args': [
            '/private/chrome --type=renderer --user-data-dir=/private/SECRET_PROFILE '
            'http://127.0.0.1/#token=SECRET_TOKEN'] + ['--flag' + str(n) + '=SECRET_VALUE' for n in range(100)],
            'namespaces': {'pid': 'pid:[1]'}, 'state': 'S', 'starttime': '100'}
        self.runner.metadata['processes'] = [process] * 200
        output = io.StringIO()
        with redirect_stderr(output):
            self.runner._emit_startup_failure('sandbox-verification', SandboxError('SECRET_ERROR'))
        event = json.loads(output.getvalue())
        self.assertLessEqual(len(output.getvalue().encode()), 32768)
        self.assertTrue(event['truncated'])
        self.assertEqual(event['counts']['processes'], 200)
        self.assertEqual(event['processes'][0]['type_flags'], [])
        self.assertEqual(event['processes'][0]['embedded_type_flags'], ['--type=renderer'])
        self.assertNotIn('SECRET_', output.getvalue())

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
            {'pid': 100, 'args': ['/opt/google/chrome/chrome'], 'executable': '/opt/google/chrome/chrome',
             'namespaces': {'pid': 'pid:[1]'},
             'seccomp': '2', 'no_new_privs': '1'},
            {'pid': 101, 'args': ['/opt/google/chrome/chrome', '--type=renderer'],
             'executable': '/opt/google/chrome/chrome', 'namespaces': {'pid': 'pid:[2]'},
             'seccomp': '2', 'no_new_privs': '1'},
        ]

    def test_combined_browser_and_kernel_evidence(self):
        self.assertTrue(verify_sandbox_evidence(self.diagnostic, self.processes)['verified'])

    def test_hosted_single_argument_renderer_title_is_recognized(self):
        # Synthetic values, using the argc=1 shape observed in hosted Chrome
        # 154 diagnostics; ownership and kernel evidence remain mandatory.
        self.processes[1]['args'] = [
            '/opt/google/chrome/chrome --type=renderer --enable-automation '
            '--user-data-dir=/tmp/synthetic-profile --lang=en-US --renderer-client-id=7']
        result = verify_sandbox_evidence(self.diagnostic, self.processes)
        self.assertTrue(result['verified'])
        self.assertEqual(result['renderer_pids'], [101])

    def test_rewritten_title_forbidden_flag_is_not_missed_on_other_owned_process(self):
        self.processes.append({'pid': 102, 'executable': '/opt/google/chrome/chrome',
                               'args': ['/opt/google/chrome/chrome --type=utility --no-sandbox'],
                               'namespaces': {'pid': 'pid:[3]'}, 'seccomp': '2', 'no_new_privs': '1'})
        with self.assertRaisesRegex(SandboxError, 'Sandbox/security disabling flag'):
            verify_sandbox_evidence(self.diagnostic, self.processes)

    def test_all_forbidden_flags_are_rejected_in_both_argument_representations(self):
        for flag in sorted(FORBIDDEN_FLAGS):
            for rewritten in (False, True):
                for suffix in ('', '=false'):
                    processes = copy.deepcopy(self.processes)
                    args = ['/opt/google/chrome/chrome', '--type=renderer', flag + suffix]
                    processes[1]['args'] = [' '.join(args)] if rewritten else args
                    with self.subTest(flag=flag, rewritten=rewritten, suffix=suffix), \
                            self.assertRaisesRegex(SandboxError, 'Sandbox/security disabling flag'):
                        verify_sandbox_evidence(self.diagnostic, processes)

    def test_ambiguous_or_fake_renderer_markers_never_establish_sandbox_proof(self):
        prefix = '/opt/google/chrome/chrome '
        invalid = [
            prefix + '--note=--type=renderer',
            prefix + '--note="hello --type=renderer"',
            prefix + "'--type=renderer'",
            prefix + '--type=renderer --type=utility',
            prefix + '--type=renderer --type=renderer',
            prefix + '--type=renderer-helper',
            prefix + '--type renderer',
            prefix + '--type=',
            prefix + '-- --type=renderer',
            prefix + 'https://example.com/--type=renderer',
            prefix + '--type=renderer https://example.com/',
            prefix + '--type=renderer\t--enable-automation',
            prefix + '--type=renderer --note=escaped\\ value',
            '/opt/google/chrome/chrome-other --type=renderer',
        ]
        for title in invalid:
            processes = copy.deepcopy(self.processes)
            processes[1]['args'] = [title]
            with self.subTest(title=title), self.assertRaises(SandboxError):
                verify_sandbox_evidence(self.diagnostic, processes)

    def test_nul_separated_arguments_keep_boundaries_and_reject_duplicate_type(self):
        for tail in (['--note=hello --type=renderer'], ['--type="renderer"'],
                     ['--type=renderer', '--type=utility'], ['--', '--type=renderer']):
            processes = copy.deepcopy(self.processes)
            processes[1]['args'] = ['/opt/google/chrome/chrome'] + tail
            with self.subTest(tail=tail), self.assertRaises(SandboxError):
                verify_sandbox_evidence(self.diagnostic, processes)

    def test_renderer_executable_must_match_direct_owned_browser(self):
        for wrong in (None, '/other/chrome'):
            for rewritten in (False, True):
                processes = copy.deepcopy(self.processes)
                processes[1]['executable'] = wrong
                if rewritten:
                    processes[1]['args'] = [' '.join(processes[1]['args'])]
                with self.subTest(executable=wrong, rewritten=rewritten), self.assertRaises(SandboxError):
                    verify_sandbox_evidence(self.diagnostic, processes)
        with self.assertRaisesRegex(SandboxError, 'direct launch'):
            verify_sandbox_evidence(self.diagnostic, self.processes, expected_executable='/other/chrome')
        self.processes[0].pop('executable')
        with self.assertRaisesRegex(SandboxError, 'direct launch'):
            verify_sandbox_evidence(self.diagnostic, self.processes)

    def test_unknown_live_chrome_child_is_not_ignored_beside_verified_renderer(self):
        for tail in ([], ['--type=unknown'], ['--type=renderer-helper']):
            processes = copy.deepcopy(self.processes)
            processes.append({'pid': 102, 'executable': '/opt/google/chrome/chrome',
                              'args': ['/opt/google/chrome/chrome'] + tail,
                              'namespaces': {'pid': 'pid:[3]'}, 'seccomp': '0', 'no_new_privs': '0'})
            with self.subTest(tail=tail), self.assertRaisesRegex(SandboxError, 'missing or unknown'):
                verify_sandbox_evidence(self.diagnostic, processes)

    def test_proc_executable_read_failure_is_explicit_and_cannot_be_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / '101'
            process.mkdir()
            (process / 'stat').write_text('101 (chrome) ' + ' '.join(['S', '100', '100', '100'] + ['0'] * 16))
            (process / 'status').write_text('PPid:\t100\nSeccomp:\t2\nNoNewPrivs:\t1\n')
            with patch('tools.sandboxed_chrome.Path', side_effect=lambda value: proc if value == '/proc' else Path(value)), \
                    patch('tools.sandboxed_chrome.os.readlink', side_effect=PermissionError(13, 'synthetic denial')):
                with self.assertRaisesRegex(SandboxError, 'executable identity could not be read'):
                    _proc_record(101)

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
