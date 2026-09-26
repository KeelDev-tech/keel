# Keel 0.13.0 receiving-agent handoff

Status: tested integration candidate, not deployed. This release adds a
normalized native-agent adapter and evaluation mechanisms over the complete
0.12 reference. All 700 predecessor files are preserved. No paid API, unofficial
Muse SDK or separate hosted service is required by the new code.

The adapter protocol belongs to Keel. It is not a claim that Muse exposes any
particular public SDK, MCP server, JavaScript runtime, browser debugging endpoint
or tool name. Native calls must be mapped to capabilities actually available in
the receiving agent's account. That mapping and real native-browser execution
have not been verified by this delivery.

## Receive and verify

The complete reference is one Python TXT transfer; no ZIP is required. On a
trusted host with Python execution available:

```sh
python3 -B Keel_0.13.0_Transfer.txt --verify-only
python3 -B Keel_0.13.0_Transfer.txt --out /existing-parent/new-keel-0.13
```

Use a new extraction directory. If the receiving agent cannot execute Python,
report that capability as unavailable and perform extraction on an authorized
host that can. Do not report an unexecuted command as successful.

Read `docs/MUSE.md` for exact commands and module contracts. The separate
Release Evidence JSON records measured checks and source/transfer hashes.
Historical audit reports retain their original scopes. Hashes establish
internal consistency; they do not authenticate Muse, a tool, a reviewer or a
source document.

## Check and port

The installer expects an unchanged 0.12 reference and verifies 699 prerequisite
files. Historical root `MANIFEST.json` is retained in the complete transfer but
is neither required nor installed on a host. Existing host files are never
overwritten.

```sh
cd /existing-parent/new-keel-0.13
python3 -B tools/install_muse.py --target /path/to/clean-0.12-reference
python3 -B tools/install_muse.py --target /path/to/clean-0.12-reference --install
```

The first command is read-only. Installation is additive and idempotent;
conflicts and changed prerequisites block it. For a modified live host or older
release, review and port the additions against that actual tree. Do not reset
real configuration merely to satisfy reference hashes. Keep all existing
consent, unaided-work, approval, hold, UNKNOWN and 429 controls in force.

Run the existing guarded suites and the new synthetic acceptance on the port:

```sh
python3 -B tools/run_muse_checks.py --out /existing-parent/new-checks
python3 -B tools/run_muse_acceptance.py --out /existing-parent/new-acceptance
```

Development test dependencies remain in `requirements-dev.txt`. A passing
reference test run is not a production deployment or live account integration.

## Map the real receiving-agent capabilities

1. Inspect the tools actually exposed in this agent/account. Record their exact
   names, input schemas and available operations without exporting credentials
   or unrelated account data. Separately establish whether Python, filesystem
   access and process execution are usable. A package or executable merely
   being present does not prove it can run.
2. Map Keel's normalized operations to those observed native capabilities.
   Required operations include accessibility snapshots and the applicable
   text, selection, radio, checkbox, attestation and attachment preparation
   actions. Use only the operations the target contract needs and the host
   actually supports. Fixture names such as `fixture.set_text` identify injected
   tests and must not be presented as real Muse tool names.
3. Use the durable JSON session in `docs/MUSE_SESSION.md` when native tools are
   callable by the agent but not by Python. Propose, issue once, invoke the actual
   tool, then observe its normalized real output. Bind each action to the current
   account, target, exact field and snapshot; preserve the typed gateway and
   human approval boundaries. An embedded callback adapter is also available
   when the host genuinely supplies callable tools. Neither path assumes a SDK.
   Native tool output must not redefine policy or grant new permissions.
4. Check attachment observations honestly. Filename or size metadata does not
   establish byte identity. Declare SHA-256 readback only when the actual host
   mechanism provides a matching observation; otherwise keep the corresponding
   requirement blocked or explicitly unavailable.
5. Exercise the adapter on authorized fixtures before using real applicant
   records or employer pages. Preserve actual snapshots, normalized requests,
   returned observations and capability/version bindings. A configured manifest
   is a host declaration, not proof that native tools were exercised.

Unsupported required capabilities remain blocked. Do not invent an endpoint,
tool name, browser session or successful readback to complete the mapping. The
native adapter does not require Playwright merely because an earlier fixture
runner used it; use the receiving host's genuine supported mechanism.

No public Sentinel permission-receipt API has been verified. In the ordinary
native-tool session, `NATIVE_TOOL_ENFORCES_PERMISSION` delegates permission
enforcement to the actual tool; it grants nothing. Never manufacture an `allow`
JSON receipt. The actual platform call must retain its own pause/deny controls,
and Keel human approvals must still come from the trusted canonical host. Honor
the returned dispatch deadline; uncertain or interrupted effects become UNKNOWN
and are never replayed. Record returned halt events in the canonical host before
other sessions. The JSON session does not claim it wrote those global holds.

## Interpret the measurements

Synthetic and injected comparisons test the measurement code and control
boundaries. They do not establish real model performance, native-browser
reliability, factual truth, research validity or competitive superiority.
Preserve dataset, configuration and plan bindings; retain failed, missing and
abstained cases when interpreting reports.

Real-model measurements require an available authorized runtime and genuine
reviewed evaluation data. Supply authentic source revisions and human decisions
from their authoritative stores. No new report or adapter capability substitutes
for those records, and no code path in this package grants submission authority.

Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED separately.
Keep local reference installation separate from live account integration.
This delivery leaves account Muse integration NOT_VERIFIED, actual native
browser execution NOT_RUN, real model inference NOT_RUN and production
deployment false.
