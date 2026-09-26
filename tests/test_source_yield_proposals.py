#!/usr/bin/env python3
"""Tests for source_yield_proposals.py (workstream 4).

No live HTTP, no queue/ledger/roster/blackboard writes: all fixtures are
synthetic in-memory event lists.
"""
import datetime as dt
import os
import sys
import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "engines"))
import source_yield_proposals as syp

NOW = dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.timezone.utc)


def _ev(event_type, role_id, ts, batch=None, fit=None, reason=""):
    # Fixture attribution is explicit; production never infers this from a role ID.
    details = {"source_id": syp.family_of(role_id)}
    if batch:
        details["batch"] = batch
    if fit is not None:
        details["fit_score"] = fit
    if reason:
        details["reason"] = reason
    return {"ts": ts.isoformat(), "event_type": event_type,
            "role_id": role_id, "details": details}


def _staged(fam_prefix, ts, batch, n_new=1, n_dup=0, fit=80):
    evs = []
    for i in range(n_new):
        evs.append(_ev("staged_ingested", f"{fam_prefix}-ACME-JOB-{i}-20260916",
                       ts, batch=batch, fit=fit))
    for i in range(n_dup):
        evs.append(_ev("staged_rejected", f"{fam_prefix}-ACME-JOB-D{i}-20260916",
                       ts, batch=batch, reason="duplicate role_id in queue"))
    return evs


def test_a_family_mapping_longest_prefix():
    assert syp.family_of("YCM-ACME-1") == "yc-mirrors"
    assert syp.family_of("SWEEP26-GETRO-ACME-1") == "getro"
    assert syp.family_of("CENSUS3X-ACME-1") == "census3x"
    assert syp.family_of("ZZZ-ACME-1") is None  # unmapped: never proposed on
    print("a ok: prefix mapping")


def test_b_decay_bar_fires_demotion_proposal():
    t1 = NOW - dt.timedelta(hours=26)
    t2 = NOW - dt.timedelta(hours=2)
    evs = (_staged("YCM", t1, "staged-ingest-2026-09-15-2200", n_new=16, n_dup=2)
           + _staged("YCM", t2, "staged-ingest-2026-09-16-0400", n_new=11, n_dup=9))
    props, suppressed, healthy = syp.generate(evs, {"yc-mirrors": {"ceiling_days": 7}}, 27, [])
    assert len(props) == 1, f"expected 1 demotion proposal, got {len(props)}"
    p = props[0]
    assert p["kind"] == "demotion" and p["domain"] == "source-yield"
    # blackboard-publish-ready schema: title, evidence, exact change + cost
    for k in ("title", "domain", "evidence", "proposal_or_fix"):
        assert p[k], f"missing {k}"
    assert "yc-mirrors" in p["title"]
    assert "delta-gated" in p["proposal_or_fix"]
    assert "operator approval is required" in p["proposal_or_fix"]
    assert "dup rate" in p["evidence"]
    print("b ok: decay bar -> demotion proposal with publish-ready schema")


def test_c_guard_suppresses_when_source_converts():
    t1 = NOW - dt.timedelta(hours=26)
    t2 = NOW - dt.timedelta(hours=2)
    evs = (_staged("YCM", t1, "staged-ingest-2026-09-15-2200", n_new=16, n_dup=2)
           + _staged("YCM", t2, "staged-ingest-2026-09-16-0400", n_new=11, n_dup=9)
           + [_ev("submitted", "YCM-ACME-CONVERT-20260916",
                   NOW - dt.timedelta(days=3))])
    props, suppressed, healthy = syp.generate(evs, {"yc-mirrors": {"ceiling_days": 7}}, 27, evs)
    assert props == [], f"guard failed: {len(props)} proposals emitted"
    assert len(suppressed) == 1
    assert "GUARD" in suppressed[0]["reason"]
    assert "submission" in suppressed[0]["reason"]
    print("c ok: submission recency guard suppresses demotion")


def test_d_promotion_bar_fires():
    t = NOW - dt.timedelta(hours=5)
    evs = _staged("SWEEP26-GETRO", t, "staged-ingest-2026-09-16-0100",
                  n_new=7, n_dup=0, fit=85)
    roster = {"getro": {"heading": "Getro network boards",
                        "cadence_label": "WEEKLY", "ceiling_days": 7,
                        "last_swept": "2026-09-16"}}
    props, suppressed, healthy = syp.generate(evs, roster, 27, [])
    assert len(props) == 1, f"expected 1 promotion proposal, got {len(props)}"
    p = props[0]
    assert p["kind"] == "promotion" and p["domain"] == "source-yield"
    assert "getro" in p["title"]
    assert "needs human approval" in p["proposal_or_fix"]
    assert "operator approval is required" in p["proposal_or_fix"]
    print("d ok: promotion bar -> promotion proposal")


def test_e_healthy_family_no_proposal():
    t = NOW - dt.timedelta(hours=5)
    evs = _staged("W2A", t, "staged-ingest-2026-09-16-0100",
                  n_new=56, n_dup=7, fit=70)
    props, suppressed, healthy = syp.generate(evs, {}, 27, [])
    assert props == [] and suppressed == []
    assert len(healthy) == 1 and healthy[0]["family"] == "wave2a-ashby-lever"
    print("e ok: healthy source emits no proposal")


def test_f_single_sweep_date_no_decay():
    # 22% dup but only one staging date: no re-sweep pattern, no demotion
    t = NOW - dt.timedelta(hours=5)
    evs = _staged("YCM", t, "staged-ingest-2026-09-16-0100", n_new=27, n_dup=8)
    props, suppressed, healthy = syp.generate(evs, {}, 27, [])
    assert props == [], "demotion fired without an intra-window re-sweep"
    print("f ok: dup rate alone does not demote without re-sweep evidence")


def test_g_skip_families_dedup():
    t1 = NOW - dt.timedelta(hours=26)
    t2 = NOW - dt.timedelta(hours=2)
    evs = (_staged("YCM", t1, "staged-ingest-2026-09-15-2200", n_new=16, n_dup=2)
           + _staged("YCM", t2, "staged-ingest-2026-09-16-0400", n_new=11, n_dup=9))
    props, _, _ = syp.generate(evs, {}, 27, [], skip_families={"yc-mirrors"})
    assert props == [], "already-covered family proposed again"
    print("g ok: open-proposal de-dup skip works")


def test_h_roster_parse_extracts_ceiling():
    import tempfile
    text = ("# Source roster (standing)\n\n"
            "## YC mirrors (company mirrors + /jobs)\n"
            "- Status: ACCEPT\n"
            "- Cadence: WEEKLY (raised 2026-09-16)\n"
            "- Last swept: 2026-09-15 (Arm 16)\n")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        p = f.name
    old = syp.ROSTER
    syp.ROSTER = p
    try:
        roster = syp.parse_roster()
    finally:
        syp.ROSTER = old
        os.unlink(p)
    assert roster["yc-mirrors"]["ceiling_days"] == 7
    assert roster["yc-mirrors"]["last_swept"] == "2026-09-15"
    print("h ok: roster cadence ceiling parsed")


if __name__ == "__main__":
    test_a_family_mapping_longest_prefix()
    test_b_decay_bar_fires_demotion_proposal()
    test_c_guard_suppresses_when_source_converts()
    test_d_promotion_bar_fires()
    test_e_healthy_family_no_proposal()
    test_f_single_sweep_date_no_decay()
    test_g_skip_families_dedup()
    test_h_roster_parse_extracts_ceiling()
    print("all 8 tests passed")


def test_i_source_attribution_is_explicit_only():
    e = {"role_id": "C3X-ACME-1", "details": {"staging_file": "yc-mirrors-delta-leads.json"}}
    assert syp.family_of_event(e) is None
    e["details"]["source_id"] = "yc-mirrors"
    assert syp.family_of_event(e) == "yc-mirrors"
    e["source_id"] = "census3x"
    assert syp.family_of_event(e) is None


def test_j_c3x_prefix_maps_to_census3x():
    assert syp.family_of("C3X-ACCELA-8039607-20260916") == "census3x"
    assert syp.family_of("CENSUS3X-GH-GUSTO-1") == "census3x"
    print("j ok: C3X/CENSUS3X prefixes both map to census3x")


def test_k_roster_anchored_resweep_fires_on_single_window_sweep():
    # The pulse-245 YC case: one staging date inside the window, but the
    # roster says last-swept 2026-09-15 against a WEEKLY ceiling -> violation
    t = NOW - dt.timedelta(hours=4)
    evs = _staged("YCM", t, "staged-ingest-2026-09-16-0307",
                  n_new=27, n_dup=11)
    roster = {"yc-mirrors": {"heading": "YC mirrors (company mirrors + /jobs)",
                             "cadence_label": "WEEKLY", "ceiling_days": 7,
                             "last_swept": "2026-09-15"}}
    props, suppressed, healthy = syp.generate(evs, roster, 30, [])
    assert len(props) == 1, f"expected demotion, got {len(props)}"
    p = props[0]
    assert p["kind"] == "demotion"
    assert "2026-09-16" in p["evidence"] and "2026-09-15" in p["evidence"]
    assert "delta-gated" in p["proposal_or_fix"]
    print("k ok: roster-anchored intra-cadence re-sweep demotes")


def test_l_roster_anchored_resweep_absent_when_outside_ceiling():
    # Sweep 8 days after last-swept against a WEEKLY ceiling: within discipline
    t = NOW - dt.timedelta(hours=4)
    evs = _staged("YCM", t, "staged-ingest-2026-09-16-0307",
                  n_new=27, n_dup=11)
    roster = {"yc-mirrors": {"heading": "YC mirrors (company mirrors + /jobs)",
                             "cadence_label": "WEEKLY", "ceiling_days": 7,
                             "last_swept": "2026-09-08"}}
    props, suppressed, healthy = syp.generate(evs, roster, 30, [])
    assert props == [], f"false demotion fired: {len(props)}"
    assert len(healthy) == 1
    print("l ok: in-cadence sweep is not decayed")


def test_m_resweep_note_carried_into_guard_suppression():
    # Suppressed demotions still name the roster-anchored evidence
    t = NOW - dt.timedelta(hours=4)
    evs = (_staged("YCM", t, "staged-ingest-2026-09-16-0307",
                   n_new=27, n_dup=11)
           + [_ev("submitted", "YCM-ACME-CONVERT-20260916",
                   NOW - dt.timedelta(days=2))])
    roster = {"yc-mirrors": {"heading": "YC mirrors (company mirrors + /jobs)",
                             "cadence_label": "WEEKLY", "ceiling_days": 7,
                             "last_swept": "2026-09-15"}}
    props, suppressed, healthy = syp.generate(evs, roster, 30, evs)
    assert props == [] and len(suppressed) == 1
    assert "2026-09-16" in suppressed[0]["reason"]
    print("m ok: suppression reason carries the resweep evidence")


def test_n_promotion_requires_roster_cadence():
    # A hot one-shot enumeration family (no roster cadence) is not a
    # promotion candidate: the proposed change (a cadence step) is meaningless
    t = NOW - dt.timedelta(hours=5)
    evs = _staged("C3X", t, "staged-ingest-2026-09-16-2100",
                  n_new=299, n_dup=1, fit=78)
    props, suppressed, healthy = syp.generate(evs, {}, 30, [])
    assert props == [], f"promotion fired for unrostered family: {len(props)}"
    assert any(h["family"] == "census3x" for h in healthy)
    print("n ok: promotion requires a rostered cadence")


def test_nonduplicate_rejections_do_not_raise_duplicate_rate():
    evs = _staged("YCM", NOW, "first", n_new=1)
    evs += [_ev("staged_rejected", "YCM-other", NOW, batch="second", reason="missing required field")]
    stats = syp.rank_sources(evs, now=NOW)["yc-mirrors"]
    assert stats["dup_rejected"] == 0
    assert stats["other_rejected"] == 1 and stats["staged_total"] == 2
    assert stats["dup_rate"] == 0


def test_event_and_candidate_deduplication():
    e = _staged("YCM", NOW, "first", n_new=1)[0]
    copied = dict(e, event_id="copy")
    stats = syp.rank_sources([e, e, copied], now=NOW)["yc-mirrors"]
    assert stats["ingested"] == stats["net_new"] == 1
    conflict = dict(e, event_id="x")
    with pytest.raises(ValueError, match="conflicting_event_id"):
        syp.rank_sources([conflict, dict(conflict, role_id="YCM-different")], now=NOW)


def test_aggregate_rejections_count_explicit_reasons():
    e = _ev("staged_rejected", "YCM-aggregate", NOW)
    e["role_id"] = ""
    e["details"].update(aggregate=True, count=3, role_ids=["a", "b", "c"],
        reasons={"duplicate role_id in queue": 2, "missing title": 1},
        reason_role_ids={"duplicate role_id in queue": ["a", "b"], "missing title": ["c"]})
    stats = syp.rank_sources([e], now=NOW)["yc-mirrors"]
    assert stats["dup_rejected"] == 2 and stats["other_rejected"] == 1
    e["details"]["count"] = 4
    with pytest.raises(ValueError, match="aggregate_rejection_count_mismatch"):
        syp.rank_sources([e], now=NOW)


def test_roster_sections_do_not_bleed_and_biweekly_is_fourteen(tmp_path, monkeypatch):
    roster = tmp_path / "roster.md"
    roster.write_text("## YC mirrors (company mirrors + /jobs)\n- Status: ACCEPT\n"
                      "## Getro network boards (VC portfolio job boards)\n"
                      "- Cadence: BIWEEKLY\n- Last swept: 2026-09-15\n")
    monkeypatch.setattr(syp, "ROSTER", str(roster))
    result = syp.parse_roster()
    assert result["yc-mirrors"]["ceiling_days"] is None
    assert result["yc-mirrors"]["last_swept"] == ""
    assert result["getro"]["ceiling_days"] == 14


@pytest.mark.parametrize("raw", ['{bad}\n', '[]\n',
    '{"ts":"2026-09-16T12:00:00","event_type":"staged_ingested"}\n',
    '{"ts":"2026-09-18T12:00:00Z","event_type":"staged_ingested"}\n',
    '{"ts":"2026-09-16T12:00:00Z","event_type":"x","details":{"x":NaN}}\n'])
def test_bad_telemetry_never_becomes_empty_success(tmp_path, monkeypatch, raw):
    telemetry = tmp_path / "events.jsonl"
    telemetry.write_text(raw)
    monkeypatch.setattr(syp, "TELEMETRY", str(telemetry))
    with pytest.raises(ValueError):
        syp.load_events(NOW - dt.timedelta(days=2), now=NOW)


def test_missing_inputs_report_hold(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(syp, "TELEMETRY", str(tmp_path / "missing"))
    assert syp.main(["--now", NOW.isoformat()]) == 2
    import json
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "HOLD" and report["proposals"] == []


def test_unknown_attribution_holds_and_unknown_submission_protects():
    events = _staged("YCM", NOW, "first", n_new=1)
    events[0]["details"].pop("source_id")
    proposals, suppressed, _ = syp.generate(events, {}, 30, [], now=NOW)
    assert not proposals and suppressed[0]["kind"] == "hold"
    assert syp.recent_submitter_families([{"event_type": "submission_claimed", "role_id": "YCM-x"}]) == {"*"}


def test_synthetic_events_excluded_and_missing_fit_not_invented():
    events = _staged("YCM", NOW, "first", n_new=1, fit=True)
    synthetic = _staged("YCM", NOW, "second", n_new=1)[0]
    synthetic["synthetic_test"] = True
    result = syp.rank_sources(events + [synthetic], now=NOW)["yc-mirrors"]
    assert result["ingested"] == 1 and result["fit_median"] is None


def test_incomplete_fit_coverage_does_not_promote():
    events = _staged("YCM", NOW, "first", n_new=6, fit=85)
    for row in events[1:]:
        row["details"].pop("fit_score")
    proposals, _, _ = syp.generate(events, {"yc-mirrors": {"ceiling_days": 7}}, 30, [], now=NOW)
    assert not proposals
    assert syp.rank_sources(events, now=NOW)["yc-mirrors"]["fit_coverage_complete"] is False


def test_ordinary_weekly_sweeps_in_long_window_do_not_demote():
    events = _staged("YCM", NOW - dt.timedelta(days=14), "first", n_new=1, n_dup=5)
    events += _staged("YCM", NOW, "second", n_new=1, n_dup=5)
    proposals, _, _ = syp.generate(events, {"yc-mirrors": {"ceiling_days": 7}}, 24 * 30, [], now=NOW)
    assert not proposals
    proposals, _, _ = syp.generate(events, {}, 24 * 30, [], now=NOW)
    assert not proposals


def test_empty_telemetry_is_hold_not_healthy():
    proposals, suppressed, healthy = syp.generate([], {}, 30, [], now=NOW)
    assert not proposals and not healthy and suppressed[0]["kind"] == "hold"
