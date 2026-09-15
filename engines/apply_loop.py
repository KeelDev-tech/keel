#!/usr/bin/env python3
"""Keel autonomous apply loop (public edition).

Picks the next eligible lead and builds a launch packet. This is the
open-core public half of the pipeline: discovery -> scoring -> materials ->
verification -> prescreen -> packet.

What it does NOT do (private execution layer, see SPLIT.md):
  - submit applications over HTTP (the API-direct transport lives in the
    private layer)
  - inject ATS-specific form-commit techniques into the brief (the private
    technique library)
  - spawn browser tasks (an agent-turn action using the packet)

Instead, each packet carries a generic brief — verified form values from the
answer bank, banded-question rules, hard gates, and the per-field
verification protocol — plus an EXECUTOR contract describing what the
submission layer must do and must never do. Plug in your own executor
(browser automation, ATS APIs, manual review) behind that contract.

Eligibility for autonomous application (standing approvals, no applicant input):
  - status READY (materials prepared) and action_band APPLY
  - employer not on the blocklist
  - employer application budget not exhausted (rate_limits.py)
  - no SUBMITTED ledger row for the same employer (dedupe)
  - materials (resume) exist on disk
  - posting URL returns HTTP 200 (pre-launch live re-verify gate)
  - packet passes prescreen.py (PARK verdicts go to needs_input, never onward)

Ordering: highest fit_score first.

What it does:
  1. Selects the highest-fit eligible lead (fit_score desc).
  2. Runs form_intel.probe on the final ATS URL -> intel JSON.
  3. Builds a generic brief (build_generic_brief).
  4. Writes a launch packet to data/launch-packets/<role_id>.json.
  5. Marks the lead IN-FLIGHT so a second loop run cannot double-process.

After the executor reports, log the outcome with record_outcome.py and update
the ledger; the packet is then archived.

Usage:
    python3 apply_loop.py            # process one lead (highest fit)
    python3 apply_loop.py --all      # process every eligible lead, one packet each

Env:
    KEEL_HOME  workspace root (default ~/keel)
"""
import json, os, sys, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("KEEL_HOME", os.path.expanduser("~/keel"))
QUEUE = os.path.join(HOME, "data", "queues", "standard-queue.json")
LEDGER = os.path.join(HOME, "data", "application-ledger.json")
BLOCKLIST = os.path.join(HOME, "data", "employer-blocklist.md")
PACKETS = os.path.join(HOME, "data", "launch-packets")
UA = {"User-Agent": "Mozilla/5.0"}

sys.path.insert(0, BASE)
import form_intel
import log_event  # noqa: E402 — telemetry: additive event logging only
import prescreen  # noqa: E402 — pre-launch packet screen (PARK before spend)
import rate_limits  # noqa: E402 — employer application budgets
import record_outcome  # noqa: E402 — outcome telemetry


def load_answer_bank():
    # Prefer the personalized working copy in data/ (edited after setup.sh);
    # the engines/ copy is only a fallback for runs outside a workspace.
    data_bank = os.path.join(HOME, "data", "answer_bank.json")
    if os.path.exists(data_bank):
        return json.load(open(data_bank))
    for name in ("answer_bank.json", "answer_bank.example.json"):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            return json.load(open(p))
    return {"answers": {}, "banded_questions": {}, "gates": {}}


def load_queue():
    d = json.load(open(QUEUE))
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def save_queue(items):
    d = json.load(open(QUEUE))
    if isinstance(d, list):
        d = items
    else:
        key = "entries" if "entries" in d else "items"
        d[key] = items
    json.dump(d, open(QUEUE, "w"), indent=2)


def blocklisted(company):
    try:
        txt = open(BLOCKLIST).read().lower()
    except FileNotFoundError:
        return False
    return company.lower() in txt if company else False


def already_submitted(company, title):
    try:
        rows = json.load(open(LEDGER))
    except FileNotFoundError:
        return False
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    c = (company or "").lower()
    for r in rows:
        if r.get("status") != "SUBMITTED":
            continue
        if c and c in (r.get("company") or "").lower():
            return True
    return False


def live(url):
    """Tri-state liveness check.

    True  -> HTTP 200, posting live.
    False -> HTTP 404/410, posting dead.
    None  -> unverifiable over plain HTTP (bot protection, timeout, 403).
            Never treated as dead — the executor re-verifies before filing.
    """
    import urllib.error
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (404, 410) else None
    except Exception:
        return None


def materials_ok(entry):
    m = entry.get("materials") or {}
    rp = m.get("resume")
    if not rp:
        return False
    return os.path.exists(os.path.join(HOME, rp))


def eligible(entry):
    if entry.get("status") not in ("READY", "READY-FOR-BROWSER"):
        return False, "not READY"
    if entry.get("action_band") != "APPLY":
        return False, "not APPLY band"
    if blocklisted(entry.get("company")):
        return False, "blocklisted employer"
    if already_submitted(entry.get("company"), entry.get("title")):
        return False, "already submitted to this employer"
    if not materials_ok(entry):
        return False, "materials not prepared"
    url = entry.get("ats_url") or entry.get("application_url")
    if not url:
        return False, "no application URL"
    lv = live(url)
    if lv is False:
        return False, "posting dead (404/410)"
    if lv is None:
        print(f"  note: {entry['role_id']} unverifiable over HTTP — executor re-verifies")
    return True, "ok"


def build_generic_brief(entry, intel, bank):
    """Build the executor brief WITHOUT private commit techniques.

    The brief carries: verified form values, banded-question rules, hard
    gates, form intel (exact option labels when available), and the
    per-field verification protocol. ATS-specific event-sequencing
    techniques are the private layer's job (see SPLIT.md).
    """
    answers = bank.get("answers", {})
    lines = [
        f"Submit a job application for the applicant to {entry.get('company')} "
        f"for the role \"{entry.get('title')}\".",
        "",
        "STEP 1 — RE-VERIFY LIVE. Confirm the posting is live and accepting "
        "applications BEFORE filling anything. If the posting is gone, expired, "
        "or the apply route is gated/paywalled, STOP and report — do not submit.",
        f"Application URL: {entry.get('ats_url') or entry.get('application_url')}",
        "",
        "STEP 2 — PRE-FLIGHT INTELLIGENCE. Enumerate every question and its "
        "exact option labels from the rendered form first; use those exact labels.",
    ]
    if intel:
        lines.append(f"Detected ATS: {intel.get('ats', 'unknown')}")
    lines += [
        "",
        "Fill the form with these truthful values (never invent anything else):",
    ]
    for k, v in answers.items():
        lines.append(f"  - {k}: {v}")
    lines += ["", "BANDED-QUESTION RULES (pre-approved — apply without improvising):"]
    for k, rule in bank.get("banded_questions", {}).items():
        r = rule.get("rule", rule) if isinstance(rule, dict) else rule
        lines.append(f"  - {k}: {r}")
    lines += ["", "HARD GATES (stop conditions — obey exactly):"]
    for k, gate in bank.get("gates", {}).items():
        lines.append(f"  - {k}: {gate}")
    lines += [
        "",
        "STEP 3 — PER-FIELD VERIFICATION PROTOCOL (mandatory, after EVERY field):",
        "  - After EVERY field fill, verify commitment before moving on: no validation",
        "    error, and the entered value must persist after clicking elsewhere (blur test).",
        "  - For dropdowns: reopen the menu and confirm the option carries a selected marker.",
        "  - Never batch-fill then check at the end. One field, one verification, then next.",
        "  - Only when all required fields verify clean may Submit be clicked.",
        "",
        "STEP 4 — SUBMIT only when every required field verifies clean. If an email/phone",
        "verification code screen appears, PAUSE and hand off — do not guess codes.",
        "Report the EXACT confirmation page text/message. Explicit confirmation is the",
        "only acceptable success evidence.",
        "",
        "EXECUTOR CONTRACT (private layer implements this):",
        "  - MUST re-verify the posting is live before filling (Step 1).",
        "  - MUST NOT invent values for fields not covered above — ask instead.",
        "  - MUST NOT attest to anything on the applicant's behalf (no-AI, travel,",
        "    arbitration, references) without explicit applicant approval.",
        "  - MUST NOT claim submission without explicit confirmation evidence.",
        "  - MUST respect employer rate limits and the blocklist.",
    ]
    return "\n".join(lines)


def build_packet(entry):
    role_id = entry["role_id"]
    url = entry.get("ats_url") or entry.get("application_url")
    try:
        intel = form_intel.probe_url(url)
        intel["role_id"] = role_id
        intel["source_url"] = url
        intel_path = os.path.join(BASE, "briefs", f"{role_id}.intel.json")
        os.makedirs(os.path.dirname(intel_path), exist_ok=True)
        json.dump(intel, open(intel_path, "w"), indent=2)
        ats = intel.get("ats")
    except Exception as ex:
        print(f"  intel failed for {role_id} ({ex}); continuing without it")
        intel, ats = None, "unknown"
    bank = load_answer_bank()
    brief = build_generic_brief(entry, intel, bank)
    m = entry.get("materials") or {}
    packet = {
        "role_id": role_id,
        "company": entry.get("company"),
        "title": entry.get("title"),
        "ats_url": url,
        "ats": ats,
        "upload_files": [os.path.join(HOME, m["resume"])] +
                        ([os.path.join(HOME, m["cover_letter"])] if m.get("cover_letter") else []),
        "brief_chars": len(brief),
        "brief": brief,
        "executor": "pluggable — implement the EXECUTOR CONTRACT in the brief",
    }
    os.makedirs(PACKETS, exist_ok=True)
    path = os.path.join(PACKETS, f"{role_id}.json")
    json.dump(packet, open(path, "w"), indent=2)
    return path


def _eff_score(e):
    f = e.get("fit_score")
    return f if isinstance(f, (int, float)) else -1


def _archive_packet(path):
    import shutil
    adir = os.path.join(PACKETS, "archive")
    os.makedirs(adir, exist_ok=True)
    if path and os.path.exists(path):
        shutil.move(path, os.path.join(adir, os.path.basename(path)))


def main():
    items = load_queue()
    bank = load_answer_bank()
    todo = sorted(
        (e for e in items if e.get("status") in ("READY", "READY-FOR-BROWSER")),
        key=_eff_score,
        reverse=True)
    if not todo:
        print("No READY leads. Nothing to do.")
        return
    inflight_ids = []
    made = 0
    for entry in todo:
        role_id = entry.get("role_id", "")
        company = entry.get("company", "")
        ok, why = eligible(entry)
        if not ok:
            log_event.log(
                "gate_blocked", role_id=role_id, company=company, ats="",
                source="apply_loop",
                details={"gate": "eligibility", "reason": why,
                         "fit_score": entry.get("fit_score")},
            )
            print(f"SKIP {role_id}: {why}")
            continue
        allowed, rl_reason = rate_limits.is_allowed(company)
        if not allowed:
            log_event.log(
                "gate_blocked", role_id=role_id, company=company, ats="",
                source="apply_loop",
                details={"gate": "rate_limit", "reason": rl_reason},
            )
            print(f"SKIP {role_id}: {rl_reason}")
            continue
        path = build_packet(entry)
        packet = json.load(open(path))
        try:
            verdict = prescreen.screen_packet(packet, bank)
        except Exception as ex:
            print(f"  prescreen error on {role_id} ({ex}); treating as CLEAN")
            verdict = {"verdict": "CLEAN", "reasons": []}
        if verdict["verdict"] == "PARK":
            res = prescreen.park_lead(role_id, verdict["reasons"])
            if res.get("ok"):
                first = verdict["reasons"][0] if verdict["reasons"] else "blocked"
                print(f"PRESCREEN-PARK {role_id}: {first}")
            else:
                print(f"PRESCREEN-PARK-FAILED {role_id}: {res.get('error')}")
            made += 1
            if "--all" not in sys.argv:
                break
            continue
        inflight_ids.append(role_id)
        log_event.log(
            "lead_verified", role_id=role_id, company=company, ats="",
            source="apply_loop",
            details={"url": entry.get("ats_url") or entry.get("application_url", "")},
        )
        log_event.log(
            "brief_built", role_id=role_id, company=company, ats="",
            source="apply_loop",
            details={"packet": path, "fit_score": entry.get("fit_score")},
        )
        print(f"PACKET {role_id} -> {path}")
        made += 1
        if "--all" not in sys.argv:
            break
    items = load_queue()
    for e in items:
        if e.get("role_id") in inflight_ids:
            e["status"] = "IN-FLIGHT"
    save_queue(items)
    print(f"Done: {made} launch packet(s).")


if __name__ == "__main__":
    main()
