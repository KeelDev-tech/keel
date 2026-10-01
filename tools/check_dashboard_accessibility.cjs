#!/usr/bin/env node
// Synthetic, offline Chromium checks. Requires an existing Playwright install.
// KEEL_PYTHON and KEEL_CHROMIUM_EXECUTABLE may select existing executables.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'keel-dashboard-accessibility-'));
  let browser;
  try {
    fs.mkdirSync(path.join(home, 'data'));
    fs.writeFileSync(path.join(home, 'data', 'application-ledger.json'), JSON.stringify([
      {status: 'SUBMITTED', company: 'Example Company', title: 'Example role', submitted_at: '2026-01-01T12:00:00Z'}
    ]));
    const rendered = spawnSync(process.env.KEEL_PYTHON || 'python3', ['-B', '-c',
      'import sys; from pathlib import Path; sys.path.insert(0, "engines"); import build_dashboard as b; print(b.render(b.collect(home=sys.argv[1])))', home], {cwd: root, encoding: 'utf8'});
    assert.equal(rendered.status, 0, 'synthetic dashboard must render');
    browser = await chromium.launch({headless: true, ...(process.env.KEEL_CHROMIUM_EXECUTABLE ? {executablePath: process.env.KEEL_CHROMIUM_EXECUTABLE} : {})});
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      await page.route('**/*', route => route.abort());
      await page.setContent(rendered.stdout);
      assert.equal(await page.getByRole('main').count(), 1);
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Skip to dashboard');
      assert.ok(await page.locator('.skip').evaluate(el => el.getBoundingClientRect().left >= 0));
      await page.keyboard.press('Enter');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'main');
      const table = page.getByRole('table', {name: 'Recent submission claims', exact: true});
      assert.equal(await table.count(), 1);
      assert.equal(await table.getByRole('cell').count(), 2);
      assert.ok((await table.getByRole('cell').nth(1).textContent()).includes('Example Company'));
      assert.deepEqual(await table.getByRole('columnheader').allTextContents(), ['Submitted', 'Company and role']);
      await page.close();
    }
    console.log('PASS: synthetic dashboard keyboard, landmark and table checks at desktop/mobile widths; not a WCAG audit.');
  } finally {
    if (browser) await browser.close();
    fs.rmSync(home, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
