"""Quarantined preparation recipes and deterministic held-out fixture replay.

The private local workshop is a host-owned record, not authenticated evidence
of real browser execution. The interpreter changes only in-memory reference
slots, never facts, policy, consent, approval, destinations or submission state.
Any real execution must separately pass the complete form and action gateways;
a recipe or matching reference cannot authorize consent or attestations. It has no eval,
shell, network, arbitrary file operations or browser execution capability.
"""
from contextlib import contextmanager
import fcntl
from pathlib import Path

from keel_agent.io import private_home
from keel_loki.common import (clone, digest, require_dict, require_id, require_hash,
    require_int, atomic_json, load_json)
from tools.bench_inventory import directory_fd

ACTIONS = frozenset({"locate", "fill_approved", "attach_approved"})
CONTROLS = frozenset({"text", "select", "checkbox", "attachment", "section"})


def _fail(code):
    raise ValueError(code)


def _scope(value):
    require_dict(value, {"workspace_id", "origin", "account_id", "role_id"}, "scope")
    for key in ("workspace_id", "account_id", "role_id"):
        require_id(value[key])
    origin = value["origin"]
    if type(origin) is not str or not origin.startswith("https://") or len(origin) > 256 or any(c in origin for c in "\n\r?#@"):
        _fail("skill_origin_invalid")
    from urllib.parse import urlsplit
    parsed = urlsplit(origin)
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        _fail("skill_origin_invalid")
    return value


def _fixture(value):
    value = clone(value)
    require_dict(value, {"fixture_id", "split", "scope", "form_revision", "observed_at", "fields"})
    require_id(value["fixture_id"])
    if value["split"] != "held_out":
        _fail("skill_replay_requires_held_out")
    _scope(value["scope"])
    require_hash(value["form_revision"])
    require_int(value["observed_at"])
    fields = value["fields"]
    if type(fields) is not list or not 1 <= len(fields) <= 64:
        _fail("skill_fixture_fields_invalid")
    seen = set()
    for field in fields:
        require_dict(field, {"field_id", "control", "required", "enabled", "approved_ref", "current_ref", "expected_ref"})
        require_id(field["field_id"])
        if field["field_id"] in seen or field["control"] not in CONTROLS:
            _fail("skill_fixture_field_invalid")
        seen.add(field["field_id"])
        if type(field["required"]) is not bool or type(field["enabled"]) is not bool:
            _fail("skill_fixture_boolean_invalid")
        for key in ("approved_ref", "current_ref", "expected_ref"):
            if field[key] is not None:
                require_hash(field[key])
    return value


def _interpret(recipe, fixture, now):
    errors, performed = [], []
    if fixture["scope"] != recipe["scope"]:
        errors.append("scope_mismatch")
    if fixture["form_revision"] != recipe["form_revision"]:
        errors.append("form_revision_mismatch")
    if not recipe["created_at"] <= now < recipe["expires_at"]:
        errors.append("recipe_expired_or_future")
    if not recipe["created_at"] <= fixture["observed_at"] <= now:
        errors.append("fixture_observation_time_invalid")
    state = {field["field_id"]: clone(field) for field in fixture["fields"]}
    for precondition in recipe["preconditions"]:
        field = state.get(precondition["field_id"])
        if field is None or field["control"] != precondition["control"] or not field["enabled"]:
            errors.append("precondition_unsatisfied")
    if not errors:
        for step in recipe["steps"]:
            field = state[step["field_id"]]
            if step["action"] == "locate":
                performed.append({"field_id": step["field_id"], "action": "locate"})
                continue
            if step["value_ref"] is None or step["value_ref"] != field["approved_ref"]:
                errors.append("approved_reference_mismatch")
                continue
            if (step["action"] == "attach_approved") != (field["control"] == "attachment"):
                errors.append("action_control_mismatch")
                continue
            field["current_ref"] = step["value_ref"]
            performed.append({"field_id": step["field_id"], "action": step["action"]})
    for field in state.values():
        if field["required"] and (field["expected_ref"] is None or field["current_ref"] != field["expected_ref"]):
            errors.append("required_readback_mismatch")
        elif field["current_ref"] != field["expected_ref"]:
            # Optional means the field may be left empty; it does not make an
            # explicitly captured final value or emptiness expectation optional.
            errors.append("optional_readback_mismatch")
    return {"fixture_id": fixture["fixture_id"], "fixture_sha256": digest(fixture),
            "status": "PASS" if not errors else "FAIL", "errors": sorted(set(errors)),
            "performed": performed, "final_state_sha256": digest(state),
            "browser_executed": False, "execution_authorized": False}


class SkillWorkshop:
    """Append-only host-owned workshop. Persisted content is checked on every use.

    Promotion consumes an internally generated replay event, never caller PASS.
    A filesystem owner can replace history; this is not independent attestation.
    """
    def __init__(self, home):
        self.home = private_home(home)

    @contextmanager
    def _locked(self):
        descriptor = directory_fd(self.home)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            __import__("os").close(descriptor)

    def _events(self):
        events, previous = [], "0" * 64
        paths = sorted(self.home.glob("event-*.json"))
        if len(paths) > 4096:
            _fail("skill_event_limit")
        for index, path in enumerate(paths):
            if path.name != f"event-{index:08d}.json":
                _fail("skill_history_gap")
            event = load_json(path)
            require_dict(event, {"sequence", "previous_sha256", "kind", "payload", "event_sha256"})
            body = {k: v for k, v in event.items() if k != "event_sha256"}
            if event["sequence"] != index or event["previous_sha256"] != previous or digest(body) != event["event_sha256"]:
                _fail("skill_history_integrity_failed")
            events.append(event)
            previous = event["event_sha256"]
        return events

    def _append(self, events, kind, payload):
        if len(events) >= 4096:
            _fail("skill_event_limit")
        event = {"sequence": len(events), "previous_sha256": events[-1]["event_sha256"] if events else "0" * 64,
                 "kind": kind, "payload": clone(payload)}
        event["event_sha256"] = digest(event)
        atomic_json(self.home / f"event-{len(events):08d}.json", event)
        return event

    def quarantine(self, trace, *, skill_id, expires_at):
        trace = clone(trace)
        require_dict(trace, {"trace_id", "scope", "form_revision", "observed_at", "observations"})
        require_id(trace["trace_id"])
        require_id(skill_id)
        _scope(trace["scope"])
        require_hash(trace["form_revision"])
        require_int(trace["observed_at"])
        require_int(expires_at, trace["observed_at"] + 1)
        observations = trace["observations"]
        if type(observations) is not list or not 1 <= len(observations) <= 64:
            _fail("skill_trace_observations_invalid")
        steps, preconditions, seen = [], [], set()
        for observation in observations:
            require_dict(observation, {"action", "field_id", "control", "value_ref", "readback_ref"})
            require_id(observation["field_id"])
            if observation["action"] not in ACTIONS or observation["control"] not in CONTROLS:
                _fail("skill_action_forbidden")
            if observation["field_id"] in seen:
                _fail("skill_duplicate_field")
            seen.add(observation["field_id"])
            if observation["action"] == "locate":
                if observation["value_ref"] is not None or observation["readback_ref"] is not None:
                    _fail("skill_locate_value_forbidden")
            else:
                if observation["control"] == "section":
                    _fail("skill_section_is_locate_only")
                require_hash(observation["value_ref"])
                if observation["value_ref"] != observation["readback_ref"]:
                    _fail("skill_observed_readback_mismatch")
                if (observation["action"] == "attach_approved") != (observation["control"] == "attachment"):
                    _fail("skill_action_control_invalid")
            steps.append({k: observation[k] for k in ("action", "field_id", "value_ref")})
            preconditions.append({k: observation[k] for k in ("field_id", "control")})
        with self._locked():
            events = self._events()
            versions = [e["payload"]["version"] for e in events if e["kind"] == "QUARANTINE" and e["payload"]["skill_id"] == skill_id]
            recipe = {"schema": "keel.loki.recipe.v1", "skill_id": skill_id, "version": max(versions, default=0) + 1,
                "scope": trace["scope"], "form_revision": trace["form_revision"],
                "created_at": trace["observed_at"], "expires_at": expires_at,
                "trace_sha256": digest(trace), "preconditions": preconditions, "steps": steps,
                "status": "QUARANTINED", "execution_authorized": False}
            self._append(events, "QUARANTINE", recipe)
        return recipe

    def _recipe(self, events, skill_id, version):
        require_id(skill_id)
        require_int(version, 1)
        matches = [e["payload"] for e in events if e["kind"] == "QUARANTINE" and
                   e["payload"]["skill_id"] == skill_id and e["payload"]["version"] == version]
        if len(matches) != 1:
            _fail("skill_version_missing")
        return clone(matches[0])

    def freeze_fixtures(self, fixtures):
        if type(fixtures) is not list or not 2 <= len(fixtures) <= 64:
            _fail("skill_fixture_count_invalid")
        fixtures = [_fixture(f) for f in fixtures]
        if len({f["fixture_id"] for f in fixtures}) != len(fixtures):
            _fail("skill_fixture_ids_duplicate")
        normalized = [{k: v for k, v in f.items() if k not in ("fixture_id", "observed_at")} for f in fixtures]
        if len({digest(f) for f in normalized}) != len(fixtures):
            _fail("skill_fixture_variants_duplicate")
        pin = digest(fixtures)
        with self._locked():
            events = self._events()
            if not any(e["kind"] == "FREEZE" and e["payload"]["fixtures_sha256"] == pin for e in events):
                self._append(events, "FREEZE", {"fixtures_sha256": pin, "fixtures": fixtures})
        return pin

    def replay(self, skill_id, version, *, fixtures_sha256, now):
        require_hash(fixtures_sha256)
        require_int(now)
        with self._locked():
            events = self._events()
            recipe = self._recipe(events, skill_id, version)
            frozen = next((e for e in events if e["kind"] == "FREEZE" and e["payload"]["fixtures_sha256"] == fixtures_sha256), None)
            if frozen is None:
                _fail("skill_frozen_fixtures_missing")
            # A fixed fixture corpus must exist before this candidate was
            # quarantined; changing fixtures after seeing failures is tuning.
            recipe_event = next(e for e in events if e["kind"] == "QUARANTINE" and e["payload"] == recipe)
            if frozen["sequence"] >= recipe_event["sequence"]:
                _fail("skill_fixtures_must_precede_candidate")
            if any(e["kind"] == "REPLAY" and e["payload"]["fixtures_sha256"] == fixtures_sha256
                   and e["payload"]["recipe_sha256"] != digest(recipe) for e in events):
                _fail("skill_fixture_partition_already_used")
            rows = [_interpret(recipe, fixture, now) for fixture in frozen["payload"]["fixtures"]]
            report = {"skill_id": skill_id, "version": version, "recipe_sha256": digest(recipe),
                "fixtures_sha256": fixtures_sha256, "observed_at": now, "rows": rows,
                "status": "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL",
                "fixture_independence_verified": False, "rendered_browser_verified": False,
                "execution_authorized": False}
            event = self._append(events, "REPLAY", report)
        return {"replay_sha256": event["event_sha256"], **report}

    def promote(self, skill_id, version, *, replay_sha256, now):
        require_hash(replay_sha256)
        require_int(now)
        with self._locked():
            events = self._events()
            self._check_control_time(events, skill_id, now)
            recipe = self._recipe(events, skill_id, version)
            replay = next((e["payload"] for e in events if e["kind"] == "REPLAY" and e["event_sha256"] == replay_sha256), None)
            if replay is None or replay["recipe_sha256"] != digest(recipe) or replay["status"] != "PASS":
                _fail("skill_passing_bound_replay_required")
            if not replay["observed_at"] <= now < recipe["expires_at"]:
                _fail("skill_promotion_time_invalid")
            payload = {"skill_id": skill_id, "version": version, "replay_sha256": replay_sha256,
                       "observed_at": now, "execution_authorized": False}
            self._append(events, "PROMOTE", payload)
        return payload

    def active(self, skill_id, *, scope, form_revision, now):
        require_id(skill_id)
        _scope(scope)
        require_hash(form_revision)
        require_int(now)
        with self._locked():
            events = self._events()
            active = None
            observed_at = None
            for event in events:
                if event["kind"] in ("PROMOTE", "ROLLBACK") and event["payload"]["skill_id"] == skill_id:
                    active = event["payload"]["version"]
                    observed_at = event["payload"]["observed_at"]
            # This is a current-state API, not a historical as-of projection.
            # A regressed caller clock must not expose a future promotion.
            if active is None or now < observed_at:
                return None
            recipe = self._recipe(events, skill_id, active)
            if recipe["scope"] != scope or recipe["form_revision"] != form_revision or not recipe["created_at"] <= now < recipe["expires_at"]:
                return None
            return {**recipe, "status": "PROMOTED"}

    @staticmethod
    def _check_control_time(events, skill_id, now):
        if any(event["kind"] in ("PROMOTE", "ROLLBACK")
               and event["payload"]["skill_id"] == skill_id
               and now < event["payload"]["observed_at"] for event in events):
            _fail("skill_control_time_regressed")

    def rollback(self, skill_id, *, to_version, now):
        require_id(skill_id)
        require_int(now)
        if to_version is not None:
            require_int(to_version, 1)
        with self._locked():
            events = self._events()
            self._check_control_time(events, skill_id, now)
            if to_version is not None:
                recipe = self._recipe(events, skill_id, to_version)
                if not any(e["kind"] == "PROMOTE" and e["payload"]["skill_id"] == skill_id and e["payload"]["version"] == to_version for e in events):
                    _fail("skill_rollback_requires_prior_promotion")
                if not recipe["created_at"] <= now < recipe["expires_at"]:
                    _fail("skill_rollback_target_expired")
            payload = {"skill_id": skill_id, "version": to_version, "observed_at": now, "execution_authorized": False}
            self._append(events, "ROLLBACK", payload)
        return payload


def demo(home):
    workshop = SkillWorkshop(home)
    scope = {"workspace_id": "fixture", "origin": "https://fixture.invalid", "account_id": "fixture", "role_id": "fixture"}
    fields = [{"field_id": "name", "control": "text", "required": True, "enabled": True,
               "approved_ref": "b" * 64, "current_ref": None, "expected_ref": "b" * 64}]
    fixtures = [{"fixture_id": "variant-" + str(i), "split": "held_out", "scope": scope,
                 "form_revision": "a" * 64, "observed_at": 10, "fields": fields} for i in range(2)]
    fixtures[1] = clone(fixtures[1])
    fixtures[1]["fields"][0]["current_ref"] = "c" * 64
    pin = workshop.freeze_fixtures(fixtures)
    trace = {"trace_id": "observed-fixture", "scope": scope, "form_revision": "a" * 64, "observed_at": 10,
             "observations": [{"action": "fill_approved", "field_id": "name", "control": "text", "value_ref": "b" * 64, "readback_ref": "b" * 64}]}
    recipe = workshop.quarantine(trace, skill_id="name-preparation", expires_at=100)
    replay = workshop.replay(recipe["skill_id"], recipe["version"], fixtures_sha256=pin, now=11)
    promoted = workshop.promote(recipe["skill_id"], recipe["version"], replay_sha256=replay["replay_sha256"], now=12)
    active = workshop.active(recipe["skill_id"], scope=scope, form_revision="a" * 64, now=12)
    workshop.rollback(recipe["skill_id"], to_version=None, now=13)
    return {"recipe": recipe, "replay": replay, "promotion": promoted, "active_before_rollback": active is not None,
            "active_after_rollback": workshop.active(recipe["skill_id"], scope=scope, form_revision="a" * 64, now=14) is not None,
            "execution_authorized": False}
