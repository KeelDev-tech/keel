"""Fixed synthetic browser qualification corpus; never application authorization.

The transport is imported internally. Caller-supplied PASS reports cannot qualify
recipes. Deterministic transport tests exercise policy only, not rendered proof.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import secrets
from .common import clone, digest, require_dict, require_hash, require_int

LAB_SCOPE = {"workspace_id": "browser-lab", "origin": "https://browser-lab.invalid",
             "account_id": "synthetic-account", "role_id": "synthetic-role"}
ATTACHMENT = b"Keel synthetic browser laboratory attachment. No personal information.\n"
VALUES = {"name": "Synthetic Applicant", "resume": {
    "name": "synthetic-resume.txt", "mime_type": "text/plain", "size": len(ATTACHMENT),
    "sha256": hashlib.sha256(ATTACHMENT).hexdigest()}}
FIELDS = [{"field_id": "name", "control": "text"},
          {"field_id": "resume", "control": "attachment"}]
FORM_REVISION = digest(FIELDS)
CASES = {"baseline": ("PREPARED", "exact_readback", 2),
         "changed_layout": ("PREPARED", "exact_readback", 2),
         "changed_schema": ("BLOCKED", "form_changed", 0),
         "stale_snapshot": ("BLOCKED", "stale_snapshot", 0),
         "disconnect": ("UNKNOWN", "transport_disconnected", 1),
         "rate_limit": ("BLOCKED", "http_429", 0),
         "redirect": ("BLOCKED", "unexpected_navigation", 0)}


def source_revision():
    """Pin reviewed fixture, transport, qualification, and promotion source bytes."""
    root = Path(__file__).resolve().parent
    files = ["browser_lab.py", "playwright_adapter.py", "skills.py", "fixtures/browser_lab.html"]
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files})


def demo_trace(now):
    require_int(now)
    return {"trace_id": "synthetic-browser-lab", "scope": clone(LAB_SCOPE),
            "form_revision": FORM_REVISION, "observed_at": now,
            "observations": [{"action": "fill_approved" if f["control"] == "text" else "attach_approved",
                **f, "value_ref": digest(VALUES[f["field_id"]]),
                "readback_ref": digest(VALUES[f["field_id"]])} for f in FIELDS]}


def validate_recipe(recipe, now):
    """This corpus qualifies only its explicit synthetic scope and value refs."""
    require_int(now)
    if recipe.get("scope") != LAB_SCOPE or recipe.get("form_revision") != FORM_REVISION:
        raise ValueError("browser_lab_scope_or_form_mismatch")
    if not recipe["created_at"] <= now < recipe["expires_at"]:
        raise ValueError("browser_lab_recipe_expired_or_future")
    trace = demo_trace(now)
    expected = [{k: v for k, v in row.items() if k not in ("control", "readback_ref")}
                for row in trace["observations"]]
    # Exact scoped field inventory, order, actions and values; no arbitrary JS,
    # selectors, credentials, destinations, consent or attestation fields.
    if recipe.get("steps") != expected or recipe.get("preconditions") != FIELDS:
        raise ValueError("browser_lab_recipe_not_in_fixed_corpus")


class Halt(ValueError):
    pass


def _snapshot(transport, *, nonce, origin, previous, challenge):
    observed = transport.snapshot(challenge)
    require_dict(observed, {"challenge", "sequence", "nonce", "origin", "fields", "submitted"})
    if (observed["challenge"] != challenge or type(observed["sequence"]) is not int
            or observed["sequence"] <= previous):
        raise Halt("stale_snapshot")
    if observed["origin"] != origin or observed["nonce"] != nonce:
        raise Halt("unexpected_navigation")
    if observed["fields"] != FIELDS or observed["submitted"] is not False:
        raise Halt("form_changed")
    return observed


def _drive(transport, recipe, *, nonce, origin):
    """Policy runner. Injection here is deliberately not qualification evidence."""
    actions, previous, observations = 0, 0, []
    try:
        status = transport.open()
        if status == 429:
            raise Halt("http_429")
        if status != 200:
            raise Halt("unexpected_navigation")
        first = _snapshot(transport, nonce=nonce, origin=origin, previous=previous,
                          challenge=secrets.token_hex(16))
        previous = first["sequence"]
        observations.append(digest(first))
        for step in recipe["steps"]:
            current = _snapshot(transport, nonce=nonce, origin=origin, previous=previous,
                                challenge=secrets.token_hex(16))
            previous = current["sequence"]
            observations.append(digest(current))
            # Count attempts before effect: failure cannot safely imply no effect.
            actions += 1
            transport.apply(step["field_id"], clone(VALUES[step["field_id"]]),
                            base64.b64encode(ATTACHMENT).decode() if step["field_id"] == "resume" else None)
        final = _snapshot(transport, nonce=nonce, origin=origin, previous=previous,
                          challenge=secrets.token_hex(16))
        observations.append(digest(final))
        readback = transport.readback()
        if readback != VALUES:
            raise Halt("readback_mismatch")
        if transport.denied_requests or transport.submission_attempts:
            raise Halt("unexpected_navigation")
        return {"status": "PREPARED", "reason": "exact_readback", "action_attempts": actions,
                "snapshot_sha256": observations, "readback": readback,
                "readback_sha256": digest(readback), "automatic_retry": False}
    except Halt as exc:
        return {"status": "UNKNOWN" if actions else "BLOCKED", "reason": str(exc),
                "action_attempts": actions, "snapshot_sha256": observations, "automatic_retry": False}
    except Exception:
        # Transport errors never authorize retry. Raw exception text may expose
        # host details and is not included in portable evidence.
        return {"status": "UNKNOWN" if actions else "BLOCKED", "reason": "transport_disconnected",
                "action_attempts": actions, "snapshot_sha256": observations, "automatic_retry": False}


def run_rendered(recipe, *, now, expected_source_revision):
    """Run the installed local browser; no adapter/report injection parameter.

    Missing optional browser dependencies give BLOCKED, not a simulated PASS.
    This consumes synthetic fixture data only and never visits recipe.origin.
    """
    require_hash(expected_source_revision)
    validate_recipe(recipe, now)
    revision = source_revision()
    if expected_source_revision != revision:
        raise ValueError("browser_lab_source_revision_changed")
    from .playwright_adapter import run_cases
    rows = run_cases(recipe)
    passing = len(rows) == len(CASES) and all(
        row.get("case_id") in CASES and row.get("browser_started") is True and
        (row.get("status"), row.get("reason"), row.get("action_attempts")) == CASES[row["case_id"]]
        for row in rows) and len({row["case_id"] for row in rows}) == len(CASES)
    # Source replacement during qualification invalidates the whole result.
    unchanged = source_revision() == revision
    return {"schema": "keel.browser_qualification.v1", "recipe_sha256": digest(recipe),
            "source_revision": revision, "form_revision": FORM_REVISION, "scope": clone(LAB_SCOPE),
            "observed_at": now, "status": "PASS" if passing and unchanged else "BLOCKED" if
            all(row.get("status") == "UNAVAILABLE" for row in rows) else "FAIL",
            "rows": rows, "rendered_browser_verified": passing and unchanged,
            "synthetic": True, "authentic_application_validated": False,
            "os_network_containment_verified": False, "fixture_independence_verified": False,
            "execution_authorized": False, "submission_authorized": False}


def main(argv=None):
    import argparse
    import json
    import time
    from .skills import SkillWorkshop
    parser = argparse.ArgumentParser(description="Run fixed local browser qualification; no downloads or external sites")
    parser.add_argument("--home", required=True, help="Fresh private workshop directory")
    args = parser.parse_args(argv)
    if Path(args.home).exists():
        parser.error("--home must be a fresh directory")
    now = int(time.time())
    workshop = SkillWorkshop(args.home)
    recipe = workshop.quarantine(demo_trace(now), skill_id="synthetic-form", expires_at=now + 3600)
    report = workshop.qualify_rendered(recipe["skill_id"], recipe["version"], now=now,
                                       expected_source_revision=source_revision())
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
