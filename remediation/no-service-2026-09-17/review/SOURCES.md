# Source and evidence notes

- Supplied `limitations-register.md`, SHA-256 recorded in `build-info.json`.
  Its live snapshot is dated 17 September 2026. No current live queue was read.
- Recovered `Keel_Gap_Build_2026-09-17.zip` and the previous MUSE text transfer.
  Source hashes are recorded in `build-info.json`; patch base hashes are in
  `patches/manifest.json`. The previous transfer is context, not an applied patch.
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html): public
  GET discovery and authenticated submission are distinct. The included fetcher
  implements only the public discovery route.
- [SQLite write-ahead logging](https://www.sqlite.org/wal.html) and
  [PRAGMA synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous):
  inform local diagnostic transaction/durability settings. The code uses WAL
  and FULL synchronization; a production power-loss recovery objective remains
  unmeasured.
- [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html):
  informs exact destination allowlisting, address validation and redirect
  refusal. This targeted fetcher is not a full outbound submission broker.

The prior baseline reproduction is a selected local suite, not an independent
adversarial audit. New local tests are offline fixtures with no provider account,
submission, CAPTCHA, approval signing key, outreach, or production deployment.
