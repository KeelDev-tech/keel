# Local rendered browser skill qualification

The browser laboratory extends `SkillWorkshop` with a separate rendered
qualification path. Existing fixture replay continues to operate on in-memory
references; it is not browser evidence. This laboratory uses an installed local
Playwright/Chromium pair, a newly created browser context, and an ephemeral
`127.0.0.1` HTTP server. It never downloads dependencies, uses a paid service,
visits an employer, imports browser cookies, or loads an external credential.

## What the implemented corpus proves

The fixed reviewed fixture contains an ordinary text field and an attachment.
Its recipe is scoped to `https://browser-lab.invalid`, a synthetic account,
workspace, and role. That URL is an identity label; the browser only navigates to
a nonce-protected local fixture URL. Recipe values are fixed synthetic data.
Arbitrary recipes, other scopes, additional fields, consent, and attestations are
rejected. Qualifying this recipe cannot qualify an employer integration.

The renderer reads the actual DOM before every action and after preparation,
checks origin, nonce, control inventory, monotonically increasing snapshot
sequence and per-request challenge, and reads text back. Attachment readback
hashes the browser's actual `File` bytes with SHA-256; metadata alone does not
pass. No form submission operation is exposed.

All seven cases are required:

| Case | Expected outcome |
| --- | --- |
| Baseline | Two fields prepared and exact DOM/file readback |
| Changed layout | Reordered controls still prepare with semantic locators |
| Changed schema | Unexpected password control stops before any effect |
| Stale snapshot | Replayed snapshot stops before any effect |
| Disconnect | Connection closes after first effect; UNKNOWN, no retry |
| HTTP 429 | Actual fixture HTTP 429 stops before any effect |
| Redirect | External redirect is denied before a preparation action |

Faults are explicitly injected into the local fixture or transport. This is a
regression corpus, not an independently held-out benchmark. Origin routing and
CSP provide application-level restrictions; they do not prove OS-level network
containment. A compromised host or replaced Python module is outside the
workshop's trust boundary.

## Run locally

Use your independently installed free Python Playwright package and Chromium.
If needed on your own development host, their standard installation is
`python -m pip install playwright` followed by
`python -m playwright install chromium`. Review and pin dependencies through your
normal environment management. The lab does not install them on your behalf.

From the source root:

```bash
python -m keel_loki.browser_lab --home /absolute/path/to/new-browser-workshop
```

The directory must be new. The command creates a quarantined synthetic recipe,
runs qualification, and writes an append-only workshop event containing the
result. It does not promote automatically. Exit code 0 means the complete
rendered corpus passed; 2 means it did not. If dependencies, the browser, or
loopback binding are unavailable, the result is explicitly `BLOCKED` and cannot
be promoted. The current build environment lacks Python Playwright and Chromium;
its actual qualification attempt returned `BLOCKED` with zero browser actions.

For a host-owned workshop:

```python
from keel_loki.browser_lab import demo_trace, source_revision, LAB_SCOPE, FORM_REVISION
from keel_loki.skills import SkillWorkshop

workshop = SkillWorkshop("/absolute/path/to/private-workshop")
recipe = workshop.quarantine(demo_trace(now), skill_id="synthetic-form", expires_at=now + 3600)
result = workshop.qualify_rendered("synthetic-form", recipe["version"], now=now,
                                  expected_source_revision=source_revision())
# Only do this after a real PASS, using its internally generated event digest.
workshop.promote_rendered("synthetic-form", recipe["version"], now=now,
                         qualification_sha256=result["qualification_sha256"])
active = workshop.active_rendered("synthetic-form", scope=LAB_SCOPE,
                                 form_revision=FORM_REVISION, now=now)
```

Use a trusted current integer Unix timestamp for `now`. A source digest covers
lab policy, transport, workshop promotion logic, and HTML fixture. Qualification
pins that digest before and after execution. Changed source, recipe, scope, form,
expiry, rollback, or a subsequent qualification attempt invalidates use of the
old qualification. A new passing trial needs an explicit new promotion.

`promote_rendered` accepts an internally persisted event digest, not a caller's
PASS object. `active_rendered` is independent of the legacy `active` API. Every
result preserves `execution_authorized=false` and `submission_authorized=false`.
Synthetic field references are not human approvals or authenticated facts;
production action and approval gates remain separate.

## Validation boundary

`tests/test_browser_lab.py` uses deterministic transports to exercise rejection,
readback, uncertain effects, source binding, expiry, requalification, and
promotion logic. These tests do not demonstrate that a browser ran. The opt-in
integration test requires an actual installed browser:

```bash
KEEL_RENDERED_BROWSER_LAB=1 python -m pytest -q tests/test_browser_lab.py -k actual_rendered_corpus
```

That test fails if the real corpus cannot pass. Default regression runs skip it
rather than presenting synthetic transport results as rendered evidence.
