#!/usr/bin/env python3
"""Real-browser regressions against the synthetic loopback workbench only.

Requires agent-browser 0.38.2 and a working sandboxed Chrome installation.
Never connects to a live workspace, imports applicant data or submits a form.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_workbench.demo import make_demo, NOW
from keel_workbench.service import Workbench
from keel_workbench.server import LocalServer


CONTRAST = r"""(() => {
  const cell = document.querySelector('th'), style = getComputedStyle(cell);
  function luminance(color) {
    const rgb = color.match(/[\d.]+/g).slice(0,3).map(Number).map(v => v / 255)
      .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  }
  const a = luminance(style.color), b = luminance(style.backgroundColor);
  return {foreground:style.color, background:style.backgroundColor,
    font_size:style.fontSize, ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
})()"""


def check(browser, chrome, out):
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault('AGENT_BROWSER_SOCKET_DIR', str(out / 'browser-sockets'))
    command = [browser, '--json', '--namespace', 'keel-ui-'+str(os.getpid()),
               '--executable-path', chrome, '--allowed-domains', '127.0.0.1,localhost']
    def call(*args):
        result = subprocess.run(command + list(args), env=env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Never echo the navigation URL's transient session token.
            raise RuntimeError('browser command failed: '+args[0])
        value = json.loads(result.stdout)
        if value.get('success') is not True:
            raise RuntimeError('browser command unsuccessful: '+args[0])
        return value['data']
    def evaluate(expression):
        return call('eval', expression)['result']
    checks = []
    def record(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
    app = Workbench(make_demo(), workspace_id='keel-demo', synthetic=True, host_clock=lambda: NOW)
    opened = False
    with LocalServer(app, 0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            call('open', 'http://'+server.authority+'/#token='+server.token)
            opened = True
            call('wait', '--fn', "document.querySelector('#main table') !== null")
            snapshot = call('snapshot', '-i')
            record('page renders meaningful controls', 'Overview' in snapshot['snapshot'])
            record('no error overlay', evaluate("!document.querySelector('[data-nextjs-dialog], .vite-error-overlay')"))
            contrast = evaluate(CONTRAST)
            record('normal table headings meet 4.5:1', contrast['ratio'] >= 4.5)
            call('screenshot', str(out / 'overview.png'))

            # Real keyboard activation removes/recreates the selected card.
            call('click', '[data-page="workflows"]')
            call('snapshot', '-i')
            selected = evaluate("(() => {const b=document.querySelector('.workflow-card:not(.active)'); b.focus(); return b.dataset.workflow;})()")
            call('press', 'Enter')
            call('snapshot', '-i')
            record('workflow selection retains keyboard focus', evaluate(
                "document.activeElement?.dataset.workflow === "+json.dumps(selected)+
                " && document.activeElement.classList.contains('active')"))
            call('screenshot', str(out / 'workflow.png'))

            # Change only the browser's synthetic view, never canonical state.
            evaluate("go('pipeline'); state.query='NO_SYNTHETIC_ROLE_MATCH'; updatePipeline(); true")
            call('snapshot', '-i')
            record('unmatched filters explain filtering', evaluate(
                "document.querySelector('#pipeline-table').innerText.includes('No opportunities match these filters')"))
            evaluate("state.view.roles=[]; state.query=''; updatePipeline(); true")
            call('snapshot', '-i')
            record('empty export does not blame filters', evaluate(
                "document.querySelector('#pipeline-table').innerText.includes('No opportunities in this export')"))
            call('screenshot', str(out / 'empty.png'))
            record('no JavaScript exceptions', call('errors')['errors'] == [])
            report = {'synthetic': True, 'execution_authorized': False,
                      'contrast': contrast, 'checks': checks,
                      'status': 'PASS' if all(row['passed'] for row in checks) else 'FAIL'}
            (out / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps(report))
            return 0 if report['status'] == 'PASS' else 1
        finally:
            try:
                if opened: call('close')
            finally:
                server.shutdown()
                thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--chrome', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        return check(args.browser, args.chrome, args.out.absolute())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print('workbench UI verification unavailable: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
