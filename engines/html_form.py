#!/usr/bin/env python3
"""Passive stdlib HTML form inspection (K49 port, 2026-09-17).

Server-rendered label/option extraction from a fetched page. Read-only and
fail-closed: never executes scripts, never reads field values, never reads
hidden controls or form action URLs. Absence of a control here says nothing
about a JavaScript-rendered form; completeness is always unknown.

WIRING STATUS (2026-09-18): ADVISORY-ONLY wiring is live. form_intel's
unknown-ATS fallback calls inspect_html and stores the result under
intel['passive_intel'] with advisory=True; intel['questions'] stays [].
The non-authoritative rendering path is form_intel.is_authoritative_intel
+ authoritative_questions, enforced at the two gating consumers:
prescreen.render_probe_brief (pre-promotion screen) and
apply_loop._form_questions (T4 bundle form_fingerprint). No consumer may
read passive_intel['questions'] as a park/block/fill signal.
"""
from html.parser import HTMLParser

MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_NODES = 50000
MAX_CONTROLS = 500
MAX_OPTIONS_PER_SELECT = 1000
MAX_LABEL_CHARS = 1000

# Controls whose content is never form evidence.
_SKIPPED_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}
_IGNORED_SUBTREES = {"script", "style", "template"}


def _clean(text):
    return " ".join(text.split())[:MAX_LABEL_CHARS]


class _FormParser(HTMLParser):
    """Collect visible form controls with their labels. No values kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls = []
        self.labels_by_for = {}
        self._open_label = None
        self._open_select = None
        self._open_option = None
        self._ignored_depth = 0
        self._nodes = 0

    def handle_starttag(self, tag, attrs):
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise ValueError("HTML node limit exceeded")
        if tag in _IGNORED_SUBTREES:
            self._ignored_depth += 1
        if self._ignored_depth:
            return
        attrs = dict(attrs)
        if tag == "label":
            self._open_label = {"for": attrs.get("for"), "text": [],
                                "control": None}
        elif tag in {"input", "select", "textarea"}:
            kind = attrs.get("type", "text") if tag == "input" else tag
            if kind in _SKIPPED_INPUT_TYPES:
                return
            if len(self.controls) >= MAX_CONTROLS:
                raise ValueError("form control limit exceeded")
            control = {
                "id": attrs.get("id"),
                "name": attrs.get("name"),
                "type": kind,
                "required": ("required" in attrs
                             or attrs.get("aria-required") == "true"),
                "label": attrs.get("aria-label", ""),
                "options": [],
            }
            self.controls.append(control)
            if self._open_label is not None:
                self._open_label["control"] = control
            if tag == "select":
                self._open_select = control
        elif tag == "option" and self._open_select is not None:
            self._open_option = []

    def handle_data(self, data):
        if self._ignored_depth:
            return
        if self._open_label is not None and self._open_select is None:
            self._open_label["text"].append(data)
        if self._open_option is not None:
            self._open_option.append(data)

    def handle_endtag(self, tag):
        if tag in _IGNORED_SUBTREES:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "label" and self._open_label is not None:
            text = _clean("".join(self._open_label["text"]))
            if self._open_label["for"]:
                self.labels_by_for[self._open_label["for"]] = text
            if self._open_label["control"] is not None:
                self._open_label["control"]["label"] = text
            self._open_label = None
        elif (tag == "option" and self._open_select is not None
                and self._open_option is not None):
            text = _clean("".join(self._open_option))
            if len(self._open_select["options"]) < MAX_OPTIONS_PER_SELECT:
                self._open_select["options"].append(text)
            self._open_option = None
        elif tag == "select":
            self._open_select = None
            self._open_option = None


def inspect_html(html, url):
    """Inspect one HTML page's visible form controls (stdlib only).

    Returns a form_intel-shaped dict with extraction_complete=False: the
    result is a partial, non-authoritative hint -- scripts, hidden controls,
    and client-rendered fields are never inspected. Raises ValueError on
    invalid or oversized input (fail-closed).
    """
    if not isinstance(html, str) or len(html) > MAX_HTML_BYTES:
        raise ValueError("invalid or oversized HTML")
    parser = _FormParser()
    parser.feed(html)
    parser.close()
    for control in parser.controls:
        control["label"] = (parser.labels_by_for.get(control["id"])
                            or control["label"]
                            or "Unlabelled control")
        if control["required"] and not control["label"].endswith("*"):
            control["label"] += "*"
    return {
        "ats": "unknown",
        "form_url": url,
        "questions": parser.controls,
        "source": "passive_html_inspection",
        "extraction_complete": False,
        "rendered_option_fetch_needed": [
            q["label"] for q in parser.controls
            if q["type"] == "select" and not q["options"]
        ],
        "note": ("Public page data only. Partial inspection: check the "
                 "rendered form; scripts and hidden controls are not "
                 "inspected."),
    }
