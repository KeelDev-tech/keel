# Credentials vault

Your private account storage. **This directory is gitignored and never
packaged** (`./package.sh` refuses to build if personal data is detected in
the payload). Keep it that way.

## Convention

- One file per account. Plain text is fine — the file names tell you which
  account; the contents are what the account needs (username + a unique
  strong password per site, or the token the site issued you).
- Naming: `<service>-<site>.txt`, e.g. `ats-greenhouse.txt`, `jobboard-indeed.txt`.
- Permissions: `chmod 600 <file>` immediately after creating it. New files
  here should never be group- or world-readable.
- If you ever need to share a copy of your Keel setup with anyone, copy the
  repo **without this directory** — the whole point of the gitignore is that
  this never travels by accident.

## Template

Create a file shaped like this (replace everything with your own values):

```
site: <the site's login page URL>
username: <your login for that site>
password: <a unique strong password, generated for this site only>
notes: <anything the login flow needs, e.g. "MFA via authenticator app">
```

## Rules

1. Real secrets live ONLY here. Never in chat, never in memory notes, never
   in engine configs, never in the open repo.
2. No outreach or payment credentials get stored without your explicit word —
   Keel never spends or sends on your behalf anyway (see
   `docs/OPERATING-CONSTRAINTS.md`).
3. If a file here looks like it ended up in a zip or a repo, rotate the
   credentials it contained and start over.
