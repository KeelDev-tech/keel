"""Supply incident regressions; all observations and records are synthetic."""
import ast
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "engines"))
from keel_local.contracts import ContractError
from keel_local.readiness import refill_plan
from keel_local.supply import (classify_pool, should_fire, pool_health, health_from_event,
                               supply_alert_reason, PoolHealthObserver, READY_FLOOR,
                               CHECK_SECONDS, SPAWN_THROTTLE_SECONDS,
                               SUPPLY_STALENESS_FANOUT_SECONDS)
from keel_local.supply_audit import audit_supply
from keel_local.tray_audit import audit_tray
import log_event

NOW = datetime(2026, 9, 18, 7, 10, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


def lead(rid="fixture-1", **changes):
    return {"role_id": rid, "status": "PARKED-PENDING-VERIFICATION", "fit_score": 82,
            "action_band": "APPLY", "holds": [], "structurally_blocked": False,
            "last_verify_attempt": (NOW - timedelta(hours=25)).isoformat(),
            "cooldown": {"base_hours": 24, "jitter_delta_hours": 0, "effective_hours": 24,
                         "policy_revision": "synthetic-policy", "reported_in_cooldown": False}, **changes}


def export(rows, **changes):
    return {"schema_version": 1, "source_revision": "synthetic-fixture", "complete": True,
            "observed_at": STAMP, "ready": 5, "leads": rows, **changes}


class PoolGuardianContractTests(unittest.TestCase):
    def test_reported_regression(self):
        self.assertEqual(should_fire(5, 0, elapsed_since_spawn=600), (False, "buffer full, supply starved"))

    def test_healthy_iff_buffer_and_actionable(self):
        for ready in range(11):
            for actionable in range(5):
                self.assertEqual(classify_pool(ready, actionable)["healthy"], ready >= 5 and actionable > 0)

    def test_empty_low_pool_preserves_message(self):
        self.assertEqual(should_fire(4, 0, elapsed_since_spawn=600),
                         (False, "pool low but nothing actionable (all in cooldown)"))

    def test_constants_unchanged(self):
        self.assertEqual((READY_FLOOR, CHECK_SECONDS, SPAWN_THROTTLE_SECONDS), (5, 30, 600))

    def test_throttle_boundary(self):
        for elapsed, expected in ((-1, False), (0, False), (599.999, False), (600, True), (601, True), (None, True)):
            self.assertEqual(should_fire(4, 1, elapsed_since_spawn=elapsed)[0], expected)

    def test_running_verifier_prevents_second_spawn(self):
        self.assertFalse(should_fire(4, 1, elapsed_since_spawn=600, verifier_running=True)[0])

    def test_invalid_counts_never_healthy(self):
        for value in (None, True, -1, 0.0, "0", float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ContractError): classify_pool(5, value)
                self.assertEqual(pool_health(5, value, observed_at=STAMP, now=NOW)["state"], "UNVERIFIED")

    def test_stale_future_naive_and_missing_observations(self):
        for stamp in (None, "bad", "2026-09-18T07:10:00", (NOW - timedelta(seconds=91)).isoformat(),
                      (NOW + timedelta(seconds=1)).isoformat()):
            self.assertFalse(pool_health(5, 1, observed_at=stamp, now=NOW)["healthy"])

    def test_freshness_boundary(self):
        self.assertTrue(pool_health(5, 1, observed_at=(NOW - timedelta(seconds=90)).isoformat(), now=NOW)["healthy"])

    def test_naive_now_refused(self):
        with self.assertRaises(ContractError): pool_health(5, 1, observed_at=STAMP, now=NOW.replace(tzinfo=None))

    def test_refill_planner_exposes_covered_empty_supply(self):
        args = dict(active=True, permitted_slots=2, capacity_per_hour=12, p95_prepare_seconds=900,
                    burst=2, executable=5, preparation_wip=0, verified_supply=0, yield_rate=.5, max_checks=30)
        result = refill_plan(**args)
        self.assertEqual(result["state"], "BUFFER_COVERED_SUPPLY_EMPTY")
        self.assertEqual((result["prepare"], result["checks"], result["target"]), (0, 0, 5))
        self.assertEqual(refill_plan(**{**args, "preparation_wip": 1})["state"], "COVERED")


class HealthObserverTests(unittest.TestCase):
    def observer(self):
        logger = Mock(side_effect=lambda event, **kw: {"event_type": event, **kw})
        return PoolHealthObserver(logger), logger

    def test_transition_heartbeat_and_recovery(self):
        """Debounce contract (DEBOUNCE_SAMPLES=2, J-20260918-1140-feed-2061):
        a state change must be seen on two consecutive checks before a
        transition is emitted; heartbeats run on the last confirmed state; a
        single-sample spike can never become a transition and is cleared when
        the next sample reverts."""
        observer, logger = self.observer()
        obs = lambda seconds, ready, actionable: observer.observe(
            ready, actionable,
            observed_at=(NOW + timedelta(seconds=seconds)).isoformat(),
            now=NOW + timedelta(seconds=seconds))

        # First sample of BUFFER_FULL_SUPPLY_STARVED: pending, nothing emitted.
        first = obs(0, 5, 0)
        self.assertFalse(first["event_emitted"])
        self.assertTrue(first["pending"])
        self.assertEqual(logger.call_count, 0)
        self.assertIsNone(observer.last_emitted)

        # Second consecutive sample: transition confirmed and emitted.
        second = obs(30, 5, 0)
        self.assertTrue(second["event_emitted"])
        self.assertNotIn("pending", second)
        self.assertEqual(logger.call_count, 1)
        self.assertEqual(logger.call_args.kwargs["details"]["observation_kind"], "transition")
        self.assertEqual(logger.call_args.kwargs["details"]["state"], "BUFFER_FULL_SUPPLY_STARVED")

        # Confirmed state, before the 60s heartbeat cadence: no emit.
        third = obs(60, 5, 0)
        self.assertFalse(third["event_emitted"])
        self.assertEqual(logger.call_count, 1)

        # Heartbeat due at 60s on the confirmed state.
        fourth = obs(90, 5, 0)
        self.assertTrue(fourth["event_emitted"])
        self.assertEqual(logger.call_count, 2)
        self.assertEqual(logger.call_args_list[1].kwargs["details"]["observation_kind"], "heartbeat")

        # A single-sample spike: pending, never published; the next sample
        # reverts and clears the pending candidate with no transition emitted.
        spike = obs(91, 5, 1)
        self.assertFalse(spike["event_emitted"])
        self.assertTrue(spike["pending"])
        self.assertEqual(logger.call_count, 2)
        revert = obs(92, 5, 0)
        self.assertFalse(revert["event_emitted"])
        self.assertNotIn("pending", revert)
        self.assertEqual(logger.call_count, 2)
        self.assertIsNone(observer.pending_signature)

        # A real flip confirms one check (~30s) later and publishes the new state.
        flip1 = obs(93, 5, 1)
        self.assertFalse(flip1["event_emitted"])
        self.assertTrue(flip1["pending"])
        flip2 = obs(123, 5, 1)
        self.assertTrue(flip2["event_emitted"])
        self.assertEqual(logger.call_count, 3)
        self.assertEqual(logger.call_args.kwargs["details"]["observation_kind"], "transition")
        self.assertEqual(logger.call_args.kwargs["details"]["state"], "HEALTHY")

    def test_failed_append_does_not_advance_watermark(self):
        """Error propagation under the debounce contract: the logger is only
        reached on the confirming (second) sample, and a failed emit leaves
        last_emitted/last_signature untouched."""
        observer, logger = self.observer()
        logger.side_effect = OSError("disk full")
        first = observer.observe(5, 0, observed_at=STAMP, now=NOW)
        self.assertFalse(first["event_emitted"])  # debounced: never reaches the logger
        self.assertEqual(logger.call_count, 0)
        with self.assertRaises(OSError): observer.observe(5, 0, observed_at=STAMP, now=NOW)
        self.assertIsNone(observer.last_emitted)  # failed emit did not advance the watermark
        logger.side_effect = lambda event, **kw: {"event_type": event, **kw}
        third = observer.observe(5, 0, observed_at=STAMP, now=NOW)
        self.assertTrue(third["event_emitted"])  # still-confirmed pending sample emits
        self.assertIsNotNone(observer.last_emitted)

    def test_missing_acknowledgement_not_claimed(self):
        observer = PoolHealthObserver(lambda *a, **kw: None)
        first = observer.observe(5, 0, observed_at=STAMP, now=NOW)
        self.assertFalse(first["event_emitted"])  # debounced: logger not yet called
        with self.assertRaisesRegex(RuntimeError, "did not acknowledge"):
            observer.observe(5, 0, observed_at=STAMP, now=NOW)
        self.assertIsNone(observer.last_emitted)

    def test_old_healthy_event_becomes_unverified(self):
        event = {"event_type": "pool_health", "source": "pool-guardian",
                 "details": pool_health(5, 1, observed_at=STAMP, now=NOW)}
        self.assertEqual(health_from_event(event, now=NOW + timedelta(seconds=91))["state"], "UNVERIFIED")

    def test_conflicting_counts_cannot_claim_healthy(self):
        health = pool_health(5, 0, observed_at=STAMP, now=NOW)
        health.update(state="HEALTHY", healthy=True)
        event = {"event_type": "pool_health", "source": "pool-guardian", "details": health}
        self.assertEqual(health_from_event(event, now=NOW)["state"], "UNVERIFIED")

    def test_wrong_source_and_schema_are_unverified(self):
        event = {"event_type": "pool_health", "source": "pool-guardian",
                 "details": pool_health(5, 1, observed_at=STAMP, now=NOW)}
        event["source"] = "some-worker"
        self.assertFalse(health_from_event(event, now=NOW)["healthy"])
        event["source"] = "pool-guardian"; event["details"]["schema_version"] = True
        self.assertFalse(health_from_event(event, now=NOW)["healthy"])

    def test_existing_event_log_is_appended_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(log_event, "EVENTS", str(Path(tmp) / "events.jsonl")):
            previous = b'{"event_id":"old","event_type":"error","details":{}}\n'
            Path(log_event.EVENTS).write_bytes(previous)
            observer = PoolHealthObserver(log_event.log)
            first = observer.observe(5, 0, observed_at=STAMP, now=NOW)
            self.assertFalse(first["event_emitted"])  # first sample debounced: nothing appended
            self.assertEqual(Path(log_event.EVENTS).read_bytes(), previous)
            second = observer.observe(5, 0, observed_at=STAMP, now=NOW)
            self.assertTrue(second["event_emitted"])  # second sample confirms the transition
            actual = Path(log_event.EVENTS).read_bytes()
            self.assertTrue(actual.startswith(previous)); self.assertEqual(len(actual.splitlines()), 2)
            self.assertEqual(json.loads(actual.splitlines()[1])["details"]["actionable"], 0)

    def test_debounce_rejects_immediate_emit_regression(self):
        """Regression guard for the J-20260918-1140-feed-2061 debounce
        contract: a lone sample must never emit. If the observer regresses
        to immediate-emit semantics, the first assertFalse fails."""
        self.assertEqual(PoolHealthObserver.DEBOUNCE_SAMPLES, 2)
        observer, logger = self.observer()
        first = observer.observe(5, 0, observed_at=STAMP, now=NOW)
        self.assertFalse(first["event_emitted"])
        self.assertTrue(first.get("pending"))
        self.assertEqual(logger.call_count, 0)
        self.assertIsNone(observer.last_emitted)
        self.assertIsNone(observer.last_signature)
        # A lone sample of a different state is equally suppressed.
        second = observer.observe(5, 1, observed_at=STAMP, now=NOW + timedelta(seconds=30))
        self.assertFalse(second["event_emitted"])
        self.assertTrue(second.get("pending"))
        self.assertEqual(logger.call_count, 0)
        self.assertIsNone(observer.last_emitted)

    def test_pending_sample_never_published_but_heartbeat_continues_on_confirmed_state(self):
        """While a new state is pending (one sample), a due heartbeat is
        emitted for the last CONFIRMED state -- the unconfirmed sample is
        never published."""
        observer, logger = self.observer()
        obs = lambda seconds, ready, actionable: observer.observe(
            ready, actionable,
            observed_at=(NOW + timedelta(seconds=seconds)).isoformat(),
            now=NOW + timedelta(seconds=seconds))
        obs(0, 5, 1); obs(30, 5, 1)  # HEALTHY transition confirmed at t=30
        self.assertEqual(logger.call_count, 1)
        spike = obs(100, 5, 0)  # one sample of a new state, heartbeat overdue
        self.assertTrue(spike["event_emitted"])  # heartbeat on confirmed HEALTHY
        self.assertTrue(spike["pending"])        # new state still unconfirmed
        self.assertEqual(logger.call_count, 2)
        details = logger.call_args.kwargs["details"]
        self.assertEqual(details["observation_kind"], "pending_heartbeat")
        self.assertEqual(details["state"], "HEALTHY")   # confirmed state, not the spike
        self.assertEqual(details["actionable"], 1)
        confirm = obs(101, 5, 0)  # second consecutive sample confirms the flip
        self.assertTrue(confirm["event_emitted"])
        self.assertNotIn("pending", confirm)
        self.assertEqual(logger.call_args.kwargs["details"]["observation_kind"], "transition")
        self.assertEqual(logger.call_args.kwargs["details"]["state"], "BUFFER_FULL_SUPPLY_STARVED")


class PulseIntegrationTests(unittest.TestCase):
    def test_snapshot_consumes_health_event_without_changing_pinned_counts(self):
        snap = module("supply_test_snapshot", "monitors/pulse_snapshot.py")
        now = datetime.now(timezone.utc)
        event = {"event_type": "pool_health", "source": "pool-guardian",
                 "details": pool_health(5, 0, observed_at=now.isoformat(), now=now)}
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = Path(tmp) / "events.jsonl"; telemetry.write_text(json.dumps(event) + "\n")
            standard = [{"role_id": str(i), "status": "READY"} for i in range(5)]
            with patch.object(snap, "TELEMETRY", str(telemetry)), patch.object(snap, "_entries", side_effect=[[], standard, [], []]), patch.object(snap, "_api_direct_stats", return_value={}):
                result = snap.build_snapshot()
            self.assertEqual(result["ready"], 5)
            self.assertEqual(result["pool_supply"]["state"], "BUFFER_FULL_SUPPLY_STARVED")

    def gate(self, health):
        gate = module("supply_test_fanout", "monitors/fanout_gate.py")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot, watermark = Path(tmp) / "snapshot.json", Path(tmp) / "watermark.json"
            snapshot.write_text(json.dumps({"ready": 5, "pool_supply": health}))
            watermark.write_text(json.dumps({"pulse_count": 1, "hashes": {"fixture": "unchanged"}}))
            output = io.StringIO()
            with patch.object(gate, "SNAPSHOT", str(snapshot)), patch.object(gate, "WATERMARK", str(watermark)), patch.object(gate, "WATCHED_FILES", {"fixture": "unused"}), patch.object(gate, "sha256_file", return_value="unchanged"), patch.object(gate, "build_watermark", return_value={}), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit): gate.main()
            return json.loads(output.getvalue())

    def test_full_starved_buffer_bypasses_unchanged_file_shortcut(self):
        now = datetime.now(timezone.utc)
        health = pool_health(5, 0, observed_at=now.isoformat(), now=now)
        result = self.gate(health)
        self.assertEqual(result["verdict"], "FANOUT")
        self.assertIn("supply_starved", result["reasons"][0])

    def test_missing_health_is_visible_even_if_counts_unchanged(self):
        # 2026-09-19 false-FANOUT hardening: a snapshot missing pool_supply
        # falls back to the pool-guardian heartbeat stream in telemetry. The
        # unverified reason must still fire when NEITHER source carries fresh
        # data (fail-open preserved); the empty telemetry file below makes
        # this scenario genuinely data-less.
        gate = module("supply_test_fanout_nohb", "monitors/fanout_gate.py")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot.json"
            watermark = Path(tmp) / "watermark.json"
            telemetry = Path(tmp) / "events.jsonl"
            snapshot.write_text(json.dumps({"ready": 5, "pool_supply": None}))
            watermark.write_text(json.dumps({"pulse_count": 1, "hashes": {"fixture": "unchanged"}}))
            telemetry.write_text("")
            output = io.StringIO()
            with patch.object(gate, "SNAPSHOT", str(snapshot)), patch.object(gate, "WATERMARK", str(watermark)), patch.object(gate, "TELEMETRY", str(telemetry)), patch.object(gate, "WATCHED_FILES", {"fixture": "unused"}), patch.object(gate, "sha256_file", return_value="unchanged"), patch.object(gate, "build_watermark", return_value={}), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit): gate.main()
            result = json.loads(output.getvalue())
        self.assertEqual(result["verdict"], "FANOUT")
        self.assertIn("supply_unverified", result["reasons"][0])

    def test_healthy_unchanged_pool_can_stay_quiet(self):
        now = datetime.now(timezone.utc)
        result = self.gate(pool_health(5, 1, observed_at=now.isoformat(), now=now))
        self.assertEqual(result["verdict"], "QUIET")


class SupplyJitterRegressionTests(unittest.TestCase):
    """ARM 1 (2026-09-19): the 90s event-contract bound fired
    supply_unverified:refresh_observation on a LIVE guardian whenever one
    heartbeat interval jittered past 90s (pulse-488: 115s-old observation;
    42/1119 intervals >90s in 24h; 10 of 22 digests on 2026-09-19 carried the
    reason). The gate's liveness question now uses SUPPLY_STALENESS_FANOUT_
    SECONDS (240s), measured against 24h of pool_health cadence (median
    64.8s, p99 203.5s; 12 genuine multi-minute outages, all but the
    203-229s self-recovering edge cases above 240s)."""

    def gate(self, health):
        gate = module("supply_jitter_fanout", "monitors/fanout_gate.py")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot, watermark = Path(tmp) / "snapshot.json", Path(tmp) / "watermark.json"
            snapshot.write_text(json.dumps({"ready": 5, "pool_supply": health}))
            watermark.write_text(json.dumps({"pulse_count": 1, "hashes": {"fixture": "unchanged"}}))
            output = io.StringIO()
            with patch.object(gate, "SNAPSHOT", str(snapshot)), patch.object(gate, "WATERMARK", str(watermark)), patch.object(gate, "WATCHED_FILES", {"fixture": "unused"}), patch.object(gate, "sha256_file", return_value="unchanged"), patch.object(gate, "build_watermark", return_value={}), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit): gate.main()
            return json.loads(output.getvalue())

    def snap(self, health, **changes):
        return {"ready": 5, "pool_supply": health, **changes}

    def reason(self, health, now):
        return supply_alert_reason(self.snap(health), now=now)

    def test_jittered_but_alive_guardian_does_not_fan_out(self):
        # pulse-488: observation aged 115s by gate time while the guardian
        # kept heartbeating. Writer output for such an observation stays
        # verified; the gate must stay QUIET on unchanged files.
        now = datetime.now(timezone.utc)
        health = pool_health(5, 1, observed_at=(now - timedelta(seconds=115)).isoformat(),
                             now=now, max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
        self.assertNotEqual(health["state"], "UNVERIFIED")
        self.assertIsNone(self.reason(health, now))
        result = self.gate(health)
        self.assertEqual(result["verdict"], "QUIET")

    def test_dead_guardian_five_minute_silence_fans_out(self):
        # A truly stopped guardian must still FANOUT within ~4-5 min.
        now = datetime.now(timezone.utc)
        event = {"event_type": "pool_health", "source": "pool-guardian",
                 "details": {"schema_version": 1, "ready_floor": READY_FLOOR,
                             "ready": 5, "actionable": 1,
                             "observed_at": (now - timedelta(seconds=301)).isoformat()}}
        health = health_from_event(event, now=now,
                                   max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
        self.assertEqual(health["state"], "UNVERIFIED")
        self.assertEqual(self.reason(health, now), "supply_unverified:refresh_observation")
        result = self.gate(health)
        self.assertEqual(result["verdict"], "FANOUT")
        self.assertIn("supply_unverified", result["reasons"][0])

    def test_flatlined_supply_still_bypasses_unchanged_file_shortcut(self):
        # The bypass rationale survives: stale past 240s with unchanged
        # files is exactly the failure mode and must fan out.
        now = datetime.now(timezone.utc)
        event = {"event_type": "pool_health", "source": "pool-guardian",
                 "details": {"schema_version": 1, "ready_floor": READY_FLOOR,
                             "ready": 5, "actionable": 1,
                             "observed_at": (now - timedelta(seconds=500)).isoformat()}}
        health = health_from_event(event, now=now,
                                   max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
        result = self.gate(health)
        self.assertEqual(result["verdict"], "FANOUT")
        self.assertIn("supply_unverified", result["reasons"][0])

    def test_staleness_boundary(self):
        now = datetime.now(timezone.utc)
        for age, expect_reason in ((239, False), (241, True)):
            health = pool_health(5, 1, observed_at=(now - timedelta(seconds=age)).isoformat(),
                                 now=now, max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
            reason = self.reason(health, now)
            if expect_reason:
                self.assertEqual(health["state"], "UNVERIFIED")
                self.assertEqual(reason, "supply_unverified:refresh_observation")
            else:
                self.assertNotEqual(health["state"], "UNVERIFIED")
                self.assertIsNone(reason)

    def test_writer_and_gate_share_liveness_bound(self):
        # Guards the writer-side fix: the canonical snapshot writer must use
        # the 240s liveness bound too. A 115s-old heartbeat event must land in
        # the snapshot verified -- otherwise the gate's jitter tolerance can
        # never recover it (counts are already lost).
        snap = module("supply_jitter_snapshot", "monitors/pulse_snapshot.py")
        now = datetime.now(timezone.utc)
        event = {"event_type": "pool_health", "source": "pool-guardian", "ts": now.isoformat(),
                 "details": pool_health(5, 1, observed_at=(now - timedelta(seconds=115)).isoformat(),
                                        now=now, max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)}
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = Path(tmp) / "events.jsonl"; telemetry.write_text(json.dumps(event) + "\n")
            standard = [{"role_id": str(i), "status": "READY"} for i in range(5)]
            with patch.object(snap, "TELEMETRY", str(telemetry)), patch.object(snap, "_entries", side_effect=[[], standard, [], []]), patch.object(snap, "_api_direct_stats", return_value={}):
                result = snap.build_snapshot()
        self.assertNotEqual(result["pool_supply"]["state"], "UNVERIFIED")
        self.assertIsNone(supply_alert_reason(result, now=now))


class SupplyAuditTests(unittest.TestCase):
    def audit(self, rows, **kw):
        return audit_supply(export(rows, **kw), now=NOW)

    def test_known_actionable_is_not_ready_promotion(self):
        result = self.audit([lead()])
        self.assertEqual(result["known_actionable_for_verification"], 1)
        self.assertEqual(result["ready_promotions"], 0)
        self.assertFalse(result["liveness_established"])

    def test_no_input_mutation(self):
        data = export([lead()]); before = copy.deepcopy(data)
        audit_supply(data, now=NOW)
        self.assertEqual(data, before)

    def test_structural_holds_retained_even_with_conflict(self):
        row = lead(structurally_blocked=True, structural_rules=[dict(rule_id="r1", field="gate_note", evidence_ref="fixture-a", contradiction_ref="fixture-b")])
        result = self.audit([row])
        self.assertEqual(result["rows"][0]["state"], "STRUCTURALLY_BLOCKED")
        self.assertEqual(result["review_candidates"], [row["role_id"]])
        self.assertEqual(result["known_actionable_for_verification"], 0)

    def test_missing_structural_rule_evidence_is_visible(self):
        result = self.audit([lead(structurally_blocked=True)])
        self.assertEqual(len(result["review_candidates"]), 1)
        self.assertEqual(result["rows"][0]["state"], "STRUCTURALLY_BLOCKED")

    def test_low_fit_gate_preserved(self):
        for fit in (0, 74.999):
            self.assertEqual(self.audit([lead(fit_score=fit)])["rows"][0]["state"], "LOW_FIT")
        self.assertEqual(self.audit([lead(fit_score=75)])["known_actionable_for_verification"], 1)

    def test_consent_and_operator_holds_cannot_be_cleared_by_liveness(self):
        for hold, expected in (("consent_quarantine", "CONSENT_QUARANTINED"), ("operator_input", "OPERATOR_INPUT"), ("rate_limit", "POLICY_OR_SAFETY_HOLD"), ("dedupe", "POLICY_OR_SAFETY_HOLD")):
            row = lead(holds=[hold], http_status=200)
            self.assertEqual(self.audit([row])["rows"][0]["state"], expected)

    def test_unknown_contract_fields_never_supply(self):
        for key, value in (("fit_score", None), ("fit_score", True), ("structurally_blocked", None), ("holds", None), ("holds", ["unknown-rule"]), ("action_band", "EXPLORE")):
            result = self.audit([lead(**{key: value})])
            self.assertEqual(result["rows"][0]["state"], "UNKNOWN")
            self.assertEqual(result["pool_health"]["state"], "UNVERIFIED")

    def test_duplicate_rows_cannot_inflate_supply(self):
        result = self.audit([lead(), lead()])
        self.assertEqual(result["counts"], {"UNKNOWN": 2})
        self.assertEqual(result["known_actionable_for_verification"], 0)

    def test_incomplete_export_never_claims_exact_pool_health(self):
        result = self.audit([lead()], complete=False)
        self.assertEqual(result["pool_health"]["state"], "UNVERIFIED")

    def test_stale_export_keeps_historical_state_separate(self):
        result = self.audit([lead()], observed_at=(NOW - timedelta(seconds=91)).isoformat())
        self.assertEqual(result["counts"], {"UNKNOWN": 1})
        self.assertIn("state_at_observation", result["rows"][0])

    def test_cooldown_releases_sorted_and_quantified(self):
        rows = []
        for i, hours in enumerate((1, 2, 2)):
            row = lead(str(i), last_verify_attempt=(NOW - timedelta(hours=24-hours)).isoformat())
            row["cooldown"]["reported_in_cooldown"] = True
            rows.append(row)
        result = self.audit(rows)
        self.assertEqual(result["counts"], {"COOLDOWN": 3})
        self.assertEqual([r["leads"] for r in result["cooldown_release_schedule"]], [1, 2])
        self.assertEqual(result["cooldown_release_schedule"][-1]["cumulative"], 3)

    def test_cooldown_exact_expiry_is_eligible(self):
        row = lead(last_verify_attempt=(NOW - timedelta(hours=24)).isoformat())
        self.assertEqual(self.audit([row])["known_actionable_for_verification"], 1)

    def test_naive_future_malformed_attempts_require_review(self):
        for stamp in ("bad", "2026-09-18T07:00:00", (NOW + timedelta(seconds=1)).isoformat()):
            self.assertEqual(self.audit([lead(last_verify_attempt=stamp)])["counts"], {"UNKNOWN": 1})

    def test_absent_attempt_differs_from_explicit_never_attempted(self):
        row = lead(); del row["last_verify_attempt"]
        self.assertEqual(self.audit([row])["counts"], {"UNKNOWN": 1})
        self.assertEqual(self.audit([lead(last_verify_attempt=None)])["known_actionable_for_verification"], 1)

    def test_same_instant_comparison_avoids_boundary_false_alarm(self):
        row = lead(last_verify_attempt=(NOW - timedelta(hours=24) + timedelta(seconds=1)).isoformat())
        row["cooldown"]["reported_in_cooldown"] = True
        result = audit_supply(export([row]), now=NOW + timedelta(seconds=2))
        self.assertEqual(result["counts"], {"COOLDOWN": 1})

    def test_jitter_overshoot_and_unquantized_windows_flagged(self):
        for effective, delta in ((48, 24), (24.1, .1), (24, 1)):
            row = lead(); row["cooldown"].update(effective_hours=effective, jitter_delta_hours=delta)
            self.assertEqual(self.audit([row])["counts"], {"UNKNOWN": 1})

    def test_reported_cooldown_or_deadline_mismatch_flagged(self):
        row = lead(); row["cooldown"]["reported_in_cooldown"] = True
        self.assertEqual(self.audit([row])["counts"], {"UNKNOWN": 1})
        row = lead(); row["cooldown"]["reported_eligible_at"] = STAMP
        self.assertEqual(self.audit([row])["counts"], {"UNKNOWN": 1})

    def test_extreme_hours_cannot_crash_audit(self):
        row = lead(); row["cooldown"].update(base_hours=1e300, effective_hours=1e300)
        self.assertEqual(self.audit([row])["counts"], {"UNKNOWN": 1})

    def test_nonpending_materials_pool_is_not_replayed(self):
        result = self.audit([lead(status="AWAITING-MATERIALS")])
        self.assertEqual(result["counts"], {"NOT_PENDING_VERIFICATION": 1})
        self.assertEqual(result["queue_writes"], 0)


def question(qid="q1", **changes):
    return {"id": qid, "employer": "Fixture Employer", "policy_scope": "posting-1-form-v1",
            "text": "Location (City)", "field_type": "text", "required": True, "options": [], **changes}


class TrayAuditTests(unittest.TestCase):
    def audit(self, questions, bank=None):
        return audit_tray({"schema_version": 1, "questions": questions, "answer_bank": bank or {}})

    def test_exact_same_scope_deduplicates_without_resolution(self):
        result = self.audit([question(), question("q2")])
        self.assertEqual((result["display_groups"], result["duplicate_presentations_removed"]), (1, 1))
        self.assertEqual(result["items_resolved"], 0)

    def test_different_employer_scope_or_options_not_merged(self):
        for change in ({"employer": "Different"}, {"policy_scope": "other"}, {"options": ["Yes", "No"]}, {"required": False}, {"text": "Not Location (City)"}):
            self.assertEqual(self.audit([question(), question("q2", **change)])["display_groups"], 2)

    def test_missing_scope_and_duplicate_ids_fail_closed(self):
        with self.assertRaises(ContractError): self.audit([question(policy_scope=None)])
        with self.assertRaises(ContractError): self.audit([question(), question()])

    def test_stripe_whatsapp_quarantine_discards_yes_candidate(self):
        bank = {"answers": {"whatsapp": "DISPUTED_YES_SECRET"}, "_provenance": {"whatsapp": {"source": "old"}}}
        result = self.audit([question(employer="Stripe", text="Do you opt-in to WhatsApp messages?", answer_bank_key="whatsapp")], bank)
        group = result["groups"][0]
        self.assertEqual(group["classification"], "CONSENT_QUARANTINED")
        self.assertEqual(group["answer_bank_candidates"], [])
        self.assertNotIn("DISPUTED_YES_SECRET", json.dumps(result))

    def test_bank_values_are_never_exported_or_authorized(self):
        result = self.audit([question(answer_bank_key="location")], {"answers": {"location": "PRIVATE_ADDRESS"}})
        self.assertNotIn("PRIVATE_ADDRESS", json.dumps(result))
        self.assertFalse(result["groups"][0]["answer_bank_candidates"][0]["reuse_authorized"])

    def test_cannot_silently_merge_a_quarantined_question(self):
        result = self.audit([question(), question("q2", quarantined=True)])
        self.assertEqual(result["display_groups"], 2)


class GuardianPatchProposalTests(unittest.TestCase):
    def source(self):
        return '''READY_FLOOR = 5
CHECK_SECONDS = 30
SPAWN_THROTTLE_SECONDS = 600
SPAWN = ["verify_retry.py", "--live", "--async", "--limit", "200"]
def should_fire(ready, actionable, elapsed):
    if ready >= READY_FLOOR:
        return False, "pool healthy"
    if actionable == 0:
        return False, "pool low but nothing actionable (all in cooldown)"
    if elapsed < SPAWN_THROTTLE_SECONDS:
        return False, "spawn throttled"
    return True, "refill"
'''

    def test_exact_reported_source_patch_preserves_other_decisions(self):
        tool = module("supply_patch", "tools/propose_guardian_patch.py")
        before, after = {}, {}
        source = self.source(); revised = tool.propose(source)
        exec(compile(source, "<synthetic-before>", "exec"), before)
        exec(compile(revised, "<synthetic-after>", "exec"), after)
        for ready in range(8):
            for actionable in range(3):
                for elapsed in (0, 599, 600, 1200):
                    if ready >= 5 and actionable == 0:
                        self.assertEqual(after["should_fire"](ready, actionable, elapsed), (False, "buffer full, supply starved"))
                    else:
                        self.assertEqual(before["should_fire"](ready, actionable, elapsed), after["should_fire"](ready, actionable, elapsed))
        for key in ("READY_FLOOR", "CHECK_SECONDS", "SPAWN_THROTTLE_SECONDS", "SPAWN"):
            self.assertEqual(before[key], after[key])

    def test_unknown_or_already_patched_source_refused(self):
        tool = module("supply_patch2", "tools/propose_guardian_patch.py")
        source = self.source()
        for changed in (source.replace("ready >= READY_FLOOR", "ready > READY_FLOOR"), tool.propose(source), "def should_fire(x): return False"):
            with self.assertRaises(ValueError): tool.propose(changed)

    def test_crlf_input_preserved(self):
        tool = module("supply_patch3", "tools/propose_guardian_patch.py")
        result = tool.propose(self.source().replace("\n", "\r\n"))
        self.assertNotIn("\n", result.replace("\r\n", ""))


if __name__ == "__main__":
    unittest.main()
