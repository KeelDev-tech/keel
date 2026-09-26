#!/usr/bin/env python3
"""Regression tests for ARM 3 (2026-09-20): readiness evidence mapping.

Blackboard J-20260920-1831-feed-3884: the exporter fail-closed 5 of 6
readiness gates on hardcoded placeholders (attempt_state UNKNOWN,
launch_lock_held None, history_reconciled False, approval_valid False,
packet_present False, dependencies "unobserved"), so executable_ready read
0 even for leads the lane had genuinely staged. export_flow_snapshot 1.3.0
maps each gate to real ledger / lock-dir / buffer evidence; every mapping
fails closed to the v1 placeholder posture when its evidence is absent.

All fixtures synthetic; nothing touches the live queues, telemetry, tray,
ledger, locks, or staged-launches. Run from ~/workspace/keel.

PII posture: packet fixtures use example identifiers only (Alex Applicant
Doe / alex.applicant1@example.invalid) so the published history embeds no
real personal identifiers.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_flow_snapshot as ex
from keel_local.readiness import (DEPENDENCIES, dependency_hash,
                                  evaluate_readiness)

NOW = datetime.now(timezone.utc)


def queue_entry(**over):
    entry = {"role_id": "TEST-ROLE-1", "company": "TestCo",
             "ats": "greenhouse",
             "application_url": "https://example.org/jobs/1",
             "fit_score": 84, "action_band": "APPLY", "status": "READY",
             "unresolved": [], "resume_lane": "B-GM-OPS"}
    entry.update(over)
    return entry


def identity_of(entry):
    ident, _ = ex.build_identity(entry)
    return ident


def evidence(**over):
    ev = {"staged_packets": {}, "lock_names": set(), "lock_dir": "/nonexistent",
          "ledger_submitted": set(), "attempt_by_app": {},
          "history_complete": True, "now": NOW}
    ev.update(over)
    return ev


def staged_rec(packet=None, identity_covered=True, staged_at=None):
    return {"entry": {"role_id": "TEST-ROLE-1"},
            "packet": packet, "identity_covered": identity_covered,
            "staged_at": staged_at or NOW - timedelta(hours=1)}


def packet(brief_markers=True, **over):
    brief = ("Submit a job application for Alex Applicant Doe to TestCo.\n"
             "STEP 0\n")
    if brief_markers:
        brief += ("  - first_name: Alex\n"
                  "  - email: alex.applicant1@example.invalid  [banked]\n")
    else:
        brief += "  - first_name: NEEDS_INPUT\n"
    pkt = {"brief": brief,
           "upload_files": ["/tmp/resume.pdf"],
           "ats_url": "https://example.org/jobs/1"}
    pkt.update(over)
    return pkt


def write_staged(tmp_path, role_id="TEST-ROLE-1", brief_markers=True,
                 packet_path=None):
    """Write a staged-launches.json + packet under tmp_path; return path."""
    pkt_path = packet_path or str(tmp_path / "packet.json")
    if packet_path is None:
        with open(pkt_path, "w") as f:
            json.dump(packet(brief_markers=brief_markers), f)
    staged = {"staged": [{
        "role_id": role_id, "company": "TestCo", "title": "T",
        "packet_path": pkt_path,
        "staged_at": (NOW - timedelta(hours=1)).isoformat()}]}
    spath = str(tmp_path / "staged-launches.json")
    with open(spath, "w") as f:
        json.dump(staged, f)
    return spath


def write_lease(lock_dir, role_id, acquired_ago_h=0.5, ttl_hours=2):
    os.makedirs(lock_dir, exist_ok=True)
    fname = hashlib.sha256(role_id.encode()).hexdigest() + ".json"
    with open(os.path.join(lock_dir, fname), "w") as f:
        json.dump({"role_id": role_id, "task_id": "t-1", "owner": "lane",
                   "acquired_at": (NOW - timedelta(hours=acquired_ago_h)).isoformat(),
                   "ttl_hours": ttl_hours}, f)


# --- packet_present / approval_valid ------------------------------------------

def test_packet_present_with_identity_coverage(tmp_path):
    spath = write_staged(tmp_path)
    staged = ex.load_staged_packets(spath)
    rec = staged["TEST-ROLE-1"]
    assert rec["packet"] is not None and rec["identity_covered"] is True
    ev = evidence(staged_packets=staged)
    row, reason = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert reason is None
    assert row["packet_present"] is True
    assert row["approval_valid"] is True
    exp = ex.parse_ts((NOW - timedelta(hours=1)).isoformat())
    got = ex.parse_ts(row["approval_expires_at"])
    assert abs((got - exp).total_seconds() - 24 * 3600) < 5


def test_packet_fail_closed_identity_withheld(tmp_path):
    spath = write_staged(tmp_path, brief_markers=False)
    staged = ex.load_staged_packets(spath)
    assert staged["TEST-ROLE-1"]["identity_covered"] is False
    ev = evidence(staged_packets=staged)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["packet_present"] is False
    assert row["approval_valid"] is False
    assert row["approval_expires_at"] is None


def test_packet_fail_closed_missing_path(tmp_path):
    spath = write_staged(tmp_path, packet_path="/nonexistent/p.json")
    staged = ex.load_staged_packets(spath)
    assert staged["TEST-ROLE-1"]["packet"] is None
    ev = evidence(staged_packets=staged)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["packet_present"] is False
    assert row["approval_valid"] is False


def test_packet_fail_closed_no_staged_entry():
    ev = evidence(staged_packets={})
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["packet_present"] is False
    assert row["approval_valid"] is False


def test_load_staged_packets_never_raises(tmp_path):
    assert ex.load_staged_packets("/nonexistent/staged.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ex.load_staged_packets(str(bad)) == {}


# --- mechanical dependency pin -------------------------------------------------

def test_dependency_pin_mechanical_when_packet_present(tmp_path):
    spath = write_staged(tmp_path)
    staged = ex.load_staged_packets(spath)
    ev = evidence(staged_packets=staged)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    deps = row["dependencies"]
    assert set(deps) == set(DEPENDENCIES)
    assert all(type(v) is str and v.strip() for v in deps.values())
    assert row["packet_dependency_hash"] == dependency_hash(deps)
    # the pin is content-derived: a rebuilt packet changes the pin
    pkt2 = packet(brief_markers=True)
    pkt2["brief"] += "  - extra: changed\n"
    deps2 = ex.packet_dependencies(queue_entry(), pkt2, NOW, "browser")
    assert dependency_hash(deps2) != row["packet_dependency_hash"]


def test_dependency_pin_unobserved_without_packet():
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), evidence())
    assert all(v == "unobserved" for v in row["dependencies"].values())
    assert row["packet_dependency_hash"] is None


# --- launch locks ---------------------------------------------------------------

def test_lock_live_held(tmp_path):
    lock_dir = str(tmp_path / "locks")
    write_lease(lock_dir, "TEST-ROLE-1", acquired_ago_h=0.5, ttl_hours=2)
    ev = evidence(lock_dir=lock_dir,
                  lock_names=set(os.listdir(lock_dir)))
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["launch_lock_held"] is True


def test_lock_expired_not_held(tmp_path):
    lock_dir = str(tmp_path / "locks")
    write_lease(lock_dir, "TEST-ROLE-1", acquired_ago_h=3, ttl_hours=2)
    ev = evidence(lock_dir=lock_dir,
                  lock_names=set(os.listdir(lock_dir)))
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["launch_lock_held"] is False


def test_lock_absent_not_held(tmp_path):
    lock_dir = str(tmp_path / "locks")
    os.makedirs(lock_dir)
    ev = evidence(lock_dir=lock_dir, lock_names=set())
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["launch_lock_held"] is False


def test_lock_corrupt_unknown(tmp_path):
    lock_dir = str(tmp_path / "locks")
    os.makedirs(lock_dir, exist_ok=True)
    fname = hashlib.sha256(b"TEST-ROLE-1").hexdigest() + ".json"
    with open(os.path.join(lock_dir, fname), "w") as f:
        f.write("{corrupt")
    ev = evidence(lock_dir=lock_dir,
                  lock_names=set(os.listdir(lock_dir)))
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["launch_lock_held"] is None


def test_lock_registry_unreadable_unknown():
    ev = evidence(lock_dir="/nonexistent", lock_names=None)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["launch_lock_held"] is None


# --- attempt_state / history_reconciled ------------------------------------------

def test_attempt_submitted_in_ledger():
    ev = evidence(ledger_submitted={"TEST-ROLE-1"})
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["attempt_state"] == "SUBMITTED"
    assert row["history_reconciled"] is False


def test_attempt_intent_outstanding():
    ident = identity_of(queue_entry())
    ev = evidence(attempt_by_app={ident: (NOW, "INTENT")},
                  history_complete=True)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["attempt_state"] == "INTENT"
    assert row["history_reconciled"] is True


def test_attempt_dispatched_outstanding():
    ident = identity_of(queue_entry())
    ev = evidence(attempt_by_app={ident: (NOW, "DISPATCHED")},
                  history_complete=True)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["attempt_state"] == "DISPATCHED"


def test_attempt_none_when_no_records():
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), evidence())
    assert row["attempt_state"] == "NONE"
    assert row["history_reconciled"] is True


def test_attempt_unknown_when_journal_incomplete():
    ident = identity_of(queue_entry())
    ev = evidence(attempt_by_app={ident: (NOW, "INTENT")},
                  history_complete=False)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["attempt_state"] == "UNKNOWN"
    assert row["history_reconciled"] is False


def test_attempt_cancelled_is_authoritative_not_submitted():
    ident = identity_of(queue_entry())
    ev = evidence(attempt_by_app={ident: (NOW, "CANCELLED_BEFORE_DISPATCH")},
                  history_complete=True)
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert row["attempt_state"] == "AUTHORITATIVE_NOT_SUBMITTED"


def test_attempt_latest_event_wins():
    ident = identity_of(queue_entry())
    events = [
        {"application_id": ident, "state": "INTENT",
         "observed_at": (NOW - timedelta(hours=2)).isoformat()},
        {"application_id": ident, "state": "DISPATCHED",
         "observed_at": (NOW - timedelta(hours=1)).isoformat()},
    ]
    by_app = ex.attempt_state_by_application(events)
    assert by_app[ident][1] == "DISPATCHED"


# --- end-to-end through the real contract ---------------------------------------

def test_e2e_fully_evidenced_row_is_executable(tmp_path):
    """A lead with genuine evidence on every gate evaluates EXECUTABLE --
    the exporter can report executable, not just 0."""
    spath = write_staged(tmp_path)
    staged = ex.load_staged_packets(spath)
    lock_dir = str(tmp_path / "locks")
    os.makedirs(lock_dir)
    ev = evidence(staged_packets=staged, lock_dir=lock_dir,
                  lock_names=set(), history_complete=True)
    row, reason = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    assert reason is None
    res = evaluate_readiness(row, now=NOW, maximum_age_seconds=900)
    assert res.executable is True, res.reasons
    assert res.reasons == ()


def test_e2e_staged_intent_holds_but_packet_shows(tmp_path):
    """A staged lead with an outstanding lane INTENT keeps attempt_hold --
    the board must not double-count staged supply as fresh -- while the
    placeholder gates clear."""
    spath = write_staged(tmp_path)
    staged = ex.load_staged_packets(spath)
    ident = identity_of(queue_entry())
    lock_dir = str(tmp_path / "locks")
    os.makedirs(lock_dir)
    ev = evidence(staged_packets=staged, lock_dir=lock_dir,
                  lock_names=set(), history_complete=True,
                  attempt_by_app={ident: (NOW, "INTENT")})
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), ev)
    res = evaluate_readiness(row, now=NOW, maximum_age_seconds=900)
    assert res.executable is False
    assert "attempt_hold" in res.reasons
    for cleared in ("packet_missing", "packet_dependencies_changed",
                    "approval_unverified", "launch_lock_held_or_unknown",
                    "legacy_history_unreconciled"):
        assert cleared not in res.reasons, cleared


def test_e2e_no_evidence_keeps_v1_fail_closed():
    """Without an evidence dict the row keeps the exact v1 placeholder
    posture (existing-test compatibility)."""
    row, reason = ex.lead_row(queue_entry(), NOW.isoformat())
    assert reason is None
    assert row["attempt_state"] == "UNKNOWN"
    assert row["launch_lock_held"] is None
    assert row["history_reconciled"] is False
    assert row["approval_valid"] is False
    assert row["packet_present"] is False
    assert all(v == "unobserved" for v in row["dependencies"].values())
    assert row["packet_dependency_hash"] is None
    res = evaluate_readiness(row, now=NOW, maximum_age_seconds=900)
    for r in ("attempt_hold", "launch_lock_held_or_unknown",
              "legacy_history_unreconciled", "approval_unverified",
              "packet_missing", "packet_dependencies_changed"):
        assert r in res.reasons, r


def test_e2e_empty_evidence_dict_is_fail_closed():
    """An evidence dict with empty observations also fails closed --
    absence of evidence is never read as evidence of absence."""
    row, _ = ex.lead_row(queue_entry(), NOW.isoformat(), evidence(
        staged_packets={}, lock_names=set(), history_complete=True))
    assert row["packet_present"] is False
    assert row["launch_lock_held"] is False  # empty registry dir: no lease
    assert row["attempt_state"] == "NONE"    # ledger+journal checked, empty
    assert row["history_reconciled"] is True


# --- identity-marker runtime override (ARM02, 2026-09-22, pulse-806) ----------
# Pulse-805 root-caused executable_ready=0 as an instrument artifact: commit
# 3e0a8b2 sanitized PACKET_IDENTITY_MARKERS to example values, so every real
# packet's brief failed identity_covered by construction. Markers are now
# env-configurable with a restricted private-file injection path; the example
# default keeps public CI / committed-history behavior unchanged.


@pytest.fixture(autouse=True)
def _pin_example_markers(monkeypatch):
    """Pin the example-value markers for every test unless the test itself
    re-pins. load_staged_packets reads the module global at call time, so a
    module-level override suffices; the host's private markers file (0600)
    cannot leak into CI or other machines' runs."""
    monkeypatch.setattr(ex, "PACKET_IDENTITY_MARKERS",
                        ex._EXAMPLE_IDENTITY_MARKERS)
    yield


def test_identity_marker_default_remains_example(tmp_path, monkeypatch):
    """With neither env var nor private file present, the loader returns the
    example-value default: committed-history behavior is unchanged."""
    monkeypatch.delenv(ex._IDENTITY_MARKERS_ENV, raising=False)
    monkeypatch.setattr(ex, "_IDENTITY_MARKERS_FILE",
                        str(tmp_path / "no-such-markers.json"))
    assert ex._load_identity_markers() == ex._EXAMPLE_IDENTITY_MARKERS
    assert ex._EXAMPLE_IDENTITY_MARKERS == (
        "alex.applicant1@example.invalid", "Alex Applicant Doe")


def test_identity_marker_env_override(tmp_path, monkeypatch):
    """KEEL_PACKET_IDENTITY_MARKERS_JSON injects custom markers; malformed
    input fails closed to the example default."""
    monkeypatch.delenv(ex._IDENTITY_MARKERS_ENV, raising=False)
    monkeypatch.setattr(ex, "_IDENTITY_MARKERS_FILE",
                        str(tmp_path / "no-such-markers.json"))
    monkeypatch.setenv(ex._IDENTITY_MARKERS_ENV,
                       '["email@example.invalid","Name Example"]')
    assert ex._load_identity_markers() == ("email@example.invalid",
                                           "Name Example")
    monkeypatch.setenv(ex._IDENTITY_MARKERS_ENV, "{not json")
    assert ex._load_identity_markers() == ex._EXAMPLE_IDENTITY_MARKERS
    monkeypatch.setenv(ex._IDENTITY_MARKERS_ENV, '["only-one"]')
    # single marker is a valid non-empty list; still honored
    assert ex._load_identity_markers() == ("only-one",)


def test_identity_marker_file_override(tmp_path, monkeypatch):
    """The restricted private file supplies markers; empty or malformed files
    fail closed to the example default."""
    monkeypatch.delenv(ex._IDENTITY_MARKERS_ENV, raising=False)
    fpath = tmp_path / "packet-identity-markers.json"
    fpath.write_text(json.dumps({"markers": ["m1@example.invalid",
                                             "Marker One"]}))
    monkeypatch.setattr(ex, "_IDENTITY_MARKERS_FILE", str(fpath))
    assert ex._load_identity_markers() == ("m1@example.invalid", "Marker One")
    fpath.write_text(json.dumps({"markers": []}))
    assert ex._load_identity_markers() == ex._EXAMPLE_IDENTITY_MARKERS
    fpath.write_text("not json at all")
    assert ex._load_identity_markers() == ex._EXAMPLE_IDENTITY_MARKERS


def test_identity_covered_recovers_with_injected_markers(tmp_path,
                                                        monkeypatch):
    """With injected markers matching a packet brief, identity_covered
    recovers from the instrument-blocked False to True; with the example
    default the same brief fails closed. Fixture markers are synthetic so no
    real identity appears in this file."""
    markers = ("real-test-1@example.invalid", "Real Testone")
    fpath = tmp_path / "packet-identity-markers.json"
    fpath.write_text(json.dumps({"markers": list(markers)}))
    monkeypatch.delenv(ex._IDENTITY_MARKERS_ENV, raising=False)
    monkeypatch.setattr(ex, "_IDENTITY_MARKERS_FILE", str(fpath))
    brief = ("Submit a job application for Real Testone to TestCo.\n"
             "STEP 0\n"
             "  - first_name: Real\n"
             "  - email: real-test-1@example.invalid  [banked]\n")
    pkt_path = str(tmp_path / "packet.json")
    with open(pkt_path, "w") as f:
        json.dump({"brief": brief, "upload_files": ["/tmp/resume.pdf"],
                   "ats_url": "https://example.org/jobs/1"}, f)
    spath = write_staged(tmp_path, packet_path=pkt_path)
    # Example defaults on an injected-marker brief: fails closed (pulse-805)
    monkeypatch.setattr(ex, "PACKET_IDENTITY_MARKERS",
                        ex._EXAMPLE_IDENTITY_MARKERS)
    before = ex.load_staged_packets(spath)
    assert before["TEST-ROLE-1"]["identity_covered"] is False
    # Injected markers: the gauge recovers
    monkeypatch.setattr(ex, "PACKET_IDENTITY_MARKERS",
                        ex._load_identity_markers())
    after = ex.load_staged_packets(spath)
    assert after["TEST-ROLE-1"]["packet"] is not None
    assert after["TEST-ROLE-1"]["identity_covered"] is True


def test_identity_markers_repo_hygiene():
    """The committed source embeds ONLY example-value identity markers.

    Guards commit 3e0a8b2's sanitization boundary: a real applicant
    identifier must never re-enter the source tree. Private markers ride
    in the 0600 file (git-ignored) or the env var — both outside the repo.
    """
    src = Path(ex.__file__).read_text()
    # Every committed literal marker is an example value: example-domain
    # emails, or example-persona names.
    for m in ex._EXAMPLE_IDENTITY_MARKERS:
        ml = m.lower()
        assert ("@example.invalid" in ml or "example" in ml
                or "applicant" in ml), m
    # The private file path stays under hidden_files (outside the
    # public-synced tree); the env var is the other injection path.
    assert "hidden_files" in ex._IDENTITY_MARKERS_FILE
    # No non-example email literal anywhere in the source.
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                        src)
    assert emails, "expected at least the example literals"
    for em in emails:
        assert em.endswith("@example.invalid"), em
