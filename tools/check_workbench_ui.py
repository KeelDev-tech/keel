#!/usr/bin/env python3
"""Real-browser regressions against the synthetic loopback workbench only.

Requires agent-browser 0.38.2 and a working sandboxed Chrome installation.
Never connects to a live workspace, imports applicant data or submits a form.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_workbench.demo import make_demo, NOW
from keel_workbench.service import Workbench
from keel_workbench.server import LocalServer
from keel_trust.common import digest
from tools.sandboxed_chrome import SandboxedChrome


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
    # Retain the real timer callback for deterministic response-order checks.
    # Earlier tests still exercise its native 30-second interval unchanged.
    timer_hook = out / 'timer-hook.js'
    timer_hook.write_text("""(() => {
      const original = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => {
        const id = original(callback, delay, ...args);
        if (delay === 30000) {
          window.workbenchRefresh = () => callback(...args);
          window.workbenchInterval = id;
        }
        return id;
      };
    })();""")
    checks = []
    def record(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
    app = Workbench(make_demo(), workspace_id='keel-demo', synthetic=True, host_clock=lambda: NOW)
    with SandboxedChrome(browser, chrome, out, init_scripts=(timer_hook,)) as session, LocalServer(app, 0) as server:
        call = session.call
        evaluate = session.evaluate
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            call('open', 'http://'+server.authority+'/#token='+server.token)
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

            # Exercise the real 30-second refresh, not a direct render call.
            call('click', '[data-page="pipeline"]')
            selected_role = evaluate("(() => {window.refreshTarget=document.querySelector('.opportunity-title'); refreshTarget.focus(); return refreshTarget.dataset.role;})()")
            call('wait', '--fn', '!window.refreshTarget.isConnected')
            call('snapshot', '-i')
            record('automatic refresh retains opportunity focus', evaluate(
                "document.activeElement?.dataset.role === "+json.dumps(selected_role)))
            call('press', 'Enter')
            record('Enter after refresh opens the same role', evaluate(
                "document.querySelector('#detail').open && document.querySelector('#detail-content .subtitle').textContent.includes("+json.dumps(selected_role)+")"))
            if evaluate("document.querySelector('#detail').open"):
                call('click', '#close-detail')

            call('click', '[data-page="overview"]')
            evaluate("window.refreshTarget=document.querySelector('[data-start=\"source-repair\"]'); refreshTarget.focus(); true")
            call('wait', '--fn', '!window.refreshTarget.isConnected')
            call('snapshot', '-i')
            record('automatic refresh retains overview action focus', evaluate(
                "document.activeElement?.dataset.start === 'source-repair'"))
            call('press', 'Enter')
            record('Enter after refresh opens the intended workflow', evaluate(
                "state.page === 'workflows' && state.workflow === 'source-repair'"))

            # Remove one role from the synthetic response only. The real timer
            # still fetches from the loopback server and updates the DOM.
            call('click', '[data-page="pipeline"]')
            evaluate("""(() => {
              window.refreshTarget=document.querySelector('.opportunity-title');
              refreshTarget.focus(); window.removedRole=refreshTarget.dataset.role;
              window.originalRefreshFetch=window.fetch;
              window.fetch=async (...args) => {
                const response=await originalRefreshFetch(...args);
                if(args[0] !== '/api/v1/overview') return response;
                const body=await response.json();
                body.roles=body.roles.filter(role=>role.role_id !== removedRole);
                return new Response(JSON.stringify(body), {status:response.status,headers:response.headers});
              }; return true;
            })()""")
            call('wait', '--fn', '!window.refreshTarget.isConnected')
            call('snapshot', '-i')
            record('removed opportunity falls back to pipeline search', evaluate(
                "document.activeElement === document.querySelector('#search') && !state.view.roles.some(role=>role.role_id === removedRole)"))
            call('press', 'Enter')
            record('removed opportunity does not activate another role', evaluate(
                "!document.querySelector('#detail').open"))
            evaluate("window.fetch=window.originalRefreshFetch; true")

            # Hold a real automatic response while a newer manual refresh
            # reads a changed, validated synthetic service snapshot.
            evaluate("""(() => {
              window.refreshFetch=window.fetch; window.heldOverview=false;
              window.fetch=async (...args) => {
                const response=await refreshFetch(...args);
                if(args[0] !== '/api/v1/overview' || heldOverview) return response;
                window.heldOverview=true;
                await new Promise(resolve=>{window.releaseOverview=resolve;});
                return response;
              }; return true;
            })()""")
            call('wait', '--fn', 'window.heldOverview')
            updated = app.snapshot()
            previous_sha256 = digest(updated)
            updated['labels'][0]['title'] = 'Updated synthetic opportunity'
            app.replace(updated, previous_sha256=previous_sha256)
            newest_sha256 = digest(updated)
            call('click', '#refresh')
            call('wait', '--fn', 'state.view.snapshot_sha256 === '+json.dumps(newest_sha256))
            call('click', '[data-page="pipeline"]')
            call('focus', '#search')
            evaluate("window.releaseOverview(); new Promise(resolve=>setTimeout(()=>resolve(true),0))")
            record('old automatic response cannot replace newer manual snapshot', evaluate(
                'state.view.snapshot_sha256 === '+json.dumps(newest_sha256)))
            record('old automatic response cannot render obsolete opportunity', evaluate(
                "document.querySelector('.opportunity-title').textContent === 'Updated synthetic opportunity'"))
            record('late response respects current navigation and focus', evaluate(
                "state.page === 'pipeline' && document.activeElement === document.querySelector('#search')"))
            evaluate("window.fetch=window.refreshFetch; true")

            # Repeat with overlapping manual refreshes and a dialog opened
            # after the newer response. A late full render must be discarded.
            evaluate("""(() => {
              window.heldOverview=false;
              window.fetch=async (...args) => {
                const response=await refreshFetch(...args);
                if(args[0] !== '/api/v1/overview' || heldOverview) return response;
                window.heldOverview=true;
                await new Promise(resolve=>{window.releaseOverview=resolve;});
                return response;
              }; return true;
            })()""")
            call('click', '#refresh')
            call('wait', '--fn', 'window.heldOverview')
            updated['labels'][0]['title'] = 'Newest synthetic opportunity'
            app.replace(updated, previous_sha256=newest_sha256)
            newest_sha256 = digest(updated)
            call('click', '#refresh')
            call('wait', '--fn', 'state.view.snapshot_sha256 === '+json.dumps(newest_sha256))
            call('click', '.opportunity-title')
            evaluate("window.dialogInvoker=document.querySelector('.opportunity-title'); window.releaseOverview(); new Promise(resolve=>setTimeout(()=>resolve(true),0))")
            record('old manual response cannot replace newer manual snapshot', evaluate(
                'state.view.snapshot_sha256 === '+json.dumps(newest_sha256)))
            record('obsolete refresh preserves open dialog and its invoker', evaluate(
                "document.querySelector('#detail').open && window.dialogInvoker.isConnected"))
            call('click', '#close-detail')
            record('closing dialog after obsolete response restores opportunity focus', evaluate(
                "document.activeElement === window.dialogInvoker"))
            evaluate("window.fetch=window.refreshFetch; true")

            # An interrupted newer request does not make an older pending
            # request current again. Keep the last successfully shown view.
            updated['labels'][0]['title'] = 'Interrupted synthetic opportunity'
            app.replace(updated, previous_sha256=newest_sha256)
            evaluate("""(() => {
              window.heldOverview=false;
              window.fetch=async (...args) => {
                if(args[0] === '/api/v1/overview' && heldOverview)
                  throw new Error('Synthetic interrupted refresh');
                const response=await refreshFetch(...args);
                if(args[0] !== '/api/v1/overview') return response;
                window.heldOverview=true;
                await new Promise(resolve=>{window.releaseOverview=resolve;});
                return response;
              }; return true;
            })()""")
            call('click', '#refresh')
            call('wait', '--fn', 'window.heldOverview')
            call('click', '#refresh')
            call('wait', '--fn', "document.querySelector('#toast').textContent === 'Synthetic interrupted refresh'")
            evaluate("window.releaseOverview(); new Promise(resolve=>setTimeout(()=>resolve(true),0))")
            record('interrupted newer refresh does not revive older pending response', evaluate(
                'state.view.snapshot_sha256 === '+json.dumps(newest_sha256)))
            record('discarded refresh does not report success over newer failure', evaluate(
                "document.querySelector('#toast').textContent === 'Synthetic interrupted refresh'"))
            evaluate("window.fetch=window.refreshFetch; true")

            # Control the real timer callback from here; no wall-clock race
            # can interleave with the intentionally delayed full responses.
            evaluate("clearInterval(window.workbenchInterval); true")
            for page, control, options in [('workflows', '#scope', '#scope'),
                                           ('twin', '#delay', '#invalidate')]:
                call('click', '#refresh')
                call('wait', '--fn', 'state.view.snapshot_sha256 === '+json.dumps(digest(updated)))
                evaluate("state.workflow='source-repair'; go("+json.dumps(page)+"); true")
                previous_sha256 = digest(updated)
                updated['labels'][0]['title'] = 'Full refresh '+page+' fixture'
                app.replace(updated, previous_sha256=previous_sha256)
                evaluate("""(() => {
                  window.fullReleases=[]; window.fullPaths=new Set();
                  window.fetch=async (...args) => {
                    const response=await refreshFetch(...args);
                    if(!['/api/v1/overview','/api/v1/workflows','/api/v1/history'].includes(args[0])
                       || fullPaths.has(args[0])) return response;
                    fullPaths.add(args[0]);
                    await new Promise(resolve=>fullReleases.push(resolve));
                    return response;
                  }; return true;
                })()""")
                call('click', '#refresh')
                call('wait', '--fn', 'window.fullReleases.length === 3')
                evaluate("toast('Awaiting full refresh'); true")
                call('focus', control)
                evaluate('window.pendingControl=document.querySelector('+json.dumps(control)+'); true')
                evaluate('window.workbenchRefresh().then(()=>true)')
                record(page+' timer cannot supersede a pending full refresh', evaluate(
                    'state.view.snapshot_sha256 === '+json.dumps(previous_sha256)))
                record(page+' pending form keeps focus during timer tick', evaluate(
                    'pendingControl.isConnected && document.activeElement === pendingControl'))
                evaluate('fullReleases.forEach(resolve=>resolve()); new Promise(resolve=>setTimeout(()=>resolve(true),0))')
                record(page+' full refresh updates the visible role options', evaluate(
                    'state.view.snapshot_sha256 === '+json.dumps(digest(updated))+
                    ' && document.querySelector('+json.dumps(options)+').textContent.includes('+
                    json.dumps(updated['labels'][0]['title'])+')'))
                record(page+' full refresh reports completion', evaluate(
                    "document.querySelector('#toast').textContent === 'Workspace refreshed.'"))
                evaluate('window.fetch=window.refreshFetch; true')

            # A superseded manual failure must not overwrite newer success.
            evaluate("""(() => {
              window.oldFailureHeld=false;
              window.fetch=async (...args) => {
                const response=await refreshFetch(...args);
                if(args[0] !== '/api/v1/overview' || oldFailureHeld) return response;
                window.oldFailureHeld=true;
                await new Promise((resolve,reject)=>{window.rejectOldRefresh=reject;});
                return response;
              }; return true;
            })()""")
            call('click', '#refresh')
            call('wait', '--fn', 'window.oldFailureHeld')
            evaluate("toast('Awaiting newer refresh'); true")
            call('click', '#refresh')
            call('wait', '--fn', "document.querySelector('#toast').textContent === 'Workspace refreshed.'")
            evaluate("rejectOldRefresh(new Error('Obsolete manual failure')); new Promise(resolve=>setTimeout(()=>resolve(true),0))")
            record('superseded manual failure cannot overwrite newer success', evaluate(
                "document.querySelector('#toast').textContent === 'Workspace refreshed.'"))
            evaluate('window.fetch=window.refreshFetch; true')

            # The same error ordering applies to an older automatic request.
            evaluate("""(() => {
              window.oldFailureHeld=false;
              window.fetch=async (...args) => {
                const response=await refreshFetch(...args);
                if(args[0] !== '/api/v1/overview' || oldFailureHeld) return response;
                window.oldFailureHeld=true;
                await new Promise((resolve,reject)=>{window.rejectOldRefresh=reject;});
                return response;
              };
              window.oldTimer=window.workbenchRefresh(); return true;
            })()""")
            call('wait', '--fn', 'window.oldFailureHeld')
            evaluate("toast('Awaiting newer refresh'); true")
            call('click', '#refresh')
            call('wait', '--fn', "document.querySelector('#toast').textContent === 'Workspace refreshed.'")
            evaluate("rejectOldRefresh(new Error('Obsolete timer failure')); oldTimer.then(()=>true)")
            record('superseded timer failure cannot overwrite newer success', evaluate(
                "document.querySelector('#toast').textContent === 'Workspace refreshed.'"))
            evaluate('window.fetch=window.refreshFetch; true')

            # A current failure releases full-refresh authority so later
            # timer ticks still fetch and render normally.
            evaluate("window.fetch=(...args)=>args[0]==='/api/v1/overview' ? Promise.reject(new Error('Current refresh failure')) : refreshFetch(...args); true")
            call('click', '#refresh')
            call('wait', '--fn', "document.querySelector('#toast').textContent === 'Current refresh failure'")
            previous_sha256 = digest(updated)
            updated['labels'][0]['title'] = 'Timer resumed synthetic opportunity'
            app.replace(updated, previous_sha256=previous_sha256)
            evaluate("window.fetch=window.refreshFetch; go('pipeline'); true")
            call('focus', '#search')
            evaluate('window.workbenchRefresh().then(()=>true)')
            record('timer resumes normally after current full refresh failure', evaluate(
                'state.view.snapshot_sha256 === '+json.dumps(digest(updated))+
                " && document.querySelector('.opportunity-title').textContent === 'Timer resumed synthetic opportunity'"+
                " && document.activeElement === document.querySelector('#search')"))

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
                      'contrast': contrast, 'checks': checks, 'sandbox': session.metadata,
                      'status': 'PASS' if all(row['passed'] for row in checks) else 'FAIL'}
            (out / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps(report))
            return 0 if report['status'] == 'PASS' else 1
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
