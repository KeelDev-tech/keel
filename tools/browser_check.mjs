// Optional free local browser check: npm install --no-save playwright
// npx playwright install chromium
// node tools/browser_check.mjs /absolute/path/to/demo-workspace/dashboard /absolute/path/to/report
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const [input, output] = process.argv.slice(2);
if (!input || !output) throw new Error('dashboard directory and report directory required');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const evidence = { checks: [], scope: 'Functional browser checks; not a WCAG certification.' };
try {
  for (const [name, width, height] of [['desktop', 1440, 1000], ['phone', 390, 844]]) {
    const page = await browser.newPage({ viewport: { width, height } });
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.route('http://**/*', route => route.abort());
    await page.route('https://**/*', route => route.abort());
    await page.goto(pathToFileURL(resolve(input, 'dashboard.html')).href);
    await page.getByRole('heading', { level: 1 }).waitFor();
    assert.equal(await page.locator('script').count(), 0);
    assert.ok(await page.getByText('Provider verification is not connected').isVisible());
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Skip to overview');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'overview');
    assert.deepEqual(errors, []);
    await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true });
    evidence.checks.push({ name, width, height, result: 'PASS' });
    await page.goto(pathToFileURL(resolve(input, 'adversarial.html')).href);
    assert.equal(await page.locator('img, script').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    await page.close();
  }
  evidence.result = 'PASS';
} finally {
  await browser.close();
  await writeFile(resolve(output, 'browser-check.json'), JSON.stringify(evidence, null, 2));
}
