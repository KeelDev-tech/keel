#!/usr/bin/env node
/** Portable, synthetic-only rendered UI verification. No automatic install.
 * Usage: node tools/check_live_browser.mjs --out NEW-report.json
 *   [--screenshots NEW-directory]
 * Optional: KEEL_PYTHON, KEEL_CHROMIUM_EXECUTABLE,
 * CODEX_PRIMARY_RUNTIME_NODE_MODULES (build environment module location).
 * The private server URL/token never appears in the report or console.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {createRequire} from "node:module";
import {spawn} from "node:child_process";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const options = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const name = process.argv[i];
  if (!["--out", "--screenshots"].includes(name) || !process.argv[i + 1] || options[name]) {
    process.stderr.write("Usage: check_live_browser.mjs --out NEW-report.json [--screenshots NEW-directory]\n");
    process.exit(2);
  }
  options[name] = path.resolve(process.argv[i + 1]);
}
if (!options["--out"] || fs.existsSync(options["--out"]) ||
    (options["--screenshots"] && fs.existsSync(options["--screenshots"]))) {
  process.stderr.write("A new report path and, if used, a new screenshot directory are required.\n");
  process.exit(2);
}

const report = {
  schema: "keel.live.rendered_ui_check.v1", synthetic: true,
  status: "UNVERIFIED", reason: "not_started", checks: [], screenshots: [],
  effects: {model_calls: 0, external_page_requests_sent: 0, external_request_attempts: 0,
    canonical_writes: 0, real_application_actions: 0},
  browser_errors: 0, execution_authorized: false,
  limitations: ["Synthetic local review UI only; no real applicant, consent, model inference, external form preparation or submission.",
    "A successful render does not establish source authenticity or operator identity."],
};
let browser, child, temporary;
let exitCode = 2;
class CheckFailure extends Error {
  constructor(code) {super(code); this.code = code;}
}
function requireCheck(name, passed) {
  report.checks.push({name, passed: Boolean(passed)});
  if (!passed) throw new CheckFailure(name);
}
function playwrightModule() {
  const candidates = [createRequire(import.meta.url)];
  if (process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES) {
    candidates.push(createRequire(path.join(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES, "keel-browser-resolve.cjs")));
  }
  for (const candidate of candidates) {
    try {return candidate("playwright");} catch {}
  }
  return null;
}
async function startDemo() {
  temporary = fs.mkdtempSync(path.join(os.tmpdir(), "keel-live-browser-"));
  fs.chmodSync(temporary, 0o700);
  return await new Promise((resolve, reject) => {
    let buffer = "", settled = false;
    const finish = (error, url) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (error) reject(new CheckFailure(error)); else resolve(url);
    };
    child = spawn(process.env.KEEL_PYTHON || "python3", ["-B", "-m", "keel_live", "demo",
      "--home", path.join(temporary, "synthetic-demo"), "--port", "0"],
      {cwd: root, stdio: ["ignore", "pipe", "pipe"]});
    const timeout = setTimeout(() => finish("synthetic_server_start_timeout"), 15000);
    child.once("error", () => finish("synthetic_server_unavailable"));
    child.once("exit", () => finish("synthetic_server_exited"));
    // Deliberately do not echo either stream: stdout contains the session token.
    child.stderr.on("data", () => {});
    child.stdout.on("data", data => {
      buffer += data.toString("utf8");
      if (buffer.length > 32768) {finish("synthetic_server_output_invalid"); return;}
      const match = buffer.match(/Private local review: (http:\/\/127\.0\.0\.1:\d+\/review#token=[A-Za-z0-9_-]+)/);
      if (match) {const url = match[1]; buffer = ""; finish(null, url);}
    });
  });
}
async function waitState(page, value) {
  await page.waitForFunction(expected => document.querySelector("#request-state")?.textContent === expected, value);
  await page.waitForFunction(() => !document.querySelector("#refresh")?.disabled);
}
async function stopChild() {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise(resolve => {
    const timeout = setTimeout(() => {child.kill("SIGKILL"); resolve();}, 2000);
    child.once("exit", () => {clearTimeout(timeout); resolve();});
    child.kill("SIGTERM");
  });
}

try {
  const playwright = playwrightModule();
  if (!playwright?.chromium) throw new CheckFailure("playwright_not_available");
  const executable = process.env.KEEL_CHROMIUM_EXECUTABLE || playwright.chromium.executablePath();
  if (!executable || !fs.existsSync(executable)) throw new CheckFailure("chromium_not_available");
  try {browser = await playwright.chromium.launch({headless: true, executablePath: executable});}
  catch {throw new CheckFailure("chromium_launch_unavailable");}
  const privateURL = await startDemo();
  const origin = new URL(privateURL).origin;
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, serviceWorkers: "block"});
  await context.route("**/*", async route => {
    let allowed = false;
    try {
      const target = new URL(route.request().url());
      allowed = target.origin === origin && target.hostname === "127.0.0.1" && target.protocol === "http:";
    } catch {}
    if (!allowed) {
      report.effects.external_request_attempts += 1;
      await route.abort("blockedbyclient");
    } else await route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on("pageerror", () => {report.browser_errors += 1;});
  page.on("console", event => {if (event.type() === "error") report.browser_errors += 1;});
  // Error messages and network URLs can contain private context; counts only.
  await page.goto(privateURL, {waitUntil: "networkidle"});
  await page.locator("#workspace").waitFor({state: "visible"});
  await page.waitForFunction(() => document.querySelector("#readiness")?.textContent === "SIX SOURCES REVIEWABLE");
  requireCheck("synthetic_review_visible", (await page.locator("#mode").textContent()).includes("Synthetic rehearsal"));
  requireCheck("token_removed_from_address", new URL(page.url()).hash === "");
  requireCheck("six_current_sources_visible", await page.locator("#current-materials details").count() === 6);
  requireCheck("request_requires_explicit_confirmation", await page.locator("#request").isDisabled());

  await page.locator("#request-confirm").check();
  await page.locator("#request").click();
  await waitState(page, "PENDING");
  requireCheck("explicit_request_recorded", await page.locator("#request-state").textContent() === "PENDING");
  requireCheck("exact_review_digest_displayed", /^[a-f0-9]{64}$/.test(await page.locator("#review-digest").inputValue()));
  requireCheck("decision_requires_explicit_confirmation", await page.locator("#approve").isDisabled());
  await page.locator("#decision-confirm").check();
  await page.locator("#approve").click();
  await waitState(page, "APPROVED");
  const approval = JSON.parse(await page.locator("#packet-json").textContent());
  requireCheck("explicit_synthetic_approval_valid", approval.approval_currently_valid === true &&
    approval.execution_authorized === false && approval.decision.actor_id === "synthetic-fixture-operator");

  await page.locator("#proof").click();
  await page.locator("#proof-json").waitFor({state: "visible"});
  const proof = JSON.parse(await page.locator("#proof-json").textContent());
  requireCheck("existing_assurance_and_trust_still_block", proof.synthetic === true && proof.state === "BLOCKED" &&
    proof.execution_authorized === false && proof.roles.length === 1 &&
    proof.roles[0].assurance_checks_passed === false && proof.roles[0].trust_checks_passed === false);
  requireCheck("render_does_not_authorize_preparation", proof.runtime_checks.rendered_preparation === "NOT_RUN" &&
    proof.runtime_checks.form_readback === "NOT_RUN" && proof.runtime_checks.local_model === "NOT_RUN");

  if (options["--screenshots"]) {
    fs.mkdirSync(options["--screenshots"], {mode: 0o700});
    for (const [name, width, height] of [["review-wide.png", 1440, 1000], ["review-narrow.png", 390, 844]]) {
      await page.setViewportSize({width, height});
      const output = path.join(options["--screenshots"], name);
      await page.screenshot({path: output, fullPage: true});
      fs.chmodSync(output, 0o600);
      report.screenshots.push(output);
    }
    await page.setViewportSize({width: 1440, height: 1000});
  }

  requireCheck("revoke_requires_explicit_confirmation", await page.locator("#revoke").isDisabled());
  await page.locator("#reason").fill("Synthetic rendered verification withdrawal");
  await page.locator("#revoke-confirm").check();
  await page.locator("#revoke").click();
  await waitState(page, "REVOKED");
  const revoked = JSON.parse(await page.locator("#packet-json").textContent());
  requireCheck("explicit_revocation_recorded", revoked.state === "REVOKED" && revoked.approval_currently_valid === false);

  await page.locator("#workbench").click();
  await page.waitForURL(url => url.origin === origin && url.pathname === "/");
  await page.locator("#navigation").waitFor({state: "visible"});
  await page.waitForFunction(() => !["Connecting", "Unverified"].includes(document.querySelector("#mode-pill")?.textContent));
  requireCheck("workbench_navigation_loaded", await page.locator("#navigation").isVisible());
  await page.getByRole("button", {name: "◇ Material review", exact: true}).click();
  await page.waitForURL(url => url.origin === origin && url.pathname === "/review");
  await waitState(page, "REVOKED");
  requireCheck("return_to_review_preserves_revocation", await page.locator("#request-state").textContent() === "REVOKED");
  requireCheck("no_browser_errors", report.browser_errors === 0);
  requireCheck("no_external_request_attempts", report.effects.external_request_attempts === 0);
  report.status = "PASS";
  report.reason = "synthetic_rendered_review_checks_passed";
  exitCode = 0;
} catch (error) {
  report.reason = error instanceof CheckFailure ? error.code : "rendered_verification_incomplete";
  const unavailable = new Set(["playwright_not_available", "chromium_not_available", "chromium_launch_unavailable"]);
  report.status = unavailable.has(report.reason) ? "UNVERIFIED" : "FAIL";
  exitCode = report.status === "FAIL" ? 1 : 2;
} finally {
  if (browser) {try {await browser.close();} catch {}}
  await stopChild();
  if (temporary) {try {fs.rmSync(temporary, {recursive: true, force: true});} catch {}}
  report.checked_at = new Date().toISOString();
  try {
    fs.writeFileSync(options["--out"], JSON.stringify(report, null, 2) + "\n", {flag: "wx", mode: 0o600});
    process.stdout.write(report.status + ": " + report.reason + "\n");
  } catch {
    process.stderr.write("Could not write new browser verification report.\n");
    exitCode = 2;
  }
}
process.exitCode = exitCode;
