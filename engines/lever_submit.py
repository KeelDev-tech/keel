#!/usr/bin/env python3
"""lever_submit.py — Lever automation-friendliness research verdict (read-only).

This is a DETECTION / RESEARCH-VERDICT module. It evaluates whether a
posting's Lever board is automation-friendly. No form data is constructed
here; no submission is attempted, ever.

Empirical verdict (read-only frontend inspection + endpoint probes, no
application submitted):
  1. api.lever.co/v0/postings/{org}/{id} POST -> HTTP 403
     {"ok":false,"error":"You need an API key. ..."} — the keyless
     endpoint requires an employer-generated API key, which this edition
     does not have.
  2. The hosted form's native POST target is
     jobs.lever.co/{org}/{id}/apply (multipart, server-rendered). Every
     apply page renders Lever's platform-wide hCaptcha, executed on EVERY
     submit click before the form posts. Pure HTTP cannot mint a valid
     hCaptcha token.

So Lever leads stay on the browser application path, which passes the
invisible hCaptcha natively. This module is kept as a READ-ONLY PROBE: it
parses the real apply page over HTTP, extracts every question with its
exact field name / required flag / options, and reports an
automation-friendliness verdict BEFORE a ~15-minute browser run is spent.
Anything that looks unmappable at this stage means the browser path (or a
park, per the form-intel protocol) decides next — this module never
decides that itself.

Usage:
    python3 lever_submit.py probe https://jobs.lever.co/<org>/<id>
"""

import html as htmllib
import json
import os
import re
import sys
import urllib.request

try:
    from .safe_http import urlopen as safe_urlopen
except ImportError:  # Direct script / legacy engines-on-sys.path entry points.
    from safe_http import urlopen as safe_urlopen
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ats as ats_mod  # noqa: E402 — board detection, public ATS layer

HCAPTCHA_PAT = re.compile(r"hcaptcha\.render\([^)]*sitekey", re.I)


def parse_lever_url(url):
    """(org, posting_id) from a Lever posting/apply URL. Raises ValueError."""
    org, pid = ats_mod.parse_lever((url or "").strip())
    if not org or not pid:
        raise ValueError(f"not a Lever posting URL: {url}")
    # strip a trailing /apply if present in the id capture
    pid = pid.split("/")[0]
    return org, pid


def fetch_apply_html(org, posting_id, timeout=25):
    """GET the server-rendered apply page (read-only). Raises on failure."""
    url = f"https://jobs.lever.co/{org}/{posting_id}/apply"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    })
    with safe_urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def has_hcaptcha(html):
    """True when the apply page renders hCaptcha (token required to submit)."""
    return bool(HCAPTCHA_PAT.search(html or ""))


def _card_questions(card_id, card_json):
    """Yield question dicts for one custom-question card.

    The hidden baseTemplate input carries the card's question array; the
    Nth question maps to form field cards[<cardId>][field<N>]. The card JSON
    carries its question array under "fields" (older pages used "questions").
    """
    out = []
    items = card_json.get("fields") or card_json.get("questions") or []
    for n, q in enumerate(items):
        opts = [{"label": o.get("text", ""),
                 "value": o.get("text", "")}
                for o in (q.get("options") or [])]
        out.append({
            "label": q.get("text") or "",
            "type": q.get("type") or "text",
            "required": bool(q.get("required")),
            "options": opts,
            "field_name": f"cards[{card_id}][field{n}]",
            "source": "lever-custom",
        })
    return out


def extract_questions(html):
    """Extract every submittable question from the rendered apply page.

    Returns (questions, hcaptcha_present). Each question:
      {"label", "type", "required", "options":[{"label","value"}],
       "field_name", "source"}.
    Base identity fields come first, then custom-question cards in page
    order. EEO survey fields (eeo[...]) are included with required=False —
    they are voluntary by Lever's own label.
    """
    m = re.search(r'<form id="application-form".*?</form>', html or "", re.S)
    form = m.group(0) if m else html

    questions = []
    # --- base identity fields (fixed contract, observed 2026-09-15) ---
    # Parsed per <li> block so a required marker (✱) never leaks from an
    # adjacent question's label.
    base = [
        ("name", "Full name", "text"),
        ("email", "Email", "text"),
        ("phone", "Phone", "text"),
        ("location", "Current location", "text"),
        ("org", "Current company", "text"),
        ("urls[LinkedIn]", "LinkedIn URL", "text"),
        ("resume", "Resume/CV", "file"),
    ]
    blocks = re.findall(
        r'<li class="application-question[^"]*">(.*?)</li>', form, re.S)
    for blk in blocks:
        for fname, flabel, ftype in base:
            mm = re.search(r'<(?:input|textarea|select)[^>]*name="%s"[^>]*>'
                           % re.escape(fname), blk)
            if mm and fname not in {q["field_name"] for q in questions}:
                questions.append({
                    "label": flabel, "type": ftype,
                    "required": bool(re.search(r"✱|required", blk, re.I)),
                    "options": [], "field_name": fname,
                    "source": "lever-base",
                })
    # --- custom-question cards: the hidden input carries value BEFORE name,
    # and the JSON's inner quotes are &quot;-escaped, so the first literal
    # double-quote after value=" is the attribute delimiter.
    for cm in re.finditer(
            r'value="(\{&quot;.*?\})"\s*name="cards\[([^\]]+)\]'
            r'\[baseTemplate\]"', form):
        card_id = cm.group(2)
        try:
            card = json.loads(htmllib.unescape(cm.group(1)))
        except Exception:
            continue
        questions.extend(_card_questions(card_id, card))
    # --- EEO survey (optional by Lever's own label) ---
    for em in re.finditer(r'name="(eeo\[[^\]]+\])"', form):
        questions.append({
            "label": "EEO (voluntary survey)", "type": "select",
            "required": False, "options": [],
            "field_name": em.group(1), "source": "lever-eeo",
        })
    return questions, has_hcaptcha(html)


def probe(url):
    """Read-only probe: verdict on one Lever posting URL.

    Returns a verdict dict; prints a short human summary. Raises
    ValueError on non-Lever URLs and urllib errors on fetch failure.
    """
    org, pid = parse_lever_url(url)
    html = fetch_apply_html(org, pid)
    questions, hcaptcha = extract_questions(html)
    required = [q for q in questions if q["required"]]
    verdict = {
        "org": org,
        "posting_id": pid,
        "http_path_ok": True,
        "hcaptcha_present": hcaptcha,
        "question_count": len(questions),
        "required_count": len(required),
        "required_labels": [q["label"] for q in required][:25],
        # Automation-friendliness verdict: pure-HTTP submission is not
        # viable (keyless API 403; hCaptcha on every submit). The browser
        # path passes hCaptcha natively, so this probe's verdict is always
        # "browser-path" — it never green-lights HTTP submission.
        "http_submission_viable": False,
        "recommended_path": "browser-path",
        "research_note": ("api.lever.co requires an employer API key "
                          "(403 keyless); hosted form executes hCaptcha on "
                          "every submit"),
    }
    return verdict


def main(argv):
    if len(argv) != 2 or argv[0] not in ("probe",):
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    v = probe(argv[1])
    print(f"org={v['org']} posting={v['posting_id']}")
    print(f"hcaptcha: {'PRESENT' if v['hcaptcha_present'] else 'not detected'}")
    print(f"questions: {v['question_count']} "
          f"({v['required_count']} required)")
    for lbl in v["required_labels"]:
        print(f"  - {lbl}")
    print(f"verdict: http_submission_viable={v['http_submission_viable']} "
          f"-> recommended_path={v['recommended_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
