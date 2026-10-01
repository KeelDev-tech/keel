#!/usr/bin/env node
// Offline rendered regression: existing Playwright/Chromium only, no installation.
// Optional KEEL_PYTHON and KEEL_CHROMIUM_EXECUTABLE select existing executables.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
(async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'keel-score-layout-'));
  let browser;
  try {
    const rendered = spawnSync(process.env.KEEL_PYTHON || 'python3', ['-B', '-c',
      'import sys; sys.path.insert(0, "engines"); import build_dashboard as b; print(b.render(b.collect(home=sys.argv[1])))', home], {cwd: root, encoding: 'utf8'});
    assert.equal(rendered.status, 0, 'synthetic dashboard must render');
    browser = await chromium.launch({headless: true, ...(process.env.KEEL_CHROMIUM_EXECUTABLE ? {executablePath: process.env.KEEL_CHROMIUM_EXECUTABLE} : {})});
    let checks = 0;
    for (const width of [320, 390, 480, 481, 768, 1440]) {
      for (const stress of [false, true]) {
        const page = await browser.newPage({viewport: {width, height: 844}});
        await page.route('**/*', route => route.abort());
        await page.setContent(rendered.stdout);
        if (stress) await page.evaluate(() => {
          // Layout-only synthetic stress values; never written to source records.
          document.querySelectorAll('.stat .n').forEach(el => {el.textContent = '123456789012345678901234567890';});
          document.querySelectorAll('.stat .l').forEach(el => {el.textContent = 'A long synthetic score label with UnbrokenSyntheticLabelForWrapping';});
        });
        const expected = await page.locator('.score').innerText();
        const layout = await page.evaluate(() => {
          const cards = [...document.querySelectorAll('.stat')];
          return {
            width: innerWidth, scroll: document.documentElement.scrollWidth,
            tops: cards.map(el => el.getBoundingClientRect().top),
            contained: cards.every(card => {
              const rect = card.getBoundingClientRect();
              return rect.left >= 0 && rect.right <= innerWidth &&
                [...card.querySelectorAll('.n, .l')].every(el => {
                  const range = document.createRange(); range.selectNodeContents(el);
                  const box = el.getBoundingClientRect();
                  return getComputedStyle(el).overflow === 'visible' &&
                    el.scrollWidth <= el.clientWidth && el.scrollHeight <= el.clientHeight &&
                    [...range.getClientRects()].every(r => r.left >= rect.left && r.right <= rect.right + 1 && r.top >= box.top && r.bottom <= box.bottom + 1);
                });
            })
          };
        });
        assert.equal(layout.scroll, width, `horizontal overflow at ${width}px, stress=${stress}`);
        assert.ok(layout.contained, `score text clipped or outside card at ${width}px, stress=${stress}`);
        assert.equal(new Set(layout.tops).size, width <= 480 ? 3 : 1, 'narrow stack / wide row');
        assert.equal(await page.locator('.score').innerText(), expected, 'all score text retained');
        // Exercises the real skip link when combined with the separate semantic PR.
        if (await page.locator('a.skip').count()) {
          await page.keyboard.press('Tab');
          assert.ok(await page.locator('a.skip').evaluate(el => el === document.activeElement && el.getBoundingClientRect().left >= 0));
          await page.keyboard.press('Enter');
          assert.equal(await page.evaluate(() => document.activeElement.id), 'main');
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
        }
        checks += 1;
        await page.close();
      }
    }
    console.log(`PASS: ${checks} synthetic score-card layout scenarios; text containment and optional existing skip navigation checked.`);
  } finally {
    if (browser) await browser.close();
    fs.rmSync(home, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
