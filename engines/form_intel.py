#!/usr/bin/env python3
"""Pre-launch form intelligence.

Probes the final application page over HTTP (no browser slot burned) and
extracts what the form actually asks BEFORE a browser task is spawned:

  Greenhouse embed (job-boards.greenhouse.io/embed/job_app):
      question labels, field types, question IDs — server-rendered.
      NOTE: react-select option lists load client-side; the agent must read
      them with browser.open and pass them in via --intel (see below).
  Lever (api.lever.co/v0/postings/<org>/<id>):
      full custom questions WITH options from the public API.
  Ashby (api.ashbyhq.com):
      application form fields from the posting API where exposed.

Usage:
    python3 form_intel.py <role_id>            # reads queue, resolves final ATS URL
    python3 form_intel.py --url <final_url>    # probe a URL directly

Output: intel.json next to the brief, with:
    {"ats": ..., "questions": [{"label","type","question_id","options":[...]}],
     "rendered_option_fetch_needed": [labels...]}

Rendered-option workflow (agent turn, uses browser.open):
    1. Run form_intel.py <role_id> -> intel file.
    2. browser.open the ATS page, read each combobox's exact option labels.
    3. Merge options into the intel JSON (or pass a small options.json).
    4. apply_loop.py consumes the intel file when building launch packets.
       option labels; the browser task selects from verified labels, never
       guessing.
"""
import copy
import json, os, re, sys, time
from urllib.parse import urlsplit, parse_qs

import http_cache  # shared short-TTL GET cache (pre-request admission +
                   # durable 429 cooldowns, same semantics as verify)

BASE = os.path.dirname(os.path.abspath(__file__))
from keel_paths import DATA  # noqa: E402
QUEUE = os.path.join(DATA, "queues", "standard-queue.json")
UA = {"User-Agent": "Mozilla/5.0"}


def has_hcaptcha(html):
    """Read-only hCaptcha detection on a hosted apply page's HTML.

    True when the page renders hCaptcha (a token is required to submit).
    Public detection knowledge: carrier-marker strings only
    (hcaptcha.com / h-captcha render markers), no submission logic.
    """
    low = (html or "").lower()
    return ("hcaptcha.com" in low or "h-captcha" in low
            or "hcaptcha.render" in low)


def fetch_apply_html(org, posting_id):
    """GET Lever's hosted apply page HTML. Raises on any failure."""
    return fetch(f"https://jobs.lever.co/{org}/{posting_id}/apply")


def fetch(url):
    body, _, _ = http_cache.fetch(url, timeout=25)
    return body.decode("utf-8", "replace")


def greenhouse_embed_intel(board, token):
    """Extract server-rendered question structure from the GH embed form."""
    url = f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={token}"
    h = fetch(url)
    questions, need_rendered = [], []
    # Walk each label; classify by the control that immediately follows it.
    labels = [(m.group(1), m.end()) for m in re.finditer(r"<label[^>]*>(.*?)</label>", h, re.S)]
    for raw, end in labels:
        label = re.sub(r"<[^>]+>", "", raw).strip()
        label = re.sub(r"\s+", " ", label)
        if not label or len(label) > 200:
            continue
        low = label.lower()
        if any(k in low for k in ("attach", "enter manually", "resume", "cover letter", "dropbox")):
            continue  # materials controls — handled separately in the brief
        if any(q["label"] == label for q in questions):
            continue
        nxt = h[end:end + 1200]
        if 'select-shell' in nxt[:600]:
            qid = re.search(r"react-select-(question_\d+)", nxt[:600])
            questions.append({
                "label": label,
                "type": "dropdown",
                "question_id": qid.group(1) if qid else None,
                "options": [],
            })
            need_rendered.append(label)
        elif re.search(r"<(input|textarea)[ >]", nxt[:300]):
            questions.append({"label": label, "type": "text", "options": []})
    return {"ats": "greenhouse", "form_url": url, "questions": questions,
            "rendered_option_fetch_needed": need_rendered}


def lever_intel(org, posting_id):
    d = json.loads(fetch(f"https://api.lever.co/v0/postings/{org}/{posting_id}"))
    qs = []
    for q in d.get("customQuestions", []) or []:
        qs.append({
            "label": q.get("text"),
            "type": q.get("type"),
            "options": (q.get("options") or []),
            "required": q.get("required"),
        })
    # hCaptcha carrier: the prescreen CAPTCHA marker keys on a "[captcha] ..."
    # line in FORM INTEL, but no writer populated it (documented carrier gap).
    # Lever's hosted apply page renders hCaptcha platform-wide (token required
    # to submit); the API posting body never mentions it. Fetch the hosted
    # page and inject the marker when present — this is what lets prescreen
    # PARK a Lever lead pre-build instead of burning browser-minutes on a
    # full fill. Best-effort: any failure skips the marker and intel still
    # succeeds. (Pure read-only detection; no submission logic.)
    try:
        if has_hcaptcha(fetch_apply_html(org, posting_id)):
            qs.append({"label": "[captcha] hCaptcha at submit",
                       "type": "captcha", "options": [], "required": True})
    except Exception:
        pass
    return {"ats": "lever", "form_url": d.get("hostedUrl"),
            "questions": qs, "rendered_option_fetch_needed": []}


def _probe_url_uncached(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username is not None or
            parsed.password is not None or parsed.port not in (None, 443)):
        raise ValueError('public HTTPS form URL required')
    greenhouse_host = parsed.hostname in {'job-boards.greenhouse.io', 'boards.greenhouse.io'}
    if greenhouse_host and parsed.path == '/embed/job_app':
        query = parse_qs(parsed.query)
        boards, tokens = query.get('for', []), query.get('token', [])
        if (len(boards) != 1 or len(tokens) != 1 or
                not re.fullmatch(r'[A-Za-z0-9_-]+', boards[0]) or
                not re.fullmatch(r'[0-9]+', tokens[0])):
            raise ValueError('unambiguous Greenhouse board and token required')
        return greenhouse_embed_intel(boards[0], tokens[0])
    m = re.fullmatch(r'/([A-Za-z0-9_-]+)/jobs/([0-9]+)/?', parsed.path)
    if greenhouse_host and m:
        # resolve board -> embed token form
        return greenhouse_embed_intel(m.group(1), m.group(2))
    m = re.fullmatch(r'/([A-Za-z0-9_-]+)/([a-f0-9-]+)(?:/apply)?/?', parsed.path)
    if parsed.hostname == 'jobs.lever.co' and m:
        return lever_intel(m.group(1), m.group(2))
    if re.search(r"[?&]gh_jid=\d+", url or ""):
        # gh_jid branch: employer career URLs carry the Greenhouse job id
        # as gh_jid while the host's first DNS label is (usually) the board
        # token. Use the SAME canonical guesser the verify path uses, so
        # form intel can never drift from the path that verifies. A wrong
        # guess fail-closes downstream (embed fetch 404s -> exception, same
        # as every other branch above). Lazy import keeps this low-level
        # module's import graph unchanged; no guess -> fall through to
        # unknown-ATS rather than inventing a verdict.
        try:
            from verify_retry import _gh_jid_guesses
            guesses = _gh_jid_guesses([url])
        except Exception:
            guesses = []
        if guesses:
            return greenhouse_embed_intel(guesses[0][0], guesses[0][1])
    from html_form import inspect_html
    passive = inspect_html(fetch(url), url)
    return {"ats": "unknown", "form_url": url, "questions": [],
            "rendered_option_fetch_needed": [],
            "source": "passive_html_inspection", "advisory": True,
            "extraction_complete": False, "passive_intel": passive,
            "note": "Passive HTML hints are advisory only; inspect the rendered form before use."}


# Process-memory memoization is scoped to the exact URL and employer.
# Different postings may have different required questions on the same board;
# one role's form must never substitute for another's. TTL is 600 seconds.
# Cache-internal errors always fail OPEN to a fresh normal probe — a
# broken cache must never block or poison a packet build. Callers that
# mutate the returned intel get a deep copy, so caller mutation can
# never poison the cached entry. A cached observation is still not proof of
# current rendered-form completeness or a grant to submit an application.
_FORM_INTEL_MEMO_TTL = 600
_form_intel_memo = {}  # (exact_url, employer) -> (expires_monotonic, intel_dict)


def _memo_board_key(url):
    """Legacy diagnostic grouping only; never an authorization or cache key."""
    try:
        u = url or ""
        m = re.search(r"job-boards\.greenhouse\.io/embed/job_app\?for=([^&]+)&token=(\d+)", u)
        if m:
            return "greenhouse:" + m.group(1).lower()
        m = re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", u)
        if m:
            return "greenhouse:" + m.group(1).lower()
        m = re.search(r"lever\.co/([^/]+)/([a-f0-9-]+)", u)
        if m:
            return "lever:" + m.group(1).lower()
        if re.search(r"[?&]gh_jid=\d+", u):
            try:
                from verify_retry import _gh_jid_guesses
                guesses = _gh_jid_guesses([u])
            except Exception:
                guesses = []
            if guesses:
                return "greenhouse:" + str(guesses[0][0]).lower()
        host = re.search(r"https?://([^/]+)", u)
        if host:
            return "host:" + host.group(1).lower()
    except Exception:
        pass
    return "unknown"


def probe_url(url, employer=None):
    """Probe a posting's form; results memoized per exact (URL, employer).

    employer: the caller's employer string. Combined with the exact URL
    it forms the memo key. When employer is omitted or blank the memo is
    bypassed entirely and the call behaves exactly as the pre-memo
    probe_url(url) — this keeps ad-hoc/CLI callers on the legacy path.
    Signature is backward compatible: probe_url(url) behaves exactly as
    before.
    """
    employer_key = (employer or "").strip().lower()
    if not employer_key:
        return _probe_url_uncached(url)
    memo_key = (url, employer_key)
    try:
        hit = _form_intel_memo.get(memo_key)
        if hit is not None and hit[0] > time.monotonic():
            return copy.deepcopy(hit[1])
    except Exception:
        pass  # cache-internal error: fail open to a fresh probe
    intel = _probe_url_uncached(url)
    try:
        if len(_form_intel_memo) >= 512:
            _form_intel_memo.pop(next(iter(_form_intel_memo)))
        _form_intel_memo[memo_key] = (time.monotonic() + _FORM_INTEL_MEMO_TTL,
                                      copy.deepcopy(intel))
    except Exception:
        pass  # fail open: a broken cache never blocks the build
    return intel


def probe_with_employer(url, employer):
    """probe_url with optional employer context, tolerant of strict doubles.

    Calls probe_url(url, employer=employer); when the installed probe_url
    is a strict replacement accepting only url (test doubles, external
    shims), retries without the employer context instead of failing the
    packet build. A TypeError raised from INSIDE a real probe still
    surfaces from the retry (same uncached code path, memo bypassed), so
    genuine failures are never masked — only the signature mismatch is
    absorbed.
    """
    try:
        return probe_url(url, employer=employer)
    except TypeError:
        return probe_url(url)


def merge_options(intel, options_map):
    """Merge agent-read option labels (from browser.open) into the intel."""
    for q in intel.get("questions", []):
        if q["label"] in options_map:
            q["options"] = options_map[q["label"]]
            if q["label"] in intel.get("rendered_option_fetch_needed", []):
                intel["rendered_option_fetch_needed"].remove(q["label"])
    return intel


def main():
    args = sys.argv[1:]
    out_dir = os.path.join(BASE, "briefs")
    os.makedirs(out_dir, exist_ok=True)
    if args and args[0] == "--url":
        url, role_id = args[1], "adhoc"
    else:
        role_id = args[0] if args else sys.exit("usage: form_intel.py <role_id> | --url <url>")
        q = json.load(open(QUEUE))
        items = q if isinstance(q, list) else q.get("entries", q.get("items", []))
        role = next((e for e in items if e["role_id"] == role_id), None)
        if not role:
            sys.exit(f"role {role_id} not found in queue")
        url = role.get("ats_url") or role.get("application_url")
    intel = probe_url(url)
    intel["role_id"] = role_id
    intel["source_url"] = url
    path = os.path.join(out_dir, f"{role_id}.intel.json")
    json.dump(intel, open(path, "w"), indent=2)
    n = len(intel["questions"])
    nr = len(intel["rendered_option_fetch_needed"])
    print(f"Wrote {path}: {n} questions, {nr} need rendered-option fetch.")


if __name__ == "__main__":
    main()
