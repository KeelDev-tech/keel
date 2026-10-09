"""Bounded Linux Chrome acceptance runner; never delegates browser launch.

agent-browser 0.38.2 adds --no-sandbox when CI is present. Its implicit
reconnect uses spawn-time AGENT_BROWSER_CDP, while explicit `open` without a
URL ignores that variable. Keep an isolated daemon, immutable CDP options,
an argument-aware command allowlist and a nonexistent local executable.
Sandbox diagnostics are mandatory; unavailable evidence is a failure.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, build_opener


AGENT_BROWSER_VERSION = '0.38.2'
AGENT_BROWSER_COMMIT = '39a74c70d7759d5a6de7a22c04570bb626bbd081'
SOURCE_BASE = 'https://github.com/vercel-labs/agent-browser/blob/' + AGENT_BROWSER_COMMIT
FORBIDDEN_FLAGS = {
    '--no-sandbox', '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox',
    '--disable-namespace-sandbox', '--disable-gpu-sandbox', '--single-process',
    '--in-process-gpu', '--no-zygote', '--disable-web-security',
    '--allow-file-access-from-files', '--ignore-certificate-errors',
}


class SandboxError(RuntimeError):
    pass


def _sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _executable(value):
    found = shutil.which(str(value))
    if not found:
        raise SandboxError('Required installed executable is unavailable: ' + str(value))
    return str(Path(found).resolve())


def _chrome_executable(value):
    resolved = Path(_executable(value))
    wrappers = {
        Path('/usr/bin/chromium'): Path('/usr/lib/chromium/chromium'),
        Path('/opt/google/chrome/google-chrome'): Path('/opt/google/chrome/chrome'),
    }
    with resolved.open('rb') as stream:
        native = stream.read(4) == b'\x7fELF'
    if not native and resolved in wrappers:
        resolved = Path(_executable(wrappers[resolved]))
    with resolved.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            raise SandboxError('Chrome must be a directly installed ELF executable, not an unknown wrapper')
    return str(resolved)


def _unsafe_flags(arguments):
    return [arg for arg in arguments if arg.split('=', 1)[0] in FORBIDDEN_FLAGS]


def _proc_record(pid):
    base = Path('/proc') / str(pid)
    fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (base / 'status').read_text().splitlines()
                  if ':' in line)
    return {
        'pid': pid, 'ppid': int(status['PPid']), 'state': fields[0], 'starttime': fields[19],
        'args': (base / 'cmdline').read_bytes().decode().rstrip('\0').split('\0'),
        'seccomp': status.get('Seccomp', '').strip(),
        'seccomp_filters': status.get('Seccomp_filters', '').strip(),
        'no_new_privs': status.get('NoNewPrivs', '').strip(),
        'namespaces': ({name: os.readlink(base / 'ns' / name) for name in ('pid', 'user', 'net')}
                       if fields[0] != 'Z' else {}),
    }


def _process_tree(pid, *, diagnostics=None):
    # Enumerate ancestry through stat first; do not read unrelated cmdlines.
    parents = {}
    own_group = set()
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                tail = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                parents[int(entry.name)] = int(tail[1])
                if int(tail[2]) == pid and int(tail[3]) == pid:
                    own_group.add(int(entry.name))
            except (OSError, ValueError, IndexError):
                continue
    owned = {pid}
    while True:
        expanded = owned | {child for child, parent in parents.items() if parent in owned}
        if expanded == owned:
            break
        owned = expanded
    records = []
    if diagnostics is not None:
        diagnostics.update({'owned_candidates': [{'pid': child, 'ppid': parents.get(child)}
                                                for child in sorted(owned)],
                            'owned_read_failures': [], 'records': records,
                            'group_only_records': [], 'group_only_read_failures': []})
    for child in [pid] + sorted(owned - {pid}):
        try:
            records.append(_proc_record(child))
        except OSError as error:
            if diagnostics is not None:
                diagnostics['owned_read_failures'].append({
                    'pid': child, 'ppid': parents.get(child),
                    'error_type': type(error).__name__, 'errno': error.errno})
            if child != pid and isinstance(error, FileNotFoundError):
                continue  # Preserve the existing transient-child behavior.
            raise
    if diagnostics is not None:
        # Diagnostic-only: the launcher creates this dedicated group/session.
        # Record reparented members, but NEVER add them to verifier evidence.
        for child in sorted(own_group - owned):
            try:
                diagnostics['group_only_records'].append(_proc_record(child))
            except (OSError, ValueError, IndexError) as error:
                diagnostics['group_only_read_failures'].append({
                    'pid': child, 'ppid': parents.get(child),
                    'error_type': type(error).__name__, 'errno': getattr(error, 'errno', None)})
    return records


def _diagnostic_process(record):
    args = record.get('args', [])
    basename = Path(args[0]).name if args else ''
    # Chrome may rewrite argv[0]. Log type markers and argument lengths to
    # diagnose representation differences without leaking URL/path values.
    result = {key: (record.get(key) if isinstance(record.get(key), (int, type(None)))
                    else str(record.get(key))[:80]) for key in (
        'pid', 'ppid', 'starttime', 'state', 'seccomp', 'seccomp_filters', 'no_new_privs')}
    result.update({
        'executable_basename': basename if re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', basename) else '<nonstandard-argv0>',
        'argv_count': len(args), 'argv_lengths': [len(arg) for arg in args[:64]],
        'type_flags': [arg for arg in args if re.fullmatch(r'--type=[A-Za-z0-9_-]{1,40}', arg)][:16],
        'embedded_type_flags': sorted({match for arg in args for match in
                                     re.findall(r'(?:^|\s)(--type=[A-Za-z0-9_-]{1,40})(?=\s|$)', arg)})[:16],
        'flag_names': sorted({match for arg in args for match in
                             re.findall(r'(?:^|\s)(--[A-Za-z][A-Za-z0-9-]{0,80})(?=[=\s]|$)', arg)})[:64],
        'namespaces': {key: str(record.get('namespaces', {}).get(key, ''))[:80]
                       for key in ('pid', 'user', 'net')},
    })
    return result


def verify_sandbox_evidence(diagnostic, processes):
    """Require browser-reported sandbox AND kernel evidence from renderers."""
    rows = {re.sub(r'\s+', ' ', str(row[0])).strip().lower(): str(row[1]).strip().lower()
            for row in diagnostic.get('rows', []) if len(row) >= 2}
    if rows.get('seccomp-bpf sandbox') != 'yes' or rows.get('pid namespaces') != 'yes':
        raise SandboxError('chrome://sandbox does not prove Seccomp-BPF and PID namespaces')
    if not processes:
        raise SandboxError('No owned Chrome processes were observable')
    browser = processes[0]
    renderers = [process for process in processes if '--type=renderer' in process['args']]
    if not renderers:
        raise SandboxError('No owned renderer was observable for sandbox verification')
    for process in processes:
        if _unsafe_flags(process['args']):
            raise SandboxError('Sandbox/security disabling flag observed in owned Chrome process')
    for renderer in renderers:
        if renderer['seccomp'] != '2' or renderer['no_new_privs'] != '1':
            raise SandboxError('Renderer lacks Seccomp filtering or NoNewPrivs')
        if renderer['namespaces']['pid'] == browser['namespaces']['pid']:
            raise SandboxError('Renderer PID namespace is not isolated from browser')
    return {'verified': True, 'renderer_pids': [p['pid'] for p in renderers],
            'diagnostic_rows': rows}


class SandboxedChrome:
    def __init__(self, browser, chrome, out, *, init_scripts=()):
        self.browser, self.chrome = _executable(browser), _chrome_executable(chrome)
        self.out = Path(out).resolve()
        self.init_scripts = tuple(str(Path(path).resolve()) for path in init_scripts)
        self.process = None
        self._temp = None
        self._log = None
        self._active = False
        self._last_result = None
        self.socket_dir = None
        self.session = 'acceptance'
        self.command = []
        self.env = {}
        self.metadata = {'sandbox_verified': False, 'agent_browser_version_required': AGENT_BROWSER_VERSION,
                         'audited_source_commit': AGENT_BROWSER_COMMIT,
                         'source': SOURCE_BASE, 'receipts': []}

    def _save(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / 'sandbox-evidence.json').write_text(json.dumps(self.metadata, indent=2) + '\n')

    def _emit_startup_failure(self, stage, error):
        """Bounded, field-allowlisted CI evidence; no page/URL/env values."""
        capture = self.metadata.get('process_capture', {})
        processes = self.metadata.get('processes', capture.get('records', []))
        group_only = capture.get('group_only_records', [])
        candidates = capture.get('owned_candidates', [])
        failures = capture.get('owned_read_failures', [])
        group_failures = capture.get('group_only_read_failures', [])
        rows = self.metadata.get('sandbox_diagnostic', {}).get('rows', [])
        event = {
            'event': 'sandbox-startup-failure', 'stage': stage, 'error_type': type(error).__name__,
            'chrome_pid': self.metadata.get('chrome_pid'),
            'chrome_version': {key: str(self.metadata.get('chrome_version', {}).get(key, ''))[:160]
                               for key in ('Browser', 'Protocol-Version')},
            'sandbox_rows': [[str(cell)[:160] for cell in row[:2]] for row in rows[:32]],
            'processes': [dict(_diagnostic_process(record), ancestry=True) for record in processes[:32]],
            'group_only_processes': [dict(_diagnostic_process(record), ancestry=False) for record in group_only[:32]],
            'owned_candidates': candidates[:128], 'owned_read_failures': failures[:64],
            'group_only_read_failures': group_failures[:64],
            'counts': {'sandbox_rows': len(rows), 'processes': len(processes), 'group_only_processes': len(group_only),
                       'owned_candidates': len(candidates), 'owned_read_failures': len(failures),
                       'group_only_read_failures': len(group_failures)},
        }
        fields = ('group_only_processes', 'processes', 'owned_candidates', 'owned_read_failures',
                  'group_only_read_failures', 'sandbox_rows')
        event['truncated'] = any(len(event[key]) < event['counts'][key] for key in fields)
        encoded = json.dumps(event, separators=(',', ':'), sort_keys=True)
        while len(encoded.encode()) > 32767:
            for field in fields:
                if len(event[field]) > (1 if field == 'processes' else 0):
                    event[field].pop()
                    break
            else:
                break
            event['truncated'] = True
            encoded = json.dumps(event, separators=(',', ':'), sort_keys=True)
        sys.stderr.write(encoded + '\n')
        sys.stderr.flush()

    def _receipt(self, kind, args, returncode=None, input_text=None):
        # Navigation fragments may contain transient Workbench tokens. Hash
        # arguments and script bodies rather than storing their contents.
        self.metadata['receipts'].append({
            'kind': kind, 'action': args[0] if args else None,
            'argument_sha256': [hashlib.sha256(str(arg).encode()).hexdigest() for arg in args],
            'input_sha256': hashlib.sha256(input_text.encode()).hexdigest() if input_text else None,
            'returncode': returncode,
        })

    def _prepare(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / 'downloads').mkdir(exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(prefix='keel-chrome-')
        self.run_dir = Path(self._temp.name)
        self.profile = self.run_dir / 'profile'
        self.profile.mkdir()
        self.socket_dir = self.run_dir / 'sockets'
        self.socket_dir.mkdir()
        self.config = self.run_dir / 'agent-browser.json'
        self.config.write_text('{}\n')  # Suppress user/project config discovery.
        self._init_script_hashes = {path: _sha256(path) for path in self.init_scripts}
        self.sentinel = self.run_dir / 'LOCAL_BROWSER_LAUNCH_FORBIDDEN'
        self.session = 'acceptance'
        # No inherited agent settings, plugins, daemon mode, or connection
        # modes. Preserve CI exactly; its value is never changed or removed.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('AGENT_BROWSER_')}
        self.env.update({'AGENT_BROWSER_SOCKET_DIR': str(self.socket_dir),
                         'AGENT_BROWSER_SESSION': self.session,
                         'AGENT_BROWSER_CONFIG': str(self.config),
                         'AGENT_BROWSER_EXECUTABLE_PATH': str(self.sentinel),
                         'AGENT_BROWSER_ENGINE': 'chrome',
                         'AGENT_BROWSER_DEFAULT_TIMEOUT': '45000',
                         'AGENT_BROWSER_NO_XVFB': '1',
                         'AGENT_BROWSER_DOWNLOAD_PATH': str(self.out / 'downloads')})

    def _attach_options(self, endpoint):
        self.endpoint = endpoint
        self.env['AGENT_BROWSER_CDP'] = endpoint
        self.command = [self.browser, '--json', '--config', str(self.config),
                        '--session', self.session, '--cdp', endpoint,
                        '--executable-path', str(self.sentinel), '--engine', 'chrome',
                        '--download-path', str(self.out / 'downloads')]
        for script in self.init_scripts:
            self.command.extend(['--init-script', script])
        self._fixed_command = tuple(self.command)
        self._fixed_env = dict(self.env)
        self.metadata['attach'] = {'endpoint': endpoint, 'session': self.session,
                                  'socket_directory': str(self.socket_dir),
                                  'config_sha256': _sha256(self.config),
                                  'local_executable_sentinel': str(self.sentinel),
                                  'init_script_sha256': dict(self._init_script_hashes)}

    def __enter__(self):
        stage = 'prepare'
        try:
            self._prepare()
            if os.name != 'posix' or not Path('/proc/self/status').exists():
                raise SandboxError('Linux /proc sandbox verification is required')
            if os.geteuid() == 0:
                raise SandboxError('Normal Chrome sandbox requires a non-root user')
            stage = 'agent-version'
            version = subprocess.run([self.browser, '--version'], env=self.env,
                                     capture_output=True, text=True, timeout=15, check=True)
            if not re.fullmatch(r'agent-browser\s+0\.38\.2\s*', version.stdout):
                raise SandboxError('agent-browser must be pinned to version 0.38.2')
            self.metadata['executables'] = {
                'agent_browser': {'path': self.browser, 'sha256': _sha256(self.browser),
                                  'version': version.stdout.strip()},
                'chrome': {'path': self.chrome, 'sha256': _sha256(self.chrome)},
            }
            args = [self.chrome, '--headless=new', '--remote-debugging-address=127.0.0.1',
                    '--remote-debugging-port=0', '--user-data-dir=' + str(self.profile),
                    '--no-first-run', '--no-default-browser-check', '--enable-automation', 'about:blank']
            if _unsafe_flags(args):
                raise SandboxError('Unsafe Chrome launch arguments')
            self.metadata['chrome_launch'] = args
            self.metadata['ci'] = {'present': 'CI' in os.environ, 'preserved': self.env.get('CI') == os.environ.get('CI')}
            self._log = open(self.out / 'chrome-stderr.log', 'w')
            stage = 'chrome-launch'
            self.process = subprocess.Popen(args, env=self.env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=self._log,
                                            start_new_session=True, cwd=self.run_dir)
            self.metadata['chrome_pid'] = self.process.pid
            self._save()
            deadline = time.monotonic() + 20
            stage = 'chrome-endpoint'
            port_file = self.profile / 'DevToolsActivePort'
            while not port_file.exists():
                if self.process.poll() is not None:
                    raise SandboxError('Normal-sandbox Chrome exited; see chrome-stderr.log (no fallback attempted)')
                if time.monotonic() >= deadline:
                    raise SandboxError('Normal-sandbox Chrome did not expose CDP (no fallback attempted)')
                time.sleep(.05)
            port, browser_path = port_file.read_text().splitlines()[:2]
            if not port.isdigit() or not 0 < int(port) < 65536 or not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', browser_path):
                raise SandboxError('Unexpected owned Chrome DevTools endpoint')
            endpoint = 'ws://127.0.0.1:' + port + browser_path
            opener = build_opener(ProxyHandler({}))
            with opener.open('http://127.0.0.1:' + port + '/json/version', timeout=5) as response:
                self.metadata['chrome_version'] = json.load(response)
            if self.metadata['chrome_version'].get('webSocketDebuggerUrl') != endpoint:
                raise SandboxError('DevTools endpoint does not match owned Chrome profile')
            self._attach_options(endpoint)
            stage = 'sandbox-document'
            self._execute('open', 'chrome://sandbox')
            diagnostic = self._execute('eval', '--stdin', input_text="JSON.stringify({text:document.body.innerText,rows:Array.from(document.querySelectorAll('tr'),r=>Array.from(r.querySelectorAll('td,th'),c=>c.innerText))})")['result']
            if isinstance(diagnostic, str):
                diagnostic = json.loads(diagnostic)
            self.metadata['sandbox_diagnostic'] = diagnostic
            stage = 'owned-process-capture'
            self.metadata['process_capture'] = {}
            processes = _process_tree(self.process.pid, diagnostics=self.metadata['process_capture'])
            self.metadata['processes'] = processes
            stage = 'sandbox-verification'
            self.metadata['sandbox'] = verify_sandbox_evidence(diagnostic, processes)
            stage = 'sandbox-screenshot'
            self._execute('screenshot', str(self.out / 'chrome-sandbox.png'))
            self._execute('open', 'about:blank')
            self.metadata['sandbox_verified'] = True
            self._active = True
            self._save()
            return self
        except Exception as error:
            self.metadata['failure'] = str(error)
            try:
                self._emit_startup_failure(stage, error)
            except Exception:
                # Failure logging must not replace the original gate failure
                # or prevent cleanup if stderr is closed or unavailable.
                pass
            self.close()
            raise

    def _output_path(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.out):
            raise SandboxError('Browser output must stay inside the evidence directory')
        return str(path)

    def _validate(self, args):
        if not args or not all(isinstance(arg, str) for arg in args):
            raise SandboxError('Browser command requires string arguments')
        verb, *rest = args
        shapes = {
            'click': 1, 'focus': 1, 'press': 1, 'fill': 2, 'select': 2,
            'check': 1, 'uncheck': 1, 'errors': 0, 'console': 0, 'close': 0,
        }
        valid = verb in shapes and len(rest) == shapes[verb]
        permitted_switches = set()
        if verb == 'open' and len(rest) == 1:
            url = urlsplit(rest[0])
            valid = ((url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost')
                      and not url.username and not url.password)
                     or (url.scheme == 'file' and not url.netloc
                         and Path(unquote(url.path)).resolve().is_relative_to(self.out)))
        elif verb == 'snapshot':
            valid = not rest or rest == ['-i']
            permitted_switches = {'-i'}
        elif verb == 'wait':
            valid = len(rest) == 2 and rest[0] == '--fn'
            permitted_switches = {'--fn'}
        elif verb == 'screenshot':
            valid = len(rest) == 1 or (len(rest) == 2 and rest[1] == '--full')
            permitted_switches = {'--full'}
            if valid:
                self._output_path(rest[0])
        elif verb == 'download':
            valid = len(rest) == 2
            if valid:
                self._output_path(rest[1])
                if Path(rest[1]).exists() or Path(rest[1]).is_symlink():
                    raise SandboxError('Download destination must be new, not existing or a symlink')
        elif verb == 'network':
            valid = rest in (['requests'], ['requests', '--clear'])
            permitted_switches = {'--clear'}
        elif verb == 'console':
            valid = not rest or rest == ['--clear']
            permitted_switches = {'--clear'}
        elif verb == 'get':
            valid = rest in (['url'], ['title'], ['cdp-url'])
        elif verb == 'set':
            valid = (len(rest) == 3 and rest[0] == 'viewport'
                     and all(item.isdigit() and 100 <= int(item) <= 5000 for item in rest[1:]))
        # The CLI scans all argv for global flags, including positional
        # values. Do not allow a selector/value to smuggle launch options.
        if not valid or any(arg.startswith('-') and arg not in permitted_switches for arg in rest):
            raise SandboxError('Command is outside the attach-only acceptance allowlist: ' + verb)

    def _execute(self, *args, input_text=None):
        self._assert_fixed_attach()
        try:
            result = subprocess.run(self.command + list(args), env=self.env,
                                    input=input_text, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as error:
            self._receipt('agent-browser', args, input_text=input_text)
            raise SandboxError('Browser command timed out: ' + args[0]) from error
        self._receipt('agent-browser', args, result.returncode, input_text)
        self._last_result = result
        self.metadata['receipts'][-1].update({
            'stdout_sha256': hashlib.sha256(result.stdout.encode()).hexdigest(),
            'stderr_sha256': hashlib.sha256(result.stderr.encode()).hexdigest(),
        })
        if result.returncode:
            raise SandboxError('Attached browser command failed: ' + args[0] + ' (no fallback attempted)')
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise SandboxError('Attached browser returned invalid JSON: ' + args[0]) from error
        if value.get('success') is not True:
            raise SandboxError('Attached browser command unsuccessful: ' + args[0])
        return value.get('data', {})

    def _assert_fixed_attach(self):
        if self.sentinel.exists() or self.sentinel.is_symlink() or self.config.read_text() != '{}\n':
            raise SandboxError('Isolated launch-denial configuration changed')
        if self.env.get('AGENT_BROWSER_CDP') != self.endpoint:
            raise SandboxError('Retained daemon CDP configuration changed')
        if tuple(self.command) != self._fixed_command or self.env != self._fixed_env:
            raise SandboxError('Fixed attach command or environment changed')
        try:
            unchanged = all(_sha256(path) == digest for path, digest in self._init_script_hashes.items())
        except OSError as error:
            raise SandboxError('Fixed init script became unavailable') from error
        if not unchanged:
            raise SandboxError('Fixed init script changed')

    def _daemon_evidence(self):
        pid = int((self.socket_dir / (self.session + '.pid')).read_text().strip())
        if pid <= 1:
            raise SandboxError('Invalid isolated daemon PID')
        base = Path('/proc') / str(pid)
        environment = dict(item.split(b'=', 1) for item in (base / 'environ').read_bytes().split(b'\0')
                           if b'=' in item)
        markers = {key: environment.get(key.encode(), b'').decode() for key in (
            'AGENT_BROWSER_SOCKET_DIR', 'AGENT_BROWSER_CDP', 'AGENT_BROWSER_EXECUTABLE_PATH',
            'AGENT_BROWSER_ENGINE', 'AGENT_BROWSER_SESSION')}
        if any(markers[key] != self.env[key] for key in markers):
            raise SandboxError('Owned daemon did not retain the fixed attach configuration')
        if any(key.encode() in environment for key in ('AGENT_BROWSER_PROVIDER', 'AGENT_BROWSER_AUTO_CONNECT',
                                                       'AGENT_BROWSER_PROFILE', 'AGENT_BROWSER_ARGS')):
            raise SandboxError('Owned daemon retained an unexpected launch mode')
        agent_environment = sorted((key.decode(), value.decode()) for key, value in environment.items()
                                   if key.startswith(b'AGENT_BROWSER_'))
        fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'starttime': fields[19], 'executable': os.readlink(base / 'exe'),
                'process': _proc_record(pid), 'retained_environment': markers,
                'agent_environment_sha256': hashlib.sha256(json.dumps(agent_environment).encode()).hexdigest()}

    def _fixed_stale_snapshot(self, daemon):
        """One fixed normal action, bypassing only the CLI attach preamble.

        There is intentionally no general raw-protocol command interface.
        This invokes the daemon's implicit stale-CDP recovery itself.
        """
        path = self.socket_dir / (self.session + '.sock')
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise SandboxError('Isolated daemon socket identity cannot be verified')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(path))
            pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != daemon['pid'] or uid != os.geteuid():
                raise SandboxError('Stale-CDP probe socket peer does not match owned daemon')
            client.sendall(b'{"id":"keel-stale-cdp-proof","action":"snapshot"}\n')
            data = bytearray()
            deadline = time.monotonic() + 10
            while b'\n' not in data:
                if time.monotonic() >= deadline or len(data) >= 1024 * 1024:
                    raise SandboxError('Stale-CDP proof response exceeded its bounds')
                chunk = client.recv(min(65536, 1024 * 1024 - len(data)))
                if not chunk:
                    raise SandboxError('Stale-CDP proof returned no complete response')
                data.extend(chunk)
        return json.loads(data.split(b'\n', 1)[0])

    def _chrome_group_members(self):
        members = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] != 'Z' and int(fields[2]) == self.process.pid:
                    members.append(int(path.name))
            except (OSError, ValueError, IndexError):
                continue
        return sorted(members)

    def _prior_chrome_states(self, processes):
        states = []
        for previous in processes:
            item = {'pid': previous['pid'], 'original_starttime': previous['starttime'], 'owned_live': False}
            try:
                fields = (Path('/proc') / str(previous['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()
                item.update({'state': fields[0], 'starttime': fields[19],
                             'owned_live': fields[19] == previous['starttime'] and fields[0] != 'Z'})
            except FileNotFoundError:
                item['state'] = 'absent'
            states.append(item)
        return states

    @staticmethod
    def _same_daemon(before, after):
        # Scheduling state may change S/R while read; process starttime,
        # executable and retained configuration must remain exactly stable.
        return all(before.get(key) == after.get(key) for key in (
            'pid', 'starttime', 'executable', 'retained_environment', 'agent_environment_sha256'))

    def _terminate_chrome(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                pass

    def prove_disconnect_fails_closed(self):
        """Final destructive check on this synthetic browser, before cleanup.

        Requires successful real sandbox diagnostics. It exercises both the
        existing daemon's stale recovery and the CLI's attach preamble. The
        browser cannot be used afterwards; run this after UI/screenshots.
        """
        if not self._active or not self.metadata['sandbox_verified']:
            raise SandboxError('Disconnect proof requires a verified live sandbox')
        self._assert_fixed_attach()
        self.call('snapshot')  # Ensure the isolated daemon exists and is connected.
        before = self._daemon_evidence()
        proof = {'passed': False, 'kind': 'runtime-owned-browser-disconnect', 'daemon_before': before,
                 'browser_processes_before': _process_tree(self.process.pid)}
        self.metadata['disconnect_proof'] = proof
        try:
            self._terminate_chrome()
            deadline = time.monotonic() + 5
            while (self._chrome_group_members()
                   or any(row['owned_live'] for row in self._prior_chrome_states(proof['browser_processes_before']))) and time.monotonic() < deadline:
                time.sleep(.05)
            proof['previous_browser_processes_after'] = self._prior_chrome_states(proof['browser_processes_before'])
            if self._chrome_group_members() or any(row['owned_live'] for row in proof['previous_browser_processes_after']):
                raise SandboxError('Observed owned Chrome processes did not terminate')
            response = self._fixed_stale_snapshot(before)
            proof['daemon_response'] = response
            prefix = 'CDP connection failed: CDP WebSocket connect failed:'
            if (response.get('success') is not False
                    or response.get('id') != 'keel-stale-cdp-proof'
                    or not str(response.get('error', '')).startswith(prefix)):
                raise SandboxError('Stale daemon did not fail specifically at retained-CDP reconnection')
            after = self._daemon_evidence()
            proof['daemon_after'] = after
            if not self._same_daemon(before, after):
                raise SandboxError('Stale-CDP proof changed daemon identity or retained configuration')
            self._last_result = None
            try:
                self.call('snapshot')
            except SandboxError as error:
                if 'Attached browser command failed: snapshot' not in str(error):
                    raise
                if self._last_result is None:
                    raise SandboxError('CLI disconnect proof has no subprocess receipt') from error
                cli_response = json.loads(self._last_result.stdout)
                if (cli_response.get('success') is not False
                        or not str(cli_response.get('error', '')).startswith('CDP WebSocket connect failed:')):
                    raise SandboxError('CLI did not fail specifically at CDP attachment') from error
                proof['cli_response'] = cli_response
                proof['cli_attach_failed'] = True
            else:
                raise SandboxError('CLI unexpectedly succeeded after owned Chrome disconnected')
            self._assert_fixed_attach()
            proof['daemon_after_cli'] = self._daemon_evidence()
            if not self._same_daemon(before, proof['daemon_after_cli']):
                raise SandboxError('CLI failure changed daemon identity or retained configuration')
            children = _process_tree(before['pid'])
            proof['daemon_processes_after'] = children
            proof['chrome_group_members_after'] = self._chrome_group_members()
            if len(children) != 1 or proof['chrome_group_members_after']:
                raise SandboxError('Unexpected process remained after failed CDP reconnection')
            proof['passed'] = True
            return proof
        except Exception as error:
            proof['failure'] = str(error)
            raise
        finally:
            self._active = False
            self._save()

    def call(self, *args):
        if not self._active:
            raise SandboxError('Sandbox verification must succeed before acceptance commands')
        if args and args[0] == 'eval' and len(args) == 2 and isinstance(args[1], str):
            return self._execute('eval', '--stdin', input_text=args[1])
        self._validate(args)
        return self._execute(*args)

    def evaluate(self, expression):
        return self.call('eval', expression)['result']

    def screenshot(self, path):
        return self.call('screenshot', self._output_path(path))

    def close(self):
        self._active = False
        # Never issue a CLI cleanup command which could restart a dead daemon.
        if self._temp and self.socket_dir is not None:
            pid_file = self.socket_dir / (self.session + '.pid')
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    environment = (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
                    marker = ('AGENT_BROWSER_SOCKET_DIR=' + str(self.socket_dir)).encode()
                    if pid > 1 and marker in environment:
                        os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError):
                    pass
        self._terminate_chrome()
        if self._log:
            self._log.close()
            self._log = None
        self._save()
        if self._temp:
            self._temp.cleanup()
            self._temp = None

    def __exit__(self, kind, value, traceback):
        if value:
            self.metadata['failure'] = str(value)
        self.close()
