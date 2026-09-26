#!/usr/bin/env node
// Trusted local worker. Receives bounded JSON, never JavaScript from an agent.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { resolve4 } from 'node:dns/promises';
import fs from 'node:fs';

const require = createRequire(import.meta.url);
const MAX_INPUT = 8 * 1024 * 1024;
const stable = x => Array.isArray(x) ? x.map(stable) : x && typeof x === 'object'
  ? Object.fromEntries(Object.keys(x).sort().map(k => [k, stable(x[k])])) : x;
const hash = x => createHash('sha256').update(JSON.stringify(stable(x))).digest('hex');
const fail = message => { const error = new Error(message); error.keelSafe = true; throw error; };
const same = (a, b) => JSON.stringify(stable(a)) === JSON.stringify(stable(b));
let browser;

function publicV4(ip) {
  const p = ip.split('.').map(Number);
  return p.length === 4 && p.every(v => Number.isInteger(v) && v >= 0 && v <= 255)
    && p[0] !== 0 && p[0] !== 10 && p[0] !== 127 && p[0] < 224
    && !(p[0] === 100 && p[1] >= 64 && p[1] <= 127)
    && !(p[0] === 169 && p[1] === 254)
    && !(p[0] === 172 && p[1] >= 16 && p[1] <= 31)
    && !(p[0] === 192 && (p[1] === 168 || p[1] === 0 || p[1] === 2))
    && !(p[0] === 198 && (p[1] === 18 || p[1] === 19 || p[1] === 51))
    && !(p[0] === 203 && p[1] === 0 && p[2] === 113);
}

try {
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > MAX_INPUT) fail('browser input exceeds limit');
  }
  const c = JSON.parse(input);
  const operation = c.operation ?? 'prepare';
  if (!['prepare', 'submit_local'].includes(operation)) fail('unsupported operation');
  if (!['local_fixture', 'external_prepare'].includes(c.mode)) fail('unsupported mode');
  const local = c.mode === 'local_fixture';
  const target = new URL(c.url);
  if (target.origin !== c.origin || !same(c.allowed_origins, [c.origin]) || target.username || target.password || target.hash)
    fail('exact origin allowlist mismatch');
  if (local) {
    if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || !target.port
      || target.pathname !== '/apply' || target.search || !/^[a-f0-9]{64}$/.test(c.fixture_nonce))
      fail('invalid loopback fixture contract');
  } else if (target.protocol !== 'https:' || target.port || operation !== 'prepare'
      || !/^[a-z0-9][a-z0-9.-]*\.[a-z0-9.-]+$/.test(target.hostname)) {
    fail('external browser is public HTTPS preparation only');
  }
  const subject = { ...c };
  delete subject.operation; delete subject.prepared; delete subject.bundle_hash;
  if (hash(subject) !== c.bundle_hash) fail('bundle digest mismatch');
  if (!Array.isArray(c.fields) || c.fields.length < 1 || c.fields.length > 100) fail('invalid fields');
  const labels = new Set();
  for (const field of c.fields) {
    if (!field.label || labels.has(field.label) || !['text', 'select', 'checkbox'].includes(field.kind)) fail('invalid field map');
    labels.add(field.label);
  }
  const args = ['--disable-background-networking', '--disable-component-update', '--disable-sync',
    '--no-pings', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'];
  if (!local) {
    const addresses = await resolve4(target.hostname);
    if (!addresses.length || addresses.some(ip => !publicV4(ip))) fail('target does not resolve exclusively to public IPv4');
    // Pin browser DNS to the address just checked; no proxy or system credentials.
    args.push(`--host-resolver-rules=MAP ${target.hostname} ${addresses[0]}, MAP * ~NOTFOUND`);
  }
  const { chromium } = require(process.env.KEEL_PLAYWRIGHT_MODULE || 'playwright');
  const executablePath = process.env.KEEL_CHROMIUM_EXECUTABLE;
  if (executablePath && !fs.existsSync(executablePath)) fail('configured Chromium executable is unavailable');
  browser = await chromium.launch({ headless: true, args, ...(executablePath ? { executablePath } : {}) });
  const context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false,
    javaScriptEnabled: false, ignoreHTTPSErrors: false, permissions: [], storageState: { cookies: [], origins: [] } });
  let page;
  context.on('page', extra => { if (page && extra !== page) extra.close().catch(() => {}); });
  let allowSubmission = false;
  let networkSealed = false;
  let blockedRequests = 0;
  await context.route('**/*', async route => {
    const request = route.request();
    if (networkSealed) { blockedRequests++; return route.abort(); }
    let requested;
    try { requested = new URL(request.url()); } catch { blockedRequests++; return route.abort(); }
    const localSubmit = local && allowSubmission && request.method() === 'POST'
      && requested.origin === c.origin && requested.pathname === '/submit' && !requested.search;
    const permittedRead = request.method() === 'GET' && requested.origin === c.origin
      && (!request.isNavigationRequest() || requested.href === target.href);
    if (request.redirectedFrom() || !(localSubmit || permittedRead)
      || ['websocket', 'eventsource'].includes(request.resourceType())
      || (!local && ['xhr', 'fetch'].includes(request.resourceType()))) {
      blockedRequests++;
      return route.abort();
    }
    await route.continue();
  });
  await context.routeWebSocket('**/*', socket => socket.close());
  page = await context.newPage();
  page.on('dialog', dialog => dialog.dismiss().catch(() => {}));
  page.setDefaultTimeout(10000);
  page.setDefaultNavigationTimeout(15000);
  const loaded = await page.goto(c.url, { waitUntil: 'domcontentloaded' });
  if (!loaded?.ok() || page.url() !== c.url) fail('page did not load at the exact reviewed destination');
  if (local && await page.locator('meta[name="keel-fixture-nonce"]').getAttribute('content') !== c.fixture_nonce)
    fail('fixture identity mismatch');
  // Public preparation requires a server-rendered account marker configured by
  // the self-hosting operator. Logged-out/unknown identity cannot be prepared.
  if (await page.locator('[data-keel-account-id]').count() !== 1
      || await page.locator('[data-keel-account-id]').getAttribute('data-keel-account-id') !== c.account_id)
    fail('account marker mismatch or absent');
  // Even CSS may conditionally request URLs based on form state. Seal egress
  // before private values enter an external page, including same-origin GET.
  if (!local) networkSealed = true;
  for (const field of c.fields) {
    const control = page.getByLabel(field.label, { exact: true });
    if (await control.count() !== 1 || !await control.isVisible() || !await control.isEnabled())
      fail('field missing, ambiguous, or disabled');
    const tag = await control.evaluate(el => ({ tag: el.tagName, type: el.type }));
    if (field.kind === 'text') {
      if (!['INPUT', 'TEXTAREA'].includes(tag.tag) || !['text','email','tel','url','search','number','textarea'].includes(tag.type))
        fail('text map cannot operate this control');
      await control.fill(field.value);
    } else if (field.kind === 'select') {
      if (tag.tag !== 'SELECT') fail('select map requires select control');
      await control.selectOption({ value: field.value });
    } else {
      if (tag.tag !== 'INPUT' || tag.type !== 'checkbox') fail('checkbox map requires checkbox');
      await control.setChecked(field.value);
    }
  }
  if (c.attachment) {
    const a = c.attachment;
    if (!a.name || /[\/\\\r\n]/.test(a.name)) fail('invalid attachment name');
    const buffer = Buffer.from(a.base64, 'base64');
    if (!buffer.length || buffer.length > 5 * 1024 * 1024 || buffer.toString('base64') !== a.base64)
      fail('invalid attachment bytes');
    const upload = page.getByLabel(a.label, { exact: true });
    if (await upload.count() !== 1 || await upload.getAttribute('type') !== 'file') fail('ambiguous attachment control');
    await upload.setInputFiles({ name: a.name, mimeType: a.mime_type, buffer });
  }
  const readback = {};
  for (const field of c.fields) {
    const control = page.getByLabel(field.label, { exact: true });
    const actual = field.kind === 'checkbox' ? await control.isChecked() : await control.inputValue();
    if (actual !== field.value) fail('field readback mismatch');
    readback[field.label] = actual;
  }
  if (c.attachment) {
    const a = c.attachment;
    const actual = await page.getByLabel(a.label, { exact: true }).evaluate(async el => {
      if (el.files.length !== 1) return null;
      const f = el.files[0];
      const data = await f.arrayBuffer();
      const digest = await crypto.subtle.digest('SHA-256', data);
      return { name: f.name, size: f.size, sha256: Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('') };
    });
    const expected = { name: a.name, size: Buffer.from(a.base64,'base64').length,
      sha256: createHash('sha256').update(Buffer.from(a.base64,'base64')).digest('hex') };
    if (!same(actual, expected)) fail('attachment readback mismatch');
    readback.attachment = actual;
  }
  const structure = await page.locator('form').evaluateAll(forms => forms.map(form => ({
    action: form.action, method: form.method, enctype: form.enctype,
    controls: Array.from(form.elements).map(el => ({ tag: el.tagName, type: el.type,
      name: el.name, required: el.required, disabled: el.disabled,
      labels: Array.from(el.labels || []).map(l => l.textContent.trim()),
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({ value: o.value, label: o.text })) : undefined }))
  })));
  if (structure.length !== 1) fail('exactly one reviewed form required');
  const fingerprint = hash(structure);
  const result = { status: 'PREPARED', bundle_hash: c.bundle_hash, form_fingerprint: fingerprint,
    readback, account_id: c.account_id, origin: c.origin, submitted: false,
    execution_authorized: false, browser_backend: 'local_playwright', blocked_requests: blockedRequests,
    account_marker_matched: true, authenticated_account_verified: false };
  if (operation === 'submit_local') {
    if (!local || !c.prepared || c.prepared.status !== 'PREPARED'
      || c.prepared.bundle_hash !== c.bundle_hash || c.prepared.form_fingerprint !== fingerprint
      || !same(c.prepared.readback, readback)) fail('prepared form or values changed; fresh review required');
    if (structure[0].action !== `${c.origin}/submit` || structure[0].method !== 'post') fail('fixture form target changed');
    // These hidden values are fixture-specific and never used on public sites.
    await page.locator('input[name="bundle_hash"]').evaluate((el, v) => { el.value = v; }, c.bundle_hash);
    allowSubmission = true;
    await Promise.all([
      page.waitForURL(`${c.origin}/submit`, { waitUntil: 'domcontentloaded' }),
      page.getByRole('button', { name: c.submit_label, exact: true }).click()
    ]);
    const receipt = JSON.parse(await page.locator('#keel-receipt').innerText());
    if (receipt.fixture_nonce !== c.fixture_nonce || receipt.bundle_hash !== c.bundle_hash
      || receipt.account_id !== c.account_id || receipt.confirmed !== true || !receipt.receipt_id
      || receipt.local_fixture_only !== true || !same(receipt.readback, readback))
      fail('local fixture receipt mismatch; outcome unverified');
    result.status = 'LOCAL_FIXTURE_CONFIRMED';
    result.receipt = receipt;
    result.local_fixture_submitted = true;
    // "submitted" means a real external application; this remains false.
    result.submitted = false;
  }
  await browser.close(); browser = null;
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  if (browser) await browser.close().catch(() => {});
  // Playwright call logs can quote entered private values; never emit them.
  const detail = error.keelSafe ? error.message
    : /Executable doesn't exist/.test(String(error.message))
      ? 'Chromium executable is not installed; run npx playwright install chromium --only-shell'
      : 'Browser operation failed; rendered outcome is unverified. Check local dependencies and control mapping.';
  process.stdout.write(JSON.stringify({ status: 'UNVERIFIED', submitted: false,
    execution_authorized: false, error: String(detail).slice(0, 1000) }));
  process.exitCode = 1;
}
