#!/usr/bin/env python3
"""breezy_preflight.py — Breezy automation-friendliness pre-flight (read-only).

This is a MINIMAL DRY-RUN / READ-ONLY PROBE. It evaluates whether a
posting's Breezy apply page is automation-friendly. No form data is
constructed here; no mapping is performed; no submission is attempted,
ever.

Concept (not ported): the private pipeline layers a set of hard gates
on top of a probe like this before any submission — e.g. refuse when the
lead is already parked for the applicant's input, refuse on a CAPTCHA
wall, refuse when any REQUIRED form question cannot map to a verified
value, require the resume to exist and carry the canonical email, and
emit honeypot fields EMPTY (a filled honeypot is a bot signal). Those
mapping/submission gates belong to the private submission machinery and
are documented here as the design philosophy only: this public edition
stops at the read-only probe.

What the probe reports for a Breezy posting URL:
  - whether the apply page renders a CAPTCHA wall (recaptcha / hCaptcha /
    turnstile / data-sitekey markers)
  - the extracted question set: base identity fields, per-position
    questionnaire questions, and honeypot inputs (flagged, source
    "breezy-honeypot"), with required flags corroborated by the SSR
    field-config JSON when present
  - the questionnaire flag and field-config summary
  - the endpoint-verification checklist (what must be confirmed before
    any "verified path" claim is made about Breezy)

Usage:
    python3 breezy_preflight.py probe https://<tenant>.breezy.hr/p/<id>-<slug>
"""

import html as htmllib
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# https://<tenant>.breezy.hr/p/<positionId>-<title-slug>[/apply]
BREEZY_URL_PAT = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.breezy\.hr/p/([a-f0-9]+)-?", re.I)

CAPTCHA_PAT = re.compile(
    r"recaptcha|hcaptcha|turnstile|enterprise\.recaptcha|data-sitekey", re.I)

# Bot-mitigation honeypots: observed as hp_<hex> on a real Breezy apply
# page. The rule is always SUBMIT EMPTY — a filled honeypot is a bot
# signal. This probe only flags them; it never fills anything.
HONEYPOT_PAT = re.compile(r"^hp_[0-9a-f]+$", re.I)
_HONEYPOT_NAME_PAT = re.compile(r"hp_[0-9a-f]+", re.I)

# The SSR HTML carries i18n placeholder keys (real labels are injected by
# the portal's translate script at render). Map the known keys to plain
# English so the probe's question labels read true.
I18N_LABELS = {
    "%HEADER_PERSONAL_DETAILS%": "Personal details",
    "%HEADER_COVER_LETTER%": "Cover letter",
    "%PLACEHOLDER_FULL_NAME%": "Full name",
    "%PLACEHOLDER_EMAIL_ADDRESS%": "Email address",
    "%PLACEHOLDER_PHONE_NUMBER%": "Phone number",
    "%ERROR_INVALID_FORM_RESUME%": "Resume",
}


def _breezy_verification_checklist():
    """What must happen before 'breezy' joins any verified submission path.

    Informational: the public edition never submits, but the checklist
    documents what verification would require — read it as the standard
    any submitter must meet, not as a roadmap this module follows.
    """
    return [
        "1. Confirm the endpoint contract from the live frontend: POST "
        "https://app.breezy.hr/api/apply/<positionId> with the exact field "
        "contract (cName/cEmail/cPhoneNumber/cResume multipart parts, "
        "honeypot-empty semantics, form_token freshness) from 3+ tenant "
        "apply pages — do not trust a REST-client reading alone.",
        "2. Confirm the per-position questionnaire question encoding: how "
        "custom questions surface on the SSR apply page (or prove they do "
        "not) for questionnaire_in_experience=true postings.",
        "3. Run a dry-run assembly against 3+ live Breezy postings and "
        "confirm question mapping parses without invention.",
        "4. Only then: one supervised live submission with explicit "
        "confirmation, logged through the outcome-recording layer.",
    ]


def parse_breezy_url(url):
    """(tenant, position_id, slug) from a Breezy posting/apply URL.

    Raises ValueError for non-Breezy URLs.
    """
    m = BREEZY_URL_PAT.match((url or "").strip())
    if not m:
        raise ValueError(f"not a Breezy posting URL: {url}")
    tenant, position_id = m.group(1), m.group(2)
    # full /p/<positionId>-<title> slug, minus any /apply suffix
    slug = re.sub(r"/apply/?$",
                  "",
                  (url or "").strip().split("/p/", 1)[1].split("?")[0])
    return tenant, position_id, slug


def fetch_apply_html(tenant, slug, timeout=25):
    """GET the server-rendered apply page (read-only). Raises on failure."""
    url = f"https://{tenant}.breezy.hr/p/{slug}/apply"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def has_captcha(html):
    """True when the apply page renders a CAPTCHA wall marker."""
    return bool(CAPTCHA_PAT.search(html or ""))


def api_root_for_tenant(tenant):
    """Reproduce the REST client's constructApiUrl() for tenant hosts.

    For <tenant>.breezy.hr (3-label hostname) the client's own logic
    resolves to https://app.breezy.hr + '/api'. Documented here so the
    probe's report names the real endpoint target.
    """
    return "https://app.breezy.hr/api"


def _field_config(html):
    """Parse the SSR field-config JSON ({name: required, ..., cover_letter:
    required, questionnaire_in_experience: bool}). Returns (config, flag)."""
    config, qflag = {}, False
    blobs = re.findall(r"\{&quot;[^}]{200,}?\}", html or "")
    for blob in sorted(blobs, key=len, reverse=True):
        try:
            d = json.loads(htmllib.unescape(blob))
        except Exception:
            continue
        if isinstance(d, dict) and "cover_letter" in d \
                and "questionnaire_in_experience" in d:
            config = {k: v for k, v in d.items()
                      if k != "questionnaire_in_experience"}
            qflag = bool(d.get("questionnaire_in_experience"))
            break
    return config, qflag


def _labelize(key):
    return I18N_LABELS.get(key, key.strip("%").replace("_", " ").title())


def _extract_balanced_json(s, key_pos):
    """Extract the JSON object containing key_pos from string s.

    Scans backwards from key_pos to the object's opening brace, then
    forward with brace-depth counting (string-aware). Returns the parsed
    dict, or None.
    """
    depth = 0
    start = None
    for i in range(key_pos - 1, max(-1, key_pos - 20000), -1):
        c = s[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        return None
    return None


def _questionnaire_questions(html):
    """Extract per-position custom questions from SSR questionnaire markup.

    Returns a list of question dicts (may be empty). The exact encoding is
    tenant-dependent; this parser covers JSON blobs shaped like
    {"questions": [{"text", "required", "options": [...]}, ...]} or the
    "fields" variant. Callers treat an empty result as "no questionnaire
    questions visible on the SSR page".
    """
    out = []
    seen = set()
    plain = htmllib.unescape(html or "")
    for m in re.finditer(r'"(?:questions|fields)"\s*:\s*\[', plain):
        d = _extract_balanced_json(plain, m.start())
        if not isinstance(d, dict):
            continue
        items = d.get("questions") or d.get("fields") or []
        for n, q in enumerate(items):
            if not isinstance(q, dict):
                continue
            text = q.get("text") or q.get("label") or q.get("question") or ""
            if not text or text in seen:
                continue
            seen.add(text)
            opts = [{"label": str(o.get("text", o) if isinstance(o, dict)
                                   else o),
                     "value": str(o.get("value", o.get("text", o)))
                     if isinstance(o, dict) else o}
                    for o in (q.get("options") or q.get("choices") or [])]
            out.append({
                "label": text,
                "type": q.get("type") or "text",
                "required": bool(q.get("required")),
                "options": opts,
                "field_name": q.get("name") or q.get("id")
                or f"breezy_question_{n}",
                "source": "breezy-questionnaire",
            })
    return out


# Fixed contract observed on the SSR apply page: input name ->
# (English label key, input type).
_BASE_CONTRACT = [
    ("cName", "%PLACEHOLDER_FULL_NAME%", "text"),
    ("cEmail", "%PLACEHOLDER_EMAIL_ADDRESS%", "email"),
    ("cPhoneNumber", "%PLACEHOLDER_PHONE_NUMBER%", "text"),
    ("cResume", "%ERROR_INVALID_FORM_RESUME%", "file"),
    ("smsConsent", "SMS consent", "checkbox"),
    ("cCoverLetter", "%HEADER_COVER_LETTER%", "textarea"),
]


def extract_questions(html):
    """Extract every submittable question from the rendered apply page.

    Returns (questions, captcha_present, field_config, questionnaire_flag).
    Each question:
      {"label", "type", "required", "options":[{"label","value"}],
       "field_name", "source"}.
    Honeypot inputs are flagged (source "breezy-honeypot") so any mapper
    always emits them empty. Required-ness prefers the per-input `required`
    attribute, corroborated by the SSR field-config JSON when present.
    """
    m = re.search(r'<form name="form".*?</form>', html or "", re.S)
    form = m.group(0) if m else html

    config, qflag = _field_config(html)
    config_state = {  # config value -> required?
        "required": True, "optional": False, "hidden": False}

    questions = []
    seen = set()
    for fname, flabel, ftype in _BASE_CONTRACT:
        if fname in seen:
            continue
        mm = re.search(r'<(?:input|textarea|select)[^>]*name="%s"[^>]*>'
                       % re.escape(fname), form)
        if not mm:
            continue
        seen.add(fname)
        tag = mm.group(0)
        required = bool(re.search(r"\brequired\b", tag, re.I))
        # corroborate with the SSR field-config (maps cName->name etc.)
        ckey = {"cName": "name", "cEmail": "email_address",
                "cPhoneNumber": "phone_number", "cResume": "resume",
                "cCoverLetter": "cover_letter",
                "smsConsent": "sms_consent"}.get(fname)
        if ckey in config and ckey in config_state:
            required = config_state[config[ckey]] or required
        questions.append({
            "label": _labelize(flabel), "type": ftype, "required": required,
            "options": [], "field_name": fname, "source": "breezy-base",
        })

    # honeypots: flagged, never mapped to a real value
    for hm in re.finditer(
            r'<input[^>]*name="(%s)"[^>]*>' % _HONEYPOT_NAME_PAT.pattern,
            form):
        hn = hm.group(1)
        if hn in seen:
            continue
        seen.add(hn)
        questions.append({
            "label": "honeypot (submit empty)", "type": "honeypot",
            "required": False, "options": [], "field_name": hn,
            "source": "breezy-honeypot",
        })

    questions.extend(_questionnaire_questions(html))
    return questions, has_captcha(html), config, qflag


def probe(url):
    """Read-only probe: verdict on one Breezy posting URL.

    Returns a verdict dict; raises ValueError on non-Breezy URLs and
    urllib errors on fetch failure.
    """
    tenant, position_id, slug = parse_breezy_url(url)
    html = fetch_apply_html(tenant, slug)
    questions, captcha, config, qflag = extract_questions(html)
    required = [q for q in questions if q["required"]]
    honeypots = [q for q in questions if q["source"] == "breezy-honeypot"]
    return {
        "tenant": tenant,
        "position_id": position_id,
        "http_path_ok": True,
        "captcha_present": captcha,
        "endpoint_target": f"{api_root_for_tenant(tenant)}/apply/{position_id}",
        "question_count": len(questions),
        "required_count": len(required),
        "required_labels": [q["label"] for q in required][:25],
        "honeypot_count": len(honeypots),
        "field_config": config,
        "questionnaire_flag": qflag,
        # Read-only probe: nothing here is a submission decision. The
        # private gates (mapping + submission) are not ported; this
        # edition's verdict is always informational.
        "recommended_path": "browser-path (or manual review)",
        "verification_checklist": _breezy_verification_checklist(),
    }


def main(argv):
    if len(argv) != 2 or argv[0] != "probe":
        print("usage: breezy_preflight.py probe "
              "https://<tenant>.breezy.hr/p/<id>-<slug>")
        return 2
    v = probe(argv[1])
    print(f"tenant={v['tenant']} position={v['position_id']}")
    print(f"captcha: {'PRESENT' if v['captcha_present'] else 'not detected'}")
    print(f"questions: {v['question_count']} "
          f"({v['required_count']} required), "
          f"honeypots: {v['honeypot_count']}")
    for lbl in v["required_labels"]:
        print(f"  - {lbl}")
    print(f"questionnaire_flag: {v['questionnaire_flag']}")
    print(f"verdict: read-only probe -> {v['recommended_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
