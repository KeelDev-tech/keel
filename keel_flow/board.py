"""One deterministic operating report from a complete, bounded canonical export."""
from collections import Counter
from . import __version__
from .common import keys, version, text, boolean, fresh, clock, require, digest, records
from .attempts import reconcile
from .holds import review_holds
from .readiness import inventory
from .forecast import forecast
from .discovery import allocate
from .questions import prioritize
from .outcomes import cohorts
from .bridge import compare_supply, inspect_attempts


def _assurance_inventory(snapshot, ready, envelope, *, now):
    """Bind an assurance observation to this exact export, then intersect gates.

    Hash binding establishes consistency, not issuer identity or authenticity.
    No advisory result may clear canonical holds or authorize an external action.
    """
    ready["base_executable_ready"] = ready["executable_ready"]
    ready["base_nominal_to_actual_loss"] = ready["nominal_to_actual_loss"]
    ready["base_blocked_reasons"] = dict(ready["blocked_reasons"])
    for row in ready["rows"]:
        row["base_executable"] = row["executable"]
        row["assurance_checks_passed"] = None
    if envelope is None:
        ready["assurance_qualified_ready"] = None
        ready["basis"] = "legacy export checks only; cognitive assurance NOT_CONFIGURED; live lane must recheck all gates"
        return {"state": "NOT_CONFIGURED", "assurance_qualified_ready": None,
                "checks_passed_count": None, "covered_roles": 0,
                "missing_role_ids": sorted(row["role_id"] for row in ready["rows"]),
                "rows": [], "execution_authorized": False,
                "reason": "No assurance envelope supplied; legacy readiness is not assurance-qualified."}

    from keel_assurance.core import evaluate
    require(type(envelope) is dict, "assurance envelope object required")
    require(envelope.get("source_revision") == snapshot["source_revision"], "assurance source revision mismatch")
    require(envelope.get("export_sha256") == digest(snapshot), "assurance export digest mismatch")
    leads = {row["role_id"]: row for row in snapshot["leads"]}
    covered = set()
    for action in records(envelope.get("actions"), "assurance actions"):
        rid = text(action.get("role_id"), "assurance role_id")
        require(rid in leads, "assurance action references unknown role")
        require(rid not in covered, "duplicate assurance role_id")
        covered.add(rid)
        lead = leads[rid]
        require(action.get("application_id") == lead["identity"], "assurance application identity mismatch")
        require(action.get("lead_sha256") == digest(lead), "assurance lead digest mismatch")
        require(action.get("packet_dependency_hash") == lead.get("packet_dependency_hash"),
                "assurance packet dependency hash mismatch")
    evaluated = evaluate(envelope, now=now)
    require(type(evaluated) is dict and evaluated.get("state") in {"CHECKS_PASSED", "REVIEW_REQUIRED", "UNVERIFIED"},
            "invalid assurance evaluator state")
    checked = {}
    for row in records(evaluated.get("rows"), "assurance evaluated rows"):
        rid = text(row.get("role_id"), "assurance evaluated role_id")
        require(rid in covered and rid not in checked, "unexpected or duplicate assurance result role")
        boolean(row.get("checks_passed"), "assurance checks_passed")
        require(type(row.get("reasons")) is list and all(type(reason) is str and reason for reason in row["reasons"]),
                "assurance reasons must be a string array")
        checked[rid] = row
    require(set(checked) == covered, "assurance evaluator did not account for every action")
    missing = sorted(set(leads) - covered)
    globally_verified = evaluated["state"] != "UNVERIFIED"
    for row in ready["rows"]:
        check = checked.get(row["role_id"])
        passed = check is not None and check["checks_passed"] and globally_verified
        row["assurance_checks_passed"] = bool(passed)
        if check is None:
            row["reasons"].append("missing_assurance")
        elif not passed:
            row["reasons"].append("assurance_not_passed")
            row["reasons"].extend("assurance:" + reason for reason in check["reasons"])
            if not globally_verified:
                row["reasons"].append("assurance_unverified")
        row["reasons"] = sorted(set(row["reasons"]))
        row["executable"] = bool(row["base_executable"] and passed)
    actual = sum(row["executable"] for row in ready["rows"])
    ready["executable_ready"] = actual
    ready["assurance_qualified_ready"] = actual
    ready["nominal_to_actual_loss"] = ready["nominal_ready"] - actual
    ready["blocked_reasons"] = dict(Counter(reason for row in ready["rows"] for reason in row["reasons"]))
    ready["basis"] = "intersection of legacy gates and snapshot-bound assurance checks; live lane must recheck all gates"
    result = {**evaluated, "covered_roles": len(covered), "missing_role_ids": missing,
              "checks_passed_count": sum(row["assurance_checks_passed"] for row in ready["rows"]),
              "assurance_qualified_ready": actual, "execution_authorized": False}
    if missing and result["state"] == "CHECKS_PASSED":
        result["state"] = "REVIEW_REQUIRED"
    result["coverage_complete"] = not missing
    return result


def build(snapshot, *, now, assurance=None):
    clock(now)
    keys(snapshot, {"schema_version", "source_revision", "observed_at", "complete", "active",
                    "leads", "holds", "hold_decisions", "pool", "capacity", "releases", "discovery",
                    "tray", "question_dependencies", "attempt_events", "attempt_history_complete", "applications"},
         optional={"capacity_evidence"})  # exporter-emitted; absence = unmeasured, never assumed
    version(snapshot); text(snapshot["source_revision"])
    for key in ("complete", "active", "attempt_history_complete"): boolean(snapshot[key], key)
    attempts = reconcile(snapshot["attempt_events"], now=now, history_complete=snapshot["attempt_history_complete"])
    holds = review_holds(snapshot["holds"], snapshot["hold_decisions"], now=now)
    ready = inventory(snapshot["leads"], holds, attempts, now=now)
    role_map = {row["role_id"]: row for row in snapshot["leads"]}
    require({row["role_id"] for row in snapshot["holds"]} <= role_map.keys(), "hold references unknown role")
    for row in records(snapshot["question_dependencies"]):
        require(row.get("role_id") in role_map, "question dependency references unknown role")
        lead = role_map[row["role_id"]]
        require(row.get("fit_score") == lead["fit_score"] and row.get("action_band") == lead.get("action_band"),
                "question dependency conflicts with current role gates")
    executable_ids = {row["role_id"] for row in ready["rows"] if row["executable"]}
    release_blocked = {row["role_id"] for row in holds["rows"] if row["family"] != "cooldown"}
    attempt_blocked = {row["application_id"] for row in attempts["applications"] if row["hold_required"]}
    for release in records(snapshot["releases"]):
        members = release.get("role_ids")
        require(type(members) is list and all(type(rid) is str for rid in members), "explicit release role IDs required")
        require(set(members) <= role_map.keys(), "release references unknown role")
        require(not set(members).intersection(executable_ids), "release double-counts ready inventory")
        for rid in members:
            lead = role_map[rid]
            require(lead["fit_score"] >= 75 and lead.get("action_band") == "APPLY" and lead.get("policy_pass") is True,
                    "future supply fails fit or policy gates")
            require(rid not in release_blocked and lead["identity"] not in attempt_blocked
                    and not set(lead["holds"]) - {"cooldown"}, "future supply has an unresolved non-cooldown hold")
            if release.get("stage") == "VERIFIED_PREPARATION_READY":
                require(lead.get("posting_verified") is True, "verified preparation supply lacks posting verification")
    # Validate future supply against the original canonical gates above, before
    # adding assurance. This prevents a blocked current row becoming refill stock.
    assurance_report = _assurance_inventory(snapshot, ready, assurance, now=now)
    assured_roles = {row["role_id"] for row in ready["rows"] if row["assurance_checks_passed"] is True}
    release_assurance_gaps = sorted({rid for release in snapshot["releases"] for rid in release["role_ids"]
                                    if assurance is not None and rid not in assured_roles})
    bridge = compare_supply(snapshot["pool"], now=now)
    reasons = []
    if not snapshot["complete"]: reasons.append("EXPORT_INCOMPLETE")
    if not fresh(snapshot["observed_at"], now): reasons.append("EXPORT_STALE_OR_FUTURE")
    if not snapshot["attempt_history_complete"]: reasons.append("ATTEMPT_HISTORY_INCOMPLETE")
    if bridge["state"] in {"UNVERIFIED", "CONTRACT_MISMATCH"}: reasons.append("POOL_UNVERIFIED")
    if snapshot["pool"]["ready"] != ready["nominal_ready"]: reasons.append("READY_COUNT_CONFLICT")
    if assurance_report["state"] == "UNVERIFIED": reasons.append("ASSURANCE_UNVERIFIED")
    keys(snapshot["discovery"], {"budget_minutes", "sources"})
    discovery = allocate(snapshot["discovery"]["sources"], budget_minutes=snapshot["discovery"]["budget_minutes"], now=now)
    demand = forecast(ready["executable_ready"], snapshot["capacity"], snapshot["releases"], now=now, active=snapshot["active"])
    if release_assurance_gaps:
        demand = {"state": "UNKNOWN", "reasons": ["FUTURE_SUPPLY_ASSURANCE_NOT_PASSED"],
                  "blocked_release_role_ids": release_assurance_gaps, "discovery_recommended": False,
                  "execution_authorized": False}
    questions = prioritize(snapshot["tray"], snapshot["question_dependencies"], now=now)
    if reasons:
        # Preserve diagnostic counts, but don't advertise actionable proposals from stale/partial data.
        demand = {"state": "UNKNOWN", "reasons": reasons, "discovery_recommended": False, "execution_authorized": False}
        discovery = {"state": "UNVERIFIED_EXPORT", "allocated_minutes": 0, "allocations": [],
                     "unallocated_minutes": snapshot["discovery"]["budget_minutes"], "execution_authorized": False}
    demand["assurance_basis"] = "NOT_CONFIGURED_LEGACY_ONLY" if assurance is None else "SNAPSHOT_BOUND_CHECKS"
    ready["current_estimate_verified"] = not reasons
    questions["current_dependencies_verified"] = not reasons
    actions = []
    def action(priority, kind, reason):
        actions.append({"priority": priority, "action": kind, "reason": reason, "execution_authorized": False})
    if reasons: action(0, "REFRESH_CANONICAL_EXPORT", ", ".join(reasons))
    if not reasons and ready["nominal_to_actual_loss"]:
        action(1, "REVIEW_READY_GATES", "nominal READY rows fail one or more current readiness checks")
    if not reasons and demand["state"] == "UNKNOWN":
        if release_assurance_gaps:
            action(2, "REVIEW_FUTURE_SUPPLY_ASSURANCE", "release members lack passed assurance checks; forecast is unknown")
        else:
            action(2, "MEASURE_SUPPLIED_CAPACITY", "a fresh non-starved capacity observation is required for forecasting")
    if assurance is not None and assurance_report["state"] != "CHECKS_PASSED":
        action(1, "REVIEW_ASSURANCE_GATES", "inspect evidence, claims, reviewer disagreement, authority and missing coverage")
    if attempts["findings"] or any(row["hold_required"] for row in attempts["applications"]):
        action(1, "RECONCILE_ATTEMPTS", "inspect canonical/provider history before any retry")
    if discovery["state"] == "RATE_LIMIT_RECONCILIATION_REQUIRED":
        action(1, "RECONCILE_RATE_LIMIT", "HTTP 429 remains a hard stop")
    if not reasons and not release_assurance_gaps and (snapshot["pool"]["actionable"] == 0 or demand["discovery_recommended"]):
        action(2, "AUDIT_REFILL_SUPPLY", "replacement supply is absent or forecast to arrive too late")
    if holds["overdue_count"] or holds["actions"].get("ASSIGN_OWNER", 0):
        action(3, "REVIEW_OWNED_HOLDS", "use the named owner and exact release condition; preserve consent gates")
    if not reasons and any(g["roles_progressed"] for g in questions["groups"]):
        action(4, "ASK_PRIORITIZED_QUESTIONS", "use current dependencies and a valid operator reply; no answers inferred")
    status = "UNVERIFIED" if reasons else "NEEDS_ATTENTION" if actions else "NO_ALERTS_IN_EXPORT"
    return {"schema_version": 1, "version": __version__, "status": status,
            "evaluated_at": now.isoformat(), "source_revision": snapshot["source_revision"],
            "snapshot_sha256": digest(snapshot), "observed_at": snapshot["observed_at"],
            "actions": actions, "readiness": ready, "forecast": demand, "holds": holds, "assurance": assurance_report,
            "discovery": discovery, "questions": questions, "attempts": attempts,
            "outcomes": cohorts(snapshot["applications"], now=now),
            "integration": {"supply": bridge, "attempt_conformance": inspect_attempts(snapshot["attempt_events"])},
            "effects": {"queue_writes": 0, "http_requests": 0, "browser_actions": 0, "scheduler_changes": 0},
            "execution_authorized": False}


def markdown(report):
    def safe(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    r, f = report["readiness"], report["forecast"]
    lines = ["# Keel operating report", "", f"Status: **{report['status']}**",
             f"Evaluated: {report['evaluated_at']}", f"Source revision: {safe(report['source_revision'])}", "",
             "| Measure | Observation |", "|---|---|",
             f"| Nominal ready | {r['nominal_ready']} |", f"| Executable estimate | {r['executable_ready']} |",
             f"| Legacy executable diagnostic | {r['base_executable_ready']} |",
             f"| Assurance-qualified ready | {r['assurance_qualified_ready'] if r['assurance_qualified_ready'] is not None else 'UNKNOWN'} |",
             f"| Estimate verified against current export | {r['current_estimate_verified']} |",
             f"| Supply | {report['integration']['supply']['state']} |", f"| Forecast | {f['state']} |",
             f"| Runway, seconds | {f.get('runway_seconds')} |",
             f"| Overdue holds | {report['holds']['overdue_count']} |",
             f"| Discovery minutes proposed | {report['discovery']['allocated_minutes']} |",
             f"| Mature observed applications | {report['outcomes']['denominator']} |", "", "## Next actions", ""]
    lines += [f"- {a['action']}: {safe(a['reason'])}." for a in report["actions"]] or ["No alerts in this export."]
    assurance = report["assurance"]
    lines += ["", "## Cognitive assurance", "", f"State: **{safe(assurance['state'])}**",
              f"Covered roles: {assurance['covered_roles']}. Missing coverage: {len(assurance['missing_role_ids'])}.",
              "Snapshot binding checks consistency, not authenticity. Passed checks do not grant execution authority."]
    if assurance["state"] == "NOT_CONFIGURED":
        lines += ["Legacy readiness and forecasting remain diagnostic only; no assurance-qualified count is available."]
    else:
        lines += ["", "| Role | Base gates passed | Assurance checks passed | Combined estimate | Reasons |",
                  "|---|---|---|---|---|"]
        lines += [f"| {safe(row['role_id'])} | {row['base_executable']} | {row['assurance_checks_passed']} | {row['executable']} | {safe(', '.join(row['reasons']) or 'none')} |"
                  for row in r["rows"]]
    lines += ["", "## Blocker review", "", "| Hold | Owner | Action |", "|---|---|---|"]
    lines += [f"| {safe(h['hold_id'])} | {safe(h['owner'] or 'UNASSIGNED')} | {h['action']} |" for h in report["holds"]["rows"]]
    lines += ["", "## Question priority", "", "| Group | Conditional unlocks | Roles progressed | Urgent |", "|---|---|---|---|"]
    lines += [f"| {safe(g['group_id'])} | {len(g['roles_unlocked_if_answered'])} | {len(g['roles_progressed'])} | {g['urgent']} |"
              for g in report["questions"]["groups"]]
    lines += ["", "This is an observation and proposal report. Counts depend on the supplied export. No browser, queue, scheduler, or HTTP actions occurred.",
              "Forecast conversions are conditional; outcome cohorts are descriptive, not causal. Recheck canonical gates before any live action.", ""]
    return "\n".join(lines)
