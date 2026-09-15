# Sweep worker snippet — API-direct candidacy flagging (canonical)

Paste this block into every discovery sweep worker prompt (it is the
systematic replacement for the old ad-hoc "flag when observable"
instruction). It keeps the detection rule in exactly one place
(`engines/ats/api_direct_detect.py`); workers only report
what they observe, the module decides.

---

## API-direct candidacy flag (set on EVERY lead)

For each lead you record, set the field `api_direct_candidate` (boolean) in
your worker JSON output:

- `true` ONLY when the posting's application URL is a **direct Greenhouse
  board URL** — `job-boards.greenhouse.io/<board>/jobs/<id>` or the older
  `boards.greenhouse.io/<board>/jobs/<id>` form — AND the posting page shows
  **no reCAPTCHA Enterprise marker** (no `recaptcha/.../enterprise.js`
  script in the page HTML).
- `false` for everything else: Lever, Ashby, Workday, employer career pages
  (even ones carrying a `gh_jid` param — those are NOT directly submittable),
  aggregator URLs, Greenhouse EU hosts (`job-boards.eu.greenhouse.io`), and
  any URL you could not verify.

When in doubt, set `false`. The browser path is the default; API-direct is
an earned fast lane, and a false `true` only costs one wasted dry-run, but
a false `false` just means the browser handles it as usual.

The pipeline re-verifies this flag at READY-promotion time
(`engines/pipeline/verify_retry.py`) and at launch time (`apply_loop.py`), so your flag is a
hint, not a commitment — but set it carefully anyway; it drives the
apply loop's ordering (api-direct leads file in ~1 min vs ~15 min browser).

Do NOT invent the flag for leads you did not check. `false` is always safe.

## NEW-ATS RADAR

If you encounter an application platform / ATS vendor that is NOT one of
{greenhouse, lever, ashby, workday, icims, smartrecruiters, jobvite, breezy,
rippling, tealhq, applytojob, workatastartup} — e.g. a new ATS hostname in a
posting URL — flag it in your sweep output as `new_ats: <platform name>` with
1-2 real posting URLs. The monthly capability radar
(`engines/ats/edge_probe.py --scan-only`) auto-detects and
probes unfamiliar platforms and onboards them to
`engines/ats/edge_case_registry.json` as "unknown" until evidence lands, so anything you
miss is still caught — but flagging it now gets it probed sooner.

---

## REQUIRED: application_url capture

For every lead you record, the field `application_url` is REQUIRED.

- It must be the **live, verified individual application/posting URL** —
  the page where the application is actually filed (ATS posting page or
  employer-direct apply link). Board, listing, search, company-careers, and
  aggregator-index URLs do not count.
- **Verify before recording:** HTTP GET the URL (or confirm via the posting's
  ATS board API) — a 404/410 or removed/expired markers mean it is NOT a
  live URL.
- A lead with no captured URL **may not be emitted as URL-bearing**. If you
  saw the posting but could not capture a live individual URL, still record
  the lead but set `url_capture_failed` to the reason — e.g.
  "aggregator mirrors only", "JS-rendered apply button, no direct link",
  "auth wall before apply page" — instead of silently emitting a URL-less
  lead. Never leave the field silently empty, and never invent a URL.
  (The pipeline's verify_retry worker can sometimes recover the URL from
  board APIs — but only if it knows the capture failed, so the flag matters.)
