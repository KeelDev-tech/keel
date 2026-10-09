#!/usr/bin/env python3
"""Browser acceptance for synthetic standalone Muse review files.

Requires pinned agent-browser, axe-core 4.10.3 and a proven sandboxed Chrome.
The fixture-only mode never starts a browser and never claims browser acceptance.
No live workspace, host, provider, applicant record or submission is involved.
"""
import argparse
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
AXE_VERSION = "4.10.3"
AXE_SHA256 = "880970c081707360e64f34cea25ff91892f5bc95675b0776925b9709dd8a68bb"
SOURCE_FILES = ("keel_muse/dashboard.py", "keel_muse/review.py", "engines/score_roles.py",
                "tools/check_muse_review_ui.py", "tools/sandboxed_chrome.py",
                "tests/test_sandboxed_chrome.py", "tools/check_workbench_ui.py",
                ".github/workflows/ci.yml", "docs/MUSE_REVIEW.md")
CASES = ("v1", "complete", "incomplete", "partial", "legacy", "unknown",
         "reviewed_empty", "absent", "null", "malformed", "wrong_revision",
         "hostile", "long")
EXPECTED = {"complete": (100, 100, 100), "incomplete": (55, 100, 55),
            "partial": (90, 90, 100), "legacy": (100, 100, 100),
            "unknown": (0, 100, 0), "reviewed_empty": (100, 100, 100)}
MANDATORY_HOLD = {"incomplete": "mandatory_requirement:experience:UNKNOWN",
                  "partial": "mandatory_requirement:skills:PARTIAL",
                  "legacy": "mandatory_requirement:experience:MISSING",
                  "unknown": "mandatory_requirement:license:MISSING"}
HOSTILE = ('</script><svg onload="globalThis.__keelFitPwned=true">'
           '<img src="https://example.com/keel-inert-fixture" '
           'onerror="globalThis.__keelFitPwned=true">&\u2028\u2029')
RAW_MARKER = "UNSUPPORTED_RAW_ASSESSMENT_MUST_NOT_ENTER_HTML"


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def file_record(path):
    raw = path.read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def number(value):
    return str(int(value)) if isinstance(value, (float, int)) and value == int(value) else str(value)


def print_outcome(report):
    """Expose bounded check labels in CI logs; keep payloads in the artifact."""
    if report["status"] not in ("PASS", "FAIL", "UNAVAILABLE") or not report["checks"]:
        return
    checks = report["checks"]
    failed = [row for row in checks if row["passed"] is not True]
    print("Muse review browser acceptance: " + report["status"]
          + "; cases_requested=" + str(len(report["cases"]))
          + "; cases_observed=" + str(len({row["case"] for row in checks}))
          + "; checks=" + str(len(checks))
          + "; passed=" + str(len(checks) - len(failed)) + "; failed=" + str(len(failed)))
    source = {}
    for key in ("source_head", "source_tree"):
        value = report.get(key)
        source[key] = value if (isinstance(value, str) and len(value) == 40
            and all(char in "0123456789abcdef" for char in value)) else "unavailable"
    worktree = report.get("source_worktree", {})
    dirty = worktree.get("dirty") if isinstance(worktree, dict) else None
    source["scoped_worktree_dirty"] = dirty if type(dirty) is bool else None
    print("Muse review source: " + json.dumps(source, sort_keys=True, separators=(",", ":")))

    browser = report.get("browser", {})
    browser = browser if isinstance(browser, dict) else {}
    sandbox = browser.get("sandbox", {})
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    proof = browser.get("disconnect_proof", {})
    proof = proof if isinstance(proof, dict) else {}
    pids, formats = sandbox.get("renderer_pids"), sandbox.get("renderer_argument_formats")
    renderer_count = None
    format_names = None
    if sandbox.get("verified") is True:
        if isinstance(pids, list) and pids and all(type(pid) is int and pid > 0 for pid in pids):
            renderer_count = len(pids)
        if isinstance(formats, dict) and formats and all(
                value in ("argv", "rewritten-title") for value in formats.values()):
            format_names = sorted(set(formats.values()))
    safety = {"sandbox_verified": browser.get("sandbox_verified") is True,
              "verified_renderer_count": renderer_count, "renderer_argument_formats": format_names,
              "disconnect_proof_kind": "runtime-owned-browser-disconnect" if
                  proof.get("kind") == "runtime-owned-browser-disconnect" else "unavailable",
              "disconnect_proof_passed": proof.get("passed") is True,
              "cli_attach_failed": proof.get("cli_attach_failed") is True}
    print("Muse review safety: " + json.dumps(safety, sort_keys=True, separators=(",", ":")))
    limit = 30
    for row in failed[:limit]:
        case = " ".join(str(row["case"]).split())[:32]
        name = " ".join(str(row["name"]).split())[:180]
        print("  FAIL [" + case + "] " + name)
    if len(failed) > limit:
        print("  " + str(len(failed) - limit) + " additional failed checks retained in report.json")


def load_product():
    # Script execution needs the checkout root; importing this module is inert.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from engines.score_roles import score_role
    from keel_loki.common import digest
    from keel_muse.dashboard import write_dashboard
    from keel_muse.review import (ReviewError, example_snapshot, make_review_request,
                                  project_review, validate_review_request)
    return {name: value for name, value in locals().items() if name != "sys"}


def supported_input():
    """Synthetic scorer inputs, independent of pytest and test-module imports."""
    facts = {"operations_years": 6, "skills": ["leadership", "planning"],
             "growth_goals": ["management"], "minimum_annual_usd": 80000,
             "founder": True, "industry": ["hospitality"], "work_models": ["remote"]}
    profile = {"scoring_facts": {name: {"value": value,
               "evidence_refs": ["fixture:profile/" + name]} for name, value in facts.items()}}

    def criterion(fact, operator, value):
        return {"fact": fact, "operator": operator, "value": value,
                "source_ref": "fixture:posting/revision-1"}

    role = {"role_id": "SYNTHETIC-FIT-1", "company": "Example fixture company",
            "title": "Synthetic operations role", "requirements_reviewed": True,
            "requirements_source_ref": "fixture:posting/revision-1",
            "hard_requirements": [dict(criterion("operations_years", "gte", 4),
                                        id="experience", mandatory=True)],
            "scoring_criteria": {
                "experience_alignment": [criterion("operations_years", "gte", 5)],
                "transferable_skills": [criterion("skills", "all_of", ["leadership", "planning"])],
                "career_upside": [criterion("growth_goals", "contains", "management")],
                "compensation": [criterion("minimum_annual_usd", "lte", 100000)],
                "founder_advantage": [criterion("founder", "eq", True)],
                "industry_alignment": [criterion("industry", "contains", "hospitality")],
                "location_work_model": [criterion("work_models", "contains", "remote")],
            }}
    profile["role_assessments"] = [{"role_id": role["role_id"],
        "component": "employer_quality", "fraction": 1,
        "reviewer": {"kind": "human", "id": "fixture-reviewer"},
        "rationale": "Synthetic reviewer found all supplied employer criteria satisfied.",
        "evidence_refs": ["fixture:review/employer-quality"]}]
    return role, profile


def fixture(case, now, api):
    snapshot = api["example_snapshot"]()
    offset = now - snapshot["captured_at"]
    # Rebase explicit synthetic clocks, not the browser or production clock.
    timestamp_keys = {"captured_at", "created_at", "deadline", "observed_at", "valid_until", "at", "since"}

    def rebase(value):
        if isinstance(value, dict):
            return {key: item + offset if key in timestamp_keys and type(item) is int
                    else rebase(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebase(item) for item in value]
        return value

    snapshot = rebase(snapshot)
    snapshot["snapshot_id"] = "synthetic-browser-" + case
    first = snapshot["applications"][0]
    first["holds"] = [{"code": "CANONICAL-HOLD", "since": now - 60}]
    first["evidence"][0]["valid_until"] = now + 7200
    if case == "v1":
        return snapshot, None
    snapshot["schema"] = "keel.muse.review-snapshot.v2"
    role, profile = supported_input()
    if case == "incomplete":
        del profile["scoring_facts"]["operations_years"]
    elif case == "partial":
        role["hard_requirements"] = [{"id": "skills", "fact": "skills", "operator": "all_of",
            "value": ["leadership", "absent"], "source_ref": "fixture:posting/partial", "mandatory": True}]
    elif case == "legacy":
        role["hard_requirements"][0]["status"] = "MISSING"
    elif case == "unknown":
        role = {"role_id": "SYNTHETIC-UNKNOWN", "hard_requirements": [{"id": "license", "status": "MISSING"}]}
        profile = {}
    elif case == "reviewed_empty":
        role["hard_requirements"] = []
    elif case == "hostile":
        role["hard_requirements"][0].update(id=HOSTILE, source_ref="javascript:globalThis.__keelFitPwned=true")
        profile["role_assessments"][0].update(rationale=HOSTILE,
            evidence_refs=["javascript:globalThis.__keelFitPwned=true"])
    elif case == "long":
        role["hard_requirements"][0]["source_ref"] = "fixture:" + "r" * 2000
        profile["role_assessments"][0].update(rationale="Synthetic rationale " + "w" * 1900,
                                             evidence_refs=["fixture:" + "e" * 2000])
    scored = api["score_role"](role, profile)
    assessment = {key: deepcopy(scored[key]) for key in (
        "scoring_version", "role_id", "fit_score", "fit_score_upper", "score_coverage_percent",
        "score_bounds", "score_evidence")}
    assessment.update(schema="keel.muse.fit-assessment.v1",
        application_revision_sha256=first["application_revision_sha256"],
        blocked_reasons=deepcopy(scored["action_eligibility"]["blocked_reasons"]))
    if case != "absent":
        first["fit_assessment"] = assessment
    if case == "null":
        first["fit_assessment"] = None
    elif case == "malformed":
        assessment["raw_posting"] = RAW_MARKER
    elif case == "wrong_revision":
        assessment["application_revision_sha256"] = "0" * 64
    return snapshot, scored


class StaticDocument(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.csp = None
        self.scripts = 0
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy":
            self.csp = attrs["content"]
        if tag == "script":
            self.scripts += 1


def generate_fixtures(out, now, api):
    fixtures = {}
    for case in CASES:
        folder = out / "fixtures" / case
        folder.mkdir(parents=True)
        snapshot, scored = fixture(case, now, api)
        report = api["project_review"](snapshot, now=now,
                    expected_snapshot_sha256=api["digest"](snapshot))
        write_json(folder / "snapshot.json", snapshot)
        write_json(folder / "projection.json", report)
        if scored is not None:
            write_json(folder / "scorer-output.json", scored)
        api["write_dashboard"](folder / "review.html", report)
        doc = StaticDocument((folder / "review.html").read_text())
        fixtures[case] = {"snapshot": snapshot, "report": report, "path": folder / "review.html",
                          "csp": doc.csp, "script_count": doc.scripts}
    return fixtures


def inventory(out):
    return [file_record(path) for path in sorted(out.rglob("*")) if path.is_file()
            and path.name != "report.json"]


class Acceptance:
    def __init__(self, session, out, api, fixtures, axe):
        self.session, self.out, self.api = session, out, api
        self.fixtures, self.axe = fixtures, axe
        self.checks, self.receipts, self.observations = [], [], []
        self.case = "setup"

    def record(self, name, passed, detail=None):
        row = {"case": self.case, "name": name, "passed": bool(passed)}
        if detail is not None:
            row["detail"] = detail
        self.checks.append(row)

    def call(self, *args):
        row = {"sequence": len(self.receipts), "case": self.case, "command": list(args), "ok": False}
        self.receipts.append(row)
        try:
            result = self.session.call(*args)
            row["ok"] = True
            return result
        finally:
            row["completed_at"] = int(time.time())

    def evaluate(self, expression):
        # Retain exact observer code without expanding huge axe source in receipts.
        index = len(self.receipts)
        path = self.out / "observers" / (str(index).zfill(5) + ".js")
        path.write_text(expression, encoding="utf-8")
        row = {"sequence": index, "case": self.case, "command": ["eval", "--stdin"],
               "source": file_record(path), "ok": False}
        self.receipts.append(row)
        try:
            result = self.session.evaluate(expression)
            row["ok"] = True
            return result
        finally:
            row["completed_at"] = int(time.time())

    def screenshot(self, name):
        self.call("screenshot", str(self.out / "screenshots" / (name + ".png")), "--full")

    def open(self, case):
        self.case = case
        self.call("network", "requests", "--clear")
        # --clear returns early in the pinned CLI; a read enables tracking.
        self.call("network", "requests")
        self.call("console", "--clear")
        self.call("open", self.fixtures[case]["path"].as_uri())
        self.call("wait", "--fn", "document.querySelector('#detail-title') !== null")
        observed = self.evaluate("""(() => ({url:location.href,
            csp:document.querySelector('meta[http-equiv="Content-Security-Policy"]').content,
            data:JSON.parse(document.querySelector('#review-data').textContent),
            scripts:document.scripts.length}))()""")
        expected = deepcopy(self.fixtures[case]["report"])
        expected.pop("snapshot")
        expected["report_sha256"] = self.api["digest"](self.fixtures[case]["report"])
        self.record("actual standalone file navigation", observed["url"] == self.fixtures[case]["path"].as_uri())
        self.record("CSP unchanged", observed["csp"] == self.fixtures[case]["csp"])
        self.record("browser parsed exact whitelisted projection", observed["data"] == expected)
        self.record("no added script nodes", observed["scripts"] == self.fixtures[case]["script_count"])

    def authority(self):
        observed = self.evaluate("""(() => {const r=JSON.parse(document.querySelector('#review-data').textContent);
            return {authorized:r.execution_authorized,authenticated:r.human_identity_authenticated,
            applications:r.applications.map(a=>({id:a.application_id,status:a.status,
            holds:a.blocking_causes,approval:a.approval_state,questions:a.questions}))};})()""")
        report = self.fixtures[self.case]["report"]
        expected = [{"id": a["application_id"], "status": a["status"], "holds": a["blocking_causes"],
                     "approval": a["approval_state"], "questions": a["questions"]} for a in report["applications"]]
        self.record("authority and exact application bindings remain unchanged",
            observed == {"authorized": False, "authenticated": False, "applications": expected})
        self.record("canonical hold and stale approval retained",
            expected[0]["status"] == "BLOCKED" and "CANONICAL-HOLD" in expected[0]["holds"]
            and expected[0]["approval"] == "STALE")
        dom = self.evaluate("""(() => ({title:document.querySelector('#detail-title').textContent,
            heading:document.querySelector('#detail-head').innerText,
            causes:document.querySelector('#causes').innerText,
            body:document.querySelector('#detail-body').innerText,
            tab:document.querySelector('[role=tab][aria-selected=true]').dataset.tab,
            questions:[...document.querySelectorAll('.question h3')].map(n=>n.textContent)}))()""")
        app = next((a for a in report["applications"] if a["role"] == dom["title"]), None)
        self.record("visible application status and canonical hold remain blocked",
            app is not None and "Blocked" in dom["heading"] and "CANONICAL-HOLD" in dom["causes"])
        if app is not None:
            self.record("visible stale-approval state matches selected application",
                ("Earlier approval is stale" in dom["heading"]) == (app["approval_state"] == "STALE"))
            if dom["tab"] == "review":
                self.record("Review displays each retained canonical blocker",
                    "This application remains blocked." in dom["body"]
                    and all(cause in dom["body"] for cause in app["blocking_causes"]))
                self.record("Review keeps exact selected questions",
                    dom["questions"] == [q["prompt"] for q in app["questions"]])

    def expand(self):
        count = self.evaluate("document.querySelectorAll('#detail-body details').length")
        for index in range(count):
            selector = "#detail-body details:nth-of-type(" + str(index + 1) + ") > summary"
            self.call("focus", selector)
            self.call("press", "Enter" if index % 2 == 0 else "Space")
        self.record("all component disclosures open by real Enter/Space",
            self.evaluate("[...document.querySelectorAll('#detail-body details')].every(n=>n.open)"))

    def fit(self):
        report = self.fixtures[self.case]["report"]
        fit = report["applications"][0].get("fit_assessment")
        self.call("click", "#tab-evidence")
        self.expand()
        dom = self.evaluate("""(() => {const b=document.querySelector('#detail-body');return {
            text:b.innerText,cards:[...b.querySelectorAll('details')].map(n=>n.innerText),
            headings:[...b.querySelectorAll('h3')].map(n=>n.textContent),
            active:b.querySelectorAll('a,img,svg,iframe,form,script,object,embed').length,
            injected:globalThis.__keelFitPwned===true};})()""")
        self.record("no active nodes or hostile execution in Evidence", dom["active"] == 0 and not dom["injected"])
        if fit is None:
            self.record("v1 has no fit view", "Fit evidence" not in dom["headings"] and not dom["cards"])
            return
        self.record("fit evidence warning remains visible", "Evidence bounds are not a confidence interval" in dom["text"])
        if "score_bounds" not in fit:
            self.record("invalid or absent assessment is unavailable without scores",
                "Fit evidence unavailable" in dom["text"] and not dom["cards"]
                and "Supplied bounds:" not in dom["text"] and RAW_MARKER not in dom["text"])
            return
        if self.case in EXPECTED:
            self.record("independent numeric fixture expectation", tuple(fit[key] for key in
                ("fit_score", "fit_score_upper", "score_coverage_percent")) == EXPECTED[self.case])
        numbers = self.evaluate("""(() => {const f=JSON.parse(document.querySelector('#review-data').textContent)
            .applications[0].fit_assessment,t=document.querySelector('#detail-body').innerText;
            return t.includes('Supplied bounds: '+f.fit_score+'–'+f.fit_score_upper+' / 100')
            && t.includes('Evidence coverage: '+f.score_coverage_percent+'%');})()""")
        self.record("visible score bounds and coverage match scorer", numbers)
        self.record("visible role, scoring version and assessment binding are exact",
            "Source role: " + fit["role_id"] in dom["text"]
            and "Scoring version: " + fit["scoring_version"] in dom["text"]
            and "Assessment binding: " + fit["assessment_sha256"] in dom["text"])
        self.record("exactly nine component disclosures", len(dom["cards"]) == 9)
        for index, (component, bounds) in enumerate(fit["score_bounds"].items()):
            evidence = fit["score_evidence"][component]
            card = dom["cards"][index] if index < len(dom["cards"]) else ""
            required = [component.replace("_", " ") + " · " + number(bounds["lower"]) + "–"
                        + number(bounds["upper"]) + " points",
                        "Supported weight: " + number(bounds["known_weight"]) + " points",
                        "Method: " + evidence["method"].replace("_", " ")]
            if bounds["known_weight"] == 0:
                required.append("unknown is not a mismatch")
            if evidence["method"] == "explicit_fact_comparison":
                for criterion in evidence["criteria"]:
                    required += [criterion["id"], criterion["status"], "Reason: " + criterion["reason"],
                                 "mandatory" if criterion["mandatory"] else "optional",
                                 "Posting reference: " + (criterion["posting_source_ref"] or "Not supplied")]
                    required += ["Applicant reference: " + ref for ref in criterion["profile_evidence_refs"]]
                    if criterion["fraction"] is not None:
                        required.append("Supported fraction: " + number(criterion["credit_numerator"]) + "/" + number(criterion["credit_denominator"]))
                    if "fact" in criterion:
                        required.append("Fact: " + criterion["fact"] + " · Comparison: " + criterion["operator"])
                    if "legacy_hold" in criterion:
                        required.append("Legacy assessment hold: " + criterion["legacy_hold"])
            elif evidence["method"] == "human_assessment":
                assessment = evidence["assessment"]
                required += [assessment["rationale"], "Supplied human reviewer: " + assessment["reviewer"]["id"],
                             "Supported fraction: " + number(assessment["fraction"])]
                required += ["Review reference: " + ref for ref in assessment["evidence_refs"]]
            elif evidence["method"] == "reviewed_no_requirements":
                required.append("Posting reference: " + evidence["posting_source_ref"])
            else:
                required.append("Reason: " + evidence["reason"])
            self.record(component + " evidence details visible", all(text in card for text in required),
                        {"missing": [text for text in required if text not in card]})
        self.record("separate assessment holds retained", "Assessment holds" in dom["text"]
            and all(reason in dom["text"] for reason in fit["blocked_reasons"]))
        if self.case in MANDATORY_HOLD:
            reason = MANDATORY_HOLD[self.case]
            self.record("mandatory assessment hold remains visible despite supplied fit",
                        reason in fit["blocked_reasons"] and reason in dom["text"])
        if not fit["blocked_reasons"]:
            self.record("absence of assessment holds does not imply readiness",
                        "This does not establish application readiness" in dom["text"])
        if self.case == "unknown":
            self.record("unknown evidence remains unavailable rather than mismatch",
                "Fit evidence unavailable" in dom["text"] and "Unknown evidence is not a demonstrated mismatch" in dom["text"])
        if self.case == "hostile":
            self.record("hostile rationale and source references are literal visible text",
                        HOSTILE in dom["text"] and "javascript:globalThis.__keelFitPwned=true" in dom["text"])

    def observe(self, label):
        requests = self.call("network", "requests")
        errors = self.call("errors")
        console = self.call("console")
        self.observations.append({"case": self.case, "label": label,
                                  "requests": requests, "errors": errors, "console": console})
        rows = requests.get("requests") if isinstance(requests, dict) else None
        document_url = self.fixtures[self.case]["path"].as_uri()
        document_requests = [row for row in rows if isinstance(row, dict)
            and row.get("url") == document_url and row.get("method") == "GET"
            and str(row.get("resourceType", "")).lower() == "document"
            and isinstance(row.get("requestId"), str) and row["requestId"]] if isinstance(rows, list) else []
        # The CLI ignores Network.enable errors. An empty list cannot establish
        # working observation: this navigation must have produced a real event.
        tracking_observed = bool(document_requests)
        self.record(label + " network tracking observed current file Document request",
                    tracking_observed, {"document_url": document_url, "document_requests": document_requests})
        if isinstance(rows, list):
            unexpected = [r for r in rows if r.get("url") != document_url
                          and not str(r.get("url", "")).startswith("blob:")]
            self.record(label + " no unexpected resource or network requests",
                        tracking_observed and not unexpected,
                        {"tracking_observed": tracking_observed, "unexpected": unexpected})
        error_rows = errors.get("errors") if isinstance(errors, dict) else None
        self.record(label + " no browser page errors", error_rows == [], errors)
        messages = console.get("messages") if isinstance(console, dict) else None
        console_errors = [m for m in messages if m.get("type") in ("error", "assert")] if isinstance(messages, list) else None
        self.record(label + " no console errors or failed assertions", console_errors == [], console_errors)
        self.record(label + " CSP unchanged after interaction", self.evaluate(
            "document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]').content") == self.fixtures[self.case]["csp"])

    def keyboard_and_selection(self):
        self.call("focus", "#tab-review")
        for key, target in (("ArrowRight", "packet"), ("ArrowRight", "evidence"),
                            ("End", "timeline"), ("Home", "review"), ("ArrowLeft", "timeline"),
                            ("Home", "review")):
            self.call("press", key)
            self.record("keyboard " + key + " selects and focuses " + target, self.evaluate(
                "(() => {const t=document.getElementById(" + json.dumps("tab-" + target) + ");"
                "return document.activeElement===t&&t.getAttribute('aria-selected')==='true'"
                "&&t.tabIndex===0&&document.querySelectorAll('[role=tab][aria-selected=true]').length===1"
                "&&document.querySelector('#detail-body').getAttribute('aria-labelledby')===t.id;})()"))
        focus = self.evaluate("""(() => {const n=document.activeElement,s=getComputedStyle(n);
            function luminance(color){const rgb=color.match(/[\\d.]+/g).slice(0,3).map(Number)
                .map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);
                return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;}
            let parent=n,background='rgb(255,255,255)';
            while(parent){const value=getComputedStyle(parent).backgroundColor;
                if(value!=='rgba(0, 0, 0, 0)'&&value!=='transparent'){background=value;break;}
                parent=parent.parentElement;}
            const a=luminance(s.outlineColor),b=luminance(background);
            return {visible:n.matches(':focus-visible'),style:s.outlineStyle,width:s.outlineWidth,
                    color:s.outlineColor,background,contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};})()""")
        self.record("keyboard focus indicator is visible", focus["visible"] and focus["style"] != "none"
                    and float(focus["width"].removesuffix("px")) >= 2 and focus["contrast"] >= 3, focus)
        for repeat in range(2):
            self.call("fill", "#search", "app-02")
            self.call("click", "#queue .queue-row")
            self.record("filtered selection binds second application " + str(repeat), self.evaluate(
                "document.querySelector('#detail-title').textContent==='Customer Success Lead'"
                "&&document.activeElement.id==='detail-title'"))
            self.call("click", "#tab-evidence")
            self.record("second application never inherits first fit", self.evaluate(
                "document.querySelector('#detail-body').innerText.includes('No assessment was supplied.')"
                "&&!document.querySelector('#detail-body details')"))
            self.call("fill", "#search", "no-such-synthetic-record")
            self.record("empty filter is explicit", self.evaluate("document.querySelector('#queue').innerText.includes('No applications match')"))
            self.call("fill", "#search", "")
            self.call("select", "#status-filter", "BLOCKED")
            self.record("status filter keeps both canonical blocked records", self.evaluate("document.querySelectorAll('#queue .queue-row').length===2"))
            self.call("select", "#status-filter", "all")
            self.call("click", "#queue li:first-child .queue-row")
            self.record("clearing filter and reselecting restores exact first assessment", self.evaluate(
                "document.querySelector('#detail-title').textContent==='Operations Manager'"
                "&&document.querySelector('#detail-body').innerText.includes('Supplied bounds: 100–100 / 100')"))
            self.call("click", "#tab-review")
        self.authority()

    def axe_check(self, label):
        self.evaluate(self.axe + "\n;true")
        results = self.evaluate("""axe.run(document, {runOnly:{type:'tag',
            values:['wcag2a','wcag2aa','wcag21a','wcag21aa']}}).then(r=>({
            version:r.testEngine.version,violations:r.violations,incomplete:r.incomplete,
            passes:r.passes.map(p=>p.id)}))""")
        write_json(self.out / ("axe-" + self.case + "-" + label + ".json"), results)
        self.record(label + " axe version pinned", results["version"] == AXE_VERSION)
        self.record(label + " WCAG A/AA automated audit", not results["violations"],
                    [row["id"] for row in results["violations"]])
        contrast_unknown = [row for row in results["incomplete"] if row["id"] == "color-contrast"]
        self.record(label + " text contrast was measured", "color-contrast" in results["passes"]
                    and not contrast_unknown, contrast_unknown)

    def layout(self, width, label="expanded"):
        self.call("set", "viewport", str(width), "900")
        self.call("focus", "#tab-review" if label == "review" else "#tab-evidence")
        metrics = self.evaluate("""(() => {
            const vw=document.documentElement.clientWidth,tabs=document.querySelector('.tabs');
            const selectors=['#detail-head h2','#detail-body h3','#detail-body h4',
                '#detail-body summary','#detail-body p','#detail-body label',
                '#detail-body input','#detail-body textarea','#detail-body select',
                '#detail-body button','#detail-body .reference','#detail-body .hash',
                '#search','#status-filter','.tabs [role=tab]'];
            const nodes=[...document.querySelectorAll(selectors.join(','))];
            const rect=n=>{const r=n.getBoundingClientRect();return {
                left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
            function inspect(n){
                const r=rect(n),s=getComputedStyle(n),issues=[];
                if(!n.getClientRects().length||r.width<=0||r.height<=0||
                    s.display==='none'||['hidden','collapse'].includes(s.visibility))issues.push('not rendered');
                if(r.left < -1||r.right>vw+1)issues.push('outside horizontal viewport');
                if(n.scrollWidth>n.clientWidth+1)issues.push('own horizontal overflow');
                if(n.scrollHeight>n.clientHeight+1)issues.push('own vertical overflow');
                if(!['INPUT','TEXTAREA','SELECT'].includes(n.tagName)){
                    const range=document.createRange();range.selectNodeContents(n);
                    for(const line of range.getClientRects()){
                        if(line.left<r.left-1||line.right>r.right+1)issues.push('text exceeds horizontal box');
                        if(line.top<r.top-1||line.bottom>r.bottom+1)issues.push('text exceeds vertical box');
                    }
                }
                for(let p=n.parentElement;p&&p!==document.documentElement;p=p.parentElement){
                    const ps=getComputedStyle(p),pr=p.getBoundingClientRect();
                    const left=pr.left+p.clientLeft,top=pr.top+p.clientTop;
                    const clipX=['hidden','clip','auto','scroll'].includes(ps.overflowX);
                    const clipY=['hidden','clip','auto','scroll'].includes(ps.overflowY);
                    if(clipX&&(r.left<left-1||r.right>left+p.clientWidth+1))
                        issues.push('clipped horizontally by '+(p.id||p.className||p.tagName));
                    if(clipY&&(r.top<top-1||r.bottom>top+p.clientHeight+1))
                        issues.push('clipped vertically by '+(p.id||p.className||p.tagName));
                }
                return {tag:n.tagName,id:n.id,class:n.className,text:n.textContent.slice(0,120),
                    rect:r,clientWidth:n.clientWidth,scrollWidth:n.scrollWidth,
                    clientHeight:n.clientHeight,scrollHeight:n.scrollHeight,issues:[...new Set(issues)]};
            }
            const inspected=nodes.map(inspect),selected=tabs.querySelector('[aria-selected=true]');
            return {viewport:vw,pageWidth:document.documentElement.scrollWidth,inspected:inspected.length,
                bad:inspected.filter(n=>n.issues.length),tabs:{clientWidth:tabs.clientWidth,
                    scrollWidth:tabs.scrollWidth,scrollLeft:tabs.scrollLeft,clientHeight:tabs.clientHeight,
                    scrollHeight:tabs.scrollHeight,selected:inspect(selected),focused:inspect(document.activeElement)}};
        })()""")
        self.record(str(width) + "px page, controls and " + label + " avoid overflow and clipping",
            metrics["viewport"] == width and metrics["pageWidth"] <= width + 1 and not metrics["bad"], metrics)
        tabs = metrics["tabs"]
        self.record(str(width) + "px complete tab strip fits without hidden overflow",
            tabs["clientWidth"] > 0 and tabs["scrollWidth"] <= tabs["clientWidth"] + 1
            and tabs["scrollHeight"] <= tabs["clientHeight"] + 1, tabs)
        expected_tab = "tab-review" if label == "review" else "tab-evidence"
        self.record(str(width) + "px selected and focused tab is visible",
            tabs["selected"]["id"] == expected_tab and tabs["focused"]["id"] == expected_tab
            and not tabs["selected"]["issues"] and not tabs["focused"]["issues"], tabs)
        self.screenshot(self.case + "-" + label + "-" + str(width))

    def validate_download(self, path, response, app_id, question_id):
        data = json.loads(path.read_text())
        report = self.fixtures[self.case]["report"]
        timestamp = data.get("created_at")
        self.record("download uses actual bounded browser timestamp " + response,
            type(timestamp) is int and report["as_of"] <= timestamp <= int(time.time()) + 1)
        expected = self.api["make_review_request"](report, application_id=app_id,
            question_id=question_id, response=response, now=timestamp,
            expected_report_sha256=self.api["digest"](report))
        self.record("browser-written download matches every Python field " + response, data == expected)
        check = self.api["validate_review_request"](data, report, now=timestamp,
                    expected_current_report_sha256=self.api["digest"](report))
        self.record("download bindings validate without authority " + response,
                    check["status"] == "BINDINGS_MATCH" and check["execution_authorized"] is False)
        self.record("download preserves all no-authority flags " + response,
            data["execution_authorized"] is False and data["human_identity_authenticated"] is False
            and data["requires_authenticated_host_ingestion"] is True
            and data["canonical_writes"] == 0 and data["network_requests"] == 0)
        return data

    def rejection(self, name, request, report, now):
        try:
            self.api["validate_review_request"](request, report, now=now,
                expected_current_report_sha256=self.api["digest"](report))
        except self.api["ReviewError"] as error:
            self.record(name, True, str(error))
        else:
            self.record(name, False)

    def downloads(self):
        self.call("click", "#tab-review")
        self.record("blank approval cannot download", self.evaluate("document.querySelector('.question button').disabled"))
        approve = None
        for response in ("approve", "reject", "defer"):
            self.call("select", "#response-review-01", response)
            path = self.out / "downloads" / ("review-" + response + ".json")
            self.call("download", ".question button", str(path))
            data = self.validate_download(path, response, "app-01", "review-01")
            if response == "approve":
                approve = data
            self.record("download reports no approval " + response, self.evaluate(
                "document.querySelector('#message').textContent.includes('Nothing was sent or approved')"))
            self.authority()
        self.call("fill", "#search", "app-02")
        self.call("click", "#queue .queue-row")
        for blank in ("", "   "):
            self.call("fill", "#response-fact-02", blank)
            self.record("blank/whitespace factual answer cannot download " + repr(blank),
                        self.evaluate("document.querySelector('.question button').disabled"))
        response = "Synthetic preference: remote work."
        self.call("fill", "#response-fact-02", response)
        path = self.out / "downloads" / "review-factual.json"
        self.call("download", ".question button", str(path))
        self.validate_download(path, response, "app-02", "fact-02")
        original = self.fixtures[self.case]
        mutations = {
            "changed assessment": lambda a: a["fit_assessment"]["blocked_reasons"].append("new_observation"),
            "malformed assessment": lambda a: a["fit_assessment"].update(raw_posting="malformed change"),
            "changed question": lambda a: a["questions"][0].update(prompt="Changed exact synthetic question"),
            "changed packet": lambda a: a["packet"]["fields"][0].update(value="Changed synthetic packet"),
        }
        for name, mutate in mutations.items():
            changed = deepcopy(original["snapshot"])
            mutate(changed["applications"][0])
            report = self.api["project_review"](changed, now=original["report"]["as_of"])
            self.rejection("download rejected after " + name, approve, report, approve["created_at"])
        expires = original["snapshot"]["applications"][0]["evidence"][0]["valid_until"]
        self.rejection("download rejected at evidence expiry", approve, original["report"], expires)
        forged = deepcopy(approve)
        forged["execution_authorized"] = True
        self.rejection("download rejects forged authority", forged, original["report"], approve["created_at"])
        self.authority()

    def run(self):
        self.call("set", "viewport", "1440", "1000")
        for case in CASES:
            self.open(case)
            self.authority()
            if case == "complete":
                self.keyboard_and_selection()
                self.axe_check("review")
                for width in (1440, 390, 320):
                    self.layout(width, "review")
                self.call("set", "viewport", "1440", "1000")
            self.fit()
            if case == "complete":
                self.axe_check("expanded-evidence")
                for width in (390, 320):
                    self.layout(width)
                self.call("set", "viewport", "1440", "1000")
            self.authority()
            if case in ("complete", "unknown", "hostile", "long"):
                self.screenshot(case + "-expanded")
            if case == "long":
                for width in (1440, 390, 320):
                    self.layout(width)
            self.observe("after-evidence")
        self.call("set", "viewport", "1440", "1000")
        self.open("complete")
        self.downloads()
        self.observe("after-downloads")
        self.open("malformed")
        self.call("select", "#response-review-01", "approve")
        path = self.out / "downloads" / "review-malformed.json"
        self.call("download", ".question button", str(path))
        request = self.validate_download(path, "approve", "app-01", "review-01")
        changed = deepcopy(self.fixtures["malformed"]["snapshot"])
        changed["applications"][0]["fit_assessment"]["raw_posting"] = "another malformed value"
        report = self.api["project_review"](changed, now=self.fixtures["malformed"]["report"]["as_of"])
        self.rejection("malformed-to-malformed change invalidates browser download", request, report, request["created_at"])
        self.observe("after-malformed-download")
        # This terminates our synthetic Chrome and exercises the real stale-CDP
        # path. The session is unusable afterwards: only record/save may follow.
        proof = self.session.prove_disconnect_fails_closed()
        self.record("runtime disconnect proves stale recovery fails closed", proof.get("passed") is True, proof)


def check(args):
    out = args.out.absolute()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("output directory must be empty; existing evidence is never overwritten")
    for directory in ("observers", "screenshots", "downloads"):
        (out / directory).mkdir()
    api = load_product()
    now = int(time.time()) if args.now is None else args.now
    report = {"schema": "keel.muse.browser-acceptance.v1", "synthetic": True,
        "status": "UNAVAILABLE", "browser_render_verified": False, "execution_authorized": False,
        "fixture_time": now, "cases": list(CASES), "checks": [], "command_receipts": [],
        "scope": "standalone file:// review desk only; no host/provider/submission validation"}
    verifier = None
    try:
        report["source_files"] = [file_record(ROOT / name) for name in SOURCE_FILES]
        report["source_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["source_tree"] = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
        status = subprocess.check_output(["git", "--no-optional-locks", "status", "--porcelain=v1",
                                          "--untracked-files=all", "--", *SOURCE_FILES], cwd=ROOT, text=True)
        report["source_worktree"] = {"dirty": bool(status), "scope": list(SOURCE_FILES),
                                     "status_porcelain": status.splitlines()}
        fixtures = generate_fixtures(out, now, api)
        if args.fixtures_only:
            report["status"] = "FIXTURES_ONLY"
            report["reason"] = "Synthetic artifacts generated; no browser acceptance was attempted."
            return 0
        if not all((args.browser, args.chrome, args.axe)):
            raise ValueError("browser mode requires --browser, --chrome and --axe")
        axe = args.axe.resolve()
        metadata = json.loads((axe.parent / "package.json").read_text())
        if metadata.get("name") != "axe-core" or metadata.get("version") != AXE_VERSION:
            raise ValueError("axe-core package must be pinned to " + AXE_VERSION)
        report["axe"] = dict(file_record(axe), version=AXE_VERSION)
        if report["axe"]["sha256"] != AXE_SHA256:
            raise ValueError("axe-core source digest differs from the official pinned distribution")
        from tools.sandboxed_chrome import SandboxedChrome
        session = SandboxedChrome(args.browser, args.chrome, out)
        try:
            with session:
                verifier = Acceptance(session, out, api, fixtures, axe.read_text())
                verifier.run()
            report["status"] = "PASS" if all(row["passed"] for row in verifier.checks) else "FAIL"
            report["browser_render_verified"] = report["status"] == "PASS"
            return 0 if report["status"] == "PASS" else 1
        finally:
            report["browser"] = session.metadata
    except Exception as error:
        report["reason"] = type(error).__name__ + ": " + str(error)
        print("Muse review browser acceptance unavailable: " + report["reason"], file=sys.stderr)
        return 2
    finally:
        if verifier is not None:
            report.update(checks=verifier.checks, command_receipts=verifier.receipts,
                          observations=verifier.observations)
        report["artifacts"] = inventory(out)
        write_json(out / "report.json", report)
        print_outcome(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="pinned agent-browser executable")
    parser.add_argument("--chrome", help="installed Chrome executable")
    parser.add_argument("--axe", type=Path, help="official axe-core@4.10.3/axe.min.js")
    parser.add_argument("--out", type=Path, required=True, help="empty evidence directory")
    parser.add_argument("--fixtures-only", action="store_true", help="generate artifacts without launching a browser")
    parser.add_argument("--now", type=int, help="explicit synthetic fixture timestamp (defaults to current time)")
    args = parser.parse_args()
    try:
        return check(args)
    except (OSError, ValueError) as error:
        print("Muse review acceptance unavailable: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
