# Local browser execution

Keel includes an optional local Playwright adapter. It does not need a browser
subscription, paid model API, hosted database, or remote browser service. Node.js,
Playwright, Chromium, and the Python fixture run on the operator's computer.
Hardware, electricity, connectivity, and optional hosting are still the
operator's responsibility.

## Install the free optional dependency

Use a supported Node.js release and install this pinned browser package in the
Keel project directory:

```sh
npm install --no-save --package-lock=false playwright@1.62.1
npx playwright install chromium --only-shell
python3 -B tools/local_application_fixture.py --rehearse
```

On Linux, Playwright may also require operating-system browser libraries. Follow
the official Playwright installation instructions for the host distribution.
Do not disable an existing operating-system security policy just to make a test
green. Installation can download roughly hundreds of megabytes depending on the
platform. No account, API key, or paid plan is required.

If Node or Playwright is outside the normal search path, the rehearsal accepts
`--node-path /absolute/path/to/node`,
`--playwright-module /absolute/path/to/node_modules/playwright`, and optionally
`--chromium-path /absolute/path/to/a/compatible/chromium`.
The adapter also reads `KEEL_PLAYWRIGHT_MODULE` and
`KEEL_CHROMIUM_EXECUTABLE` from the operator's environment. These are trusted
installation settings, not model-generated parameters.

Official documentation:

- [Installation](https://playwright.dev/docs/intro)
- [Browser installation](https://playwright.dev/docs/browsers)
- [Accessible locators](https://playwright.dev/docs/locators)
- [Browser contexts and routing](https://playwright.dev/docs/api/class-browsercontext)

## What the rehearsal does

The command starts a server on an ephemeral `127.0.0.1` port with a fresh 256-bit
nonce, then uses actual Chromium to:

1. Observe the exact fixture origin, nonce, and account marker.
2. Find controls by exact accessible labels.
3. Fill synthetic text, a select, and a checkbox; upload synthetic résumé bytes.
4. Read back the values and browser-side attachment SHA-256.
5. Record a digest of the rendered form structure.
6. Open a fresh context, repeat the checks, and require the prepared form and
   values to match before clicking the fixture's submit button.
7. Verify the server's receipt including all received values and attachment hash.
8. Repeat the same fixture request and require the original receipt to be
   returned without a second recorded application.

The fixture stores receipts in memory. Its duplicate behavior is a test oracle,
not the production persistence layer. The server shuts down when the rehearsal
finishes. No employer is contacted. Successful output is
`LOCAL_BROWSER_REHEARSAL_PASS`; it is not a live application submission.

For the smaller HTTP-only fixture check, run
`python3 -B tools/check_local_browser.py`. This starts an ephemeral loopback
server and verifies GET, multipart upload, exact receipt contents, stable
duplicates, rejected changed payloads, foreign nonces, and foreign origins.
Its output explicitly records `rendered_browser=false`; it does not require
Playwright and cannot establish browser automation correctness.

## Python interface

```python
from keel_agent.browser import BrowserAdapter

adapter = BrowserAdapter()
prepared = adapter.prepare(contract)
# The host must check its own one-use approval and durable duplicate state.
receipt = adapter.submit_local(contract, prepared)
```

Contracts contain `mode`, `origin`, `url`, a single exact `allowed_origins`
entry, `account_id`, explicit `fields` with `label`, `kind`, and `value`, and an
optional attachment with `label`, `name`, `mime_type`, and canonical `base64`.
The local fixture additionally requires its `fixture_nonce`. Supported kinds are
`text`, `select`, and `checkbox`. Attachments are at most 5 MiB and accept PDF,
DOCX, or plain text; no filesystem path supplied by a model is dereferenced.

The prepared bundle digest binds the normalized target, expected account,
fixture nonce, field map, values, and attachment bytes. The form fingerprint
binds form action/method and the control names, types, labels, requirements,
disabled states, and select options. It excludes volatile hidden values; public
form submission is therefore not supported by this generic preparation hook.
Changing any bound value requires a new preparation. A preparation is evidence,
not human approval or an authentication credential.

## External preparation boundary

`mode=external_prepare` only supports an explicitly allowlisted public HTTPS
origin on port 443. It never clicks submit or permits POST. It requires a
server-rendered `data-keel-account-id` marker matching the expected account.
That marker is a site integration convention, **not proof of an authenticated
session**. Output explicitly reports `authenticated_account_verified=false`.
Existing job portals generally do not expose this marker, so site-specific
authenticated adapters remain unfinished.

The browser starts with no cookies, storage, credentials, permissions, or saved
profile. Page JavaScript, service workers, popups, WebSockets, redirects, and
cross-origin resources are blocked. Public DNS must resolve to permitted public
IPv4 addresses, and Chromium's resolver is pinned to the checked address. All
external network activity, including same-origin GET requests, is sealed before
private field values or attachment bytes enter the page. This also blocks
conditional CSS resource requests that could reveal form state.

This conservative adapter will not work on most dynamic authenticated ATS
forms yet. Extending it requires reviewed site adapters with session handling,
explicit account observation, employer-specific field mapping, current authority
checks, and durable reconciliation for ambiguous delivery. The current release
does not automate logins, CAPTCHAs, account creation, or real job submissions.

Browser and driver binaries execute with the operator's local privileges; the
request policy is not an operating-system sandbox. Use a dedicated host account
or container for untrusted public sites. The worker launches a fixed checked-in
module via list arguments and no shell; model output never becomes JavaScript,
selectors, shell commands, or executable paths. Raw Playwright failure logs are
not emitted because they may quote private entered values. No screenshots,
traces, cookies, or full page snapshots are retained by this adapter.

## Validation status for the packaged build

The unit tests validate contract binding, rejected targets, required preparation,
subprocess boundaries, unknown outcomes, fixture parsing, nonce isolation,
attachment checks, and duplicate receipts without network access. They mock the
browser process and are not rendered-browser evidence.

During this build, Playwright 1.62.1 was installed in the execution environment
but its Chromium binary was missing. The official browser download timed out.
The actual rehearsal returned `UNVERIFIED` with the missing-browser reason.
Consequently the rendered end-to-end path remains **unverified in this build**.
Run the one-command rehearsal on the self-host before enabling its integration.
