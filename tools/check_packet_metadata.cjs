#!/usr/bin/env node
// Synthetic source-only review checks. Existing Playwright/Chromium required.
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const fixtures = spawnSync(process.env.KEEL_PYTHON || 'python3', ['-B', '-c', `
import json
from copy import deepcopy
from keel_loki.common import digest
from keel_muse.review import example_snapshot, project_review
from keel_muse.dashboard import render_dashboard
out = {}
for case in ('label', 'required', 'added', 'removed', 'hostile', 'attachment', 'missing', 'unchanged'):
    snapshot = example_snapshot()
    app = snapshot['applications'][0]
    snapshot['applications'] = [app]
    app['previous_packet'] = deepcopy(app['packet'])
    old = app['previous_packet']['fields'][0]
    old.update(label='Old label', required=False)
    app['packet']['fields'][0] = deepcopy(old)
    new = app['packet']['fields'][0]
    if case == 'label': new['label'] = 'New label'
    if case == 'required': new['required'] = True
    if case == 'added': app['previous_packet']['fields'].pop(0)
    if case == 'removed': app['packet']['fields'].pop(0)
    if case == 'hostile': new['label'] = '<img src=x onerror="globalThis.pwned=true">'
    if case == 'attachment': app['packet']['attachments'][0]['sha256'] = 'b' * 64
    if case == 'missing': app['previous_packet'] = None
    if case != 'unchanged': app['packet']['revision_sha256'] = digest(case)
    report = project_review(snapshot, now=snapshot['captured_at'])
    assert report['execution_authorized'] is False
    out[case] = render_dashboard(report)
print(json.dumps(out))
`], {cwd: root, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024});
assert.equal(fixtures.status, 0, 'synthetic fixtures must render');
(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.KEEL_CHROMIUM_EXECUTABLE ? {executablePath: process.env.KEEL_CHROMIUM_EXECUTABLE} : {})});
  let requests = 0;
  try {
    for (const [name, html] of Object.entries(JSON.parse(fixtures.stdout))) {
      const page = await browser.newPage();
      await page.route('**/*', route => {requests += 1; return route.abort();});
      await page.setContent(html);
      await page.locator('#tab-packet').click();
      const compare = page.locator('.compare');
      const columns = compare.locator(':scope > div');
      if (name === 'missing' || name === 'unchanged') {
        assert.equal(await compare.count(), 0);
        const body = await page.locator('#detail-body').innerText();
        assert.ok(body.includes(name === 'missing' ? 'A comparison cannot be calculated.' : 'No displayed fields or attachment records changed.'));
      } else {
        assert.equal(await compare.count(), 1);
        const before = await columns.nth(0).innerText(), after = await columns.nth(1).innerText();
        if (name === 'label') {assert.ok(before.includes('Old label · optional')); assert.ok(after.includes('New label · optional'));}
        if (name === 'required') {assert.ok(before.includes('Old label · optional')); assert.ok(after.includes('Old label · required'));}
        if (name === 'added') {assert.ok(before.includes('Not present')); assert.ok(after.includes('Old label · optional'));}
        if (name === 'removed') {assert.ok(before.includes('Old label · optional')); assert.ok(after.includes('Not present'));}
        if (name === 'hostile') {assert.ok(after.includes('<img src=x onerror="globalThis.pwned=true">')); assert.equal(await page.locator('img').count(), 0); assert.equal(await page.evaluate(() => Boolean(globalThis.pwned)), false);}
        if (name === 'attachment') {assert.ok(after.includes('SHA-256: ' + 'b'.repeat(64))); assert.notEqual(before, after);}
      }
      await page.close();
    }
    assert.equal(requests, 0);
    console.log('PASS: 8 synthetic packet comparison scenarios; zero external requests. No runtime opened.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
