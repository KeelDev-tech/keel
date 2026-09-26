"""A snapshot-driven pipeline model, with bounded hypothetical interventions.

No live synchronization adapter, learned person model, scheduler or provider
transport. Simulated events never enter the canonical log or grant authority.
"""
from datetime import timedelta
from keel_flow.board import build
from .common import (clone, digest, keys, version, text, choice, require,
                     number, clock, timestamp, records)

MODEL_VERSION = "keel-twin-v1"


def capture(flow_export, *, now):
    clock(now); snapshot = clone(flow_export)
    report = build(snapshot, now=now)
    return {"schema_version": 1, "model_version": MODEL_VERSION, "captured_at": now.isoformat(),
            "source_sha256": digest(snapshot), "source_revision": snapshot["source_revision"], "export": snapshot,
            "status": "CURRENT_EXPORT_MODEL" if report["readiness"]["current_estimate_verified"] else "UNVERIFIED_EXPORT_MODEL",
            "synchronization": "CALLER_EXPORT", "live_sync_connected": False, "calibration": "NOT_ESTABLISHED",
            "baseline": report, "execution_authorized": False}


def validate_model(model):
    keys(model, {"schema_version", "model_version", "captured_at", "source_sha256", "source_revision", "export", "status",
                 "synchronization", "live_sync_connected", "calibration", "baseline", "execution_authorized"})
    version(model); require(model["model_version"] == MODEL_VERSION, "unsupported twin model")
    require(model["source_sha256"] == digest(model["export"]), "twin export changed")
    anchor = timestamp(model["captured_at"])
    baseline = build(model["export"], now=anchor)
    require(digest(model["baseline"]) == digest(baseline), "twin baseline changed")
    require(model["source_revision"] == model["export"]["source_revision"], "source revision mismatch")
    require(model["execution_authorized"] is False and model["live_sync_connected"] is False
            and model["synchronization"] == "CALLER_EXPORT" and model["calibration"] == "NOT_ESTABLISHED", "unsupported model authority claim")
    expected = "CURRENT_EXPORT_MODEL" if baseline["readiness"]["current_estimate_verified"] else "UNVERIFIED_EXPORT_MODEL"
    require(model["status"] == expected, "model status contradicts source")
    return anchor, baseline


def metrics(report):
    return {"status": report["status"], "executable_ready": report["readiness"]["executable_ready"],
            "estimate_current": report["readiness"]["current_estimate_verified"], "forecast_state": report["forecast"]["state"],
            "runway_seconds": report["forecast"].get("runway_seconds"),
            "first_refill_seconds": report["forecast"].get("first_refill_opportunity_seconds"),
            "exhaustion_seconds": report["forecast"].get("earliest_exhaustion_seconds"),
            "discovery_minutes": report["discovery"]["allocated_minutes"],
            "held_applications": sum(row["hold_required"] for row in report["attempts"]["applications"])}


def simulate(model, scenario):
    anchor, baseline = validate_model(model)
    keys(scenario, {"schema_version", "scenario_id", "base_sha256", "assumption_ref", "changes"}); version(scenario)
    text(scenario["scenario_id"]); text(scenario["assumption_ref"])
    require(scenario["base_sha256"] == model["source_sha256"], "scenario bound to different export")
    changes = records(scenario["changes"], "scenario changes", maximum=100)
    candidate = clone(model["export"]); changed_targets = set()
    for change in changes:
        keys(change, {"kind", "target", "value"})
        kind, target, value = change["kind"], change["target"], change["value"]
        choice(kind, {"CAPACITY_MULTIPLIER", "VERIFY_MULTIPLIER", "PREPARE_MULTIPLIER", "RELEASE_DELAY_SECONDS",
                      "RATE_HOLD", "INVALIDATE_PACKET", "UNKNOWN_ATTEMPT", "OBSERVATION_AGE_SECONDS"}, "intervention")
        text(target)
        require((kind, target) not in changed_targets, "duplicate scenario target"); changed_targets.add((kind, target))
        if kind == "CAPACITY_MULTIPLIER":
            require(target == "capacity" and candidate["capacity"] is not None, "capacity measurement required")
            number(value, minimum=.25, maximum=4); candidate["capacity"]["value"] *= value
        elif kind in {"VERIFY_MULTIPLIER", "PREPARE_MULTIPLIER", "RELEASE_DELAY_SECONDS"}:
            rows = [r for r in candidate["releases"] if r["release_id"] == target]; require(len(rows) == 1, "unknown release target")
            row = rows[0]
            if kind == "RELEASE_DELAY_SECONDS":
                number(value, maximum=604800); row["available_at"] = (timestamp(row["available_at"]) + timedelta(seconds=value)).isoformat()
            else:
                number(value, minimum=.25, maximum=4)
                key = "p95_verify_seconds" if kind == "VERIFY_MULTIPLIER" else "p95_prepare_seconds"
                require(key in row, "stage timing unavailable"); row[key] *= value
        elif kind == "RATE_HOLD":
            require(value is True, "scenario may introduce, not clear, a rate hold")
            rows = [r for r in candidate["discovery"]["sources"] if r["source_id"] == target]
            require(len(rows) == 1, "unknown source target"); rows[0]["rate_limited"] = True
        elif kind in {"INVALIDATE_PACKET", "UNKNOWN_ATTEMPT"}:
            rows = [r for r in candidate["leads"] if r["role_id"] == target]; require(len(rows) == 1, "unknown role target")
            row = rows[0]
            if kind == "INVALIDATE_PACKET":
                choice(value, set(row["dependencies"]), "packet dependency")
                row["dependencies"][value] = "simulated-" + digest(scenario)  # packet hash is deliberately NOT updated
            else:
                require(value is True, "unknown-attempt injection requires true")
                require(not any(target in r["role_ids"] for r in candidate["releases"]), "unknown attempt on forecast supply requires a fresh canonical export")
                attempt_id = "sim-" + digest([scenario["scenario_id"], row["identity"]])
                for seq, state in enumerate(("INTENT", "DISPATCHED", "UNKNOWN")):
                    candidate["attempt_events"].append({"schema_version": 1, "event_id": digest([attempt_id, seq]),
                        "application_id": row["identity"], "attempt_id": attempt_id, "sequence": seq, "state": state,
                        "content_hash": digest(["SIMULATION", scenario]), "observed_at": anchor.isoformat(),
                        "source_ref": "SIMULATION_ONLY", "execution_authorized": False})
        else:
            require(target == "snapshot", "observation target must be snapshot")
            number(value, maximum=604800)
            old = timestamp(candidate["observed_at"])
            candidate["observed_at"] = min(old, anchor - timedelta(seconds=value)).isoformat()
    report = build(candidate, now=anchor)
    before, after = metrics(baseline), metrics(report)
    return {"schema_version": 1, "scenario_id": scenario["scenario_id"], "mode": "SIMULATION_ONLY", "as_of": anchor.isoformat(),
            "base_sha256": model["source_sha256"], "scenario_sha256": digest(scenario), "candidate_sha256": digest(candidate),
            "assumption_ref": scenario["assumption_ref"], "baseline": before, "scenario": after,
            "changed_metrics": {k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]},
            "constraints_preserved": {"ready_floor": 5, "check_seconds": 30, "spawn_throttle_seconds": 600,
                                      "consent_and_fit_gates_changed": False},
            "forecast_calibration": "NOT_ESTABLISHED", "live_sync_connected": False,
            "live_writes": 0, "execution_authorized": False, "provider_acceptance_verified": False}


def drift(model, current_export, *, now):
    anchor, baseline = validate_model(model); clock(now)
    require(now >= anchor, "new observation predates model capture")
    current = build(clone(current_export), now=now)
    old = {row["role_id"]: row for row in model["export"]["leads"]}
    new = {row["role_id"]: row for row in current_export["leads"]}
    changed = []
    for rid in sorted(old.keys() & new.keys()):
        fields = [key for key in sorted(set(old[rid]) | set(new[rid])) if old[rid].get(key) != new[rid].get(key)]
        if fields: changed.append({"role_id": rid, "fields": fields})
    verified = current["readiness"]["current_estimate_verified"]
    return {"status": "OBSERVED_CHANGE" if verified and digest(current_export) != model["source_sha256"] else
                       "NO_CHANGE_IN_EXPORT" if verified else "CURRENT_EXPORT_UNVERIFIED",
            "elapsed_seconds": (now - anchor).total_seconds(), "new_sha256": digest(current_export),
            "added_roles": sorted(new.keys() - old.keys()), "removed_roles": sorted(old.keys() - new.keys()),
            "changed_roles": changed, "before": metrics(baseline), "after": metrics(current),
            "forecast_error_estimated": False, "live_sync_connected": False, "execution_authorized": False}
