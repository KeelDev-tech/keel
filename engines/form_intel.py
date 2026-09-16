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
import json, os, re, sys, urllib.request

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
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


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


def probe_url(url):
    m = re.search(r"job-boards\.greenhouse\.io/embed/job_app\?for=([^&]+)&token=(\d+)", url)
    if m:
        return greenhouse_embed_intel(m.group(1), m.group(2))
    m = re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if m:
        # resolve board -> embed token form
        return greenhouse_embed_intel(m.group(1), m.group(2))
    m = re.search(r"lever\.co/([^/]+)/([a-f0-9-]+)", url)
    if m:
        return lever_intel(m.group(1), m.group(2))
    return {"ats": "unknown", "form_url": url, "questions": [],
            "rendered_option_fetch_needed": [],
            "note": "No HTTP-level extraction for this ATS; agent must read the rendered form with browser.open."}


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
