# Security Policy

## Reporting a vulnerability

Report vulnerabilities via **GitHub Security Advisories** on this
repository (the "Report a vulnerability" button under the Security tab).
Private disclosure keeps the details out of public view while the issue
is assessed and fixed.

Please do **not** open a public issue for a suspected vulnerability.

## Scope

This policy covers **this public repository only**.

The private execution layer described in `SPLIT.md` is out of scope by
design: it is not published here, so vulnerabilities in it cannot be
reported against this repo.

## What to include

- A description of the vulnerability and the impact you believe it has.
- Steps to reproduce, or a proof-of-concept if one is safe to share.
- The version/commit you tested against.

## What NOT to include

Never include personal data in a report — no names, emails, phone
numbers, addresses, credentials, or real employer names. Redact sample
logs and configs before sending. A report missing redacted details is
better than one carrying personal data.

## Response

Reports are reviewed on a **best-effort basis**; there is **no SLA**
promised. If the report is accepted, a fix will be committed to the
public repo and noted in `CHANGELOG.md`. Reporters will be credited in
the changelog entry unless they ask not to be.
