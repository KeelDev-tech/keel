#!/usr/bin/env python3
"""Tests for source_yield_proposals.py (workstream 4).

No live HTTP, no queue/ledger/roster/blackboard writes: all fixtures are
synthetic in-memory event lists.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "engines"))
import source_yield_proposals as syp

NOW = dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.timezone.utc)


def _ev(event_type, role_id, ts, batch=None, fit=None, reason=""):
    details = {}
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
    props, suppressed, healthy = syp.generate(evs, {}, 27, [])
    assert len(props) == 1, f"expected 1 demotion proposal, got {len(props)}"
    p = props[0]
    assert p["kind"] == "demotion" and p["domain"] == "source-yield"
    # blackboard-publish-ready schema: title, evidence, exact change + cost
    for k in ("title", "domain", "evidence", "proposal_or_fix"):
        assert p[k], f"missing {k}"
    assert "yc-mirrors" in p["title"]
    assert "delta-gated" in p["proposal_or_fix"]
    assert "0 Trent taps" in p["proposal_or_fix"]
    assert "dup rate" in p["evidence"]
    print("b ok: decay bar -> demotion proposal with publish-ready schema")


def test_c_guard_suppresses_when_source_converts():
    t1 = NOW - dt.timedelta(hours=26)
    t2 = NOW - dt.timedelta(hours=2)
    evs = (_staged("YCM", t1, "staged-ingest-2026-09-15-2200", n_new=16, n_dup=2)
           + _staged("YCM", t2, "staged-ingest-2026-09-16-0400", n_new=11, n_dup=9)
           + [_ev("submitted", "YCM-ACME-CONVERT-20260916",
                   NOW - dt.timedelta(days=3))])
    props, suppressed, healthy = syp.generate(evs, {}, 27, evs)
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
    assert "0 Trent taps" in p["proposal_or_fix"]
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


def test_i_staging_file_attribution_takes_precedence():
    # The staging file names the sweep even when the role_id prefix is generic
    e = {"role_id": "C3X-ACME-1", "details": {"staging_file": "yc-mirrors-delta-leads.json"}}
    assert syp.family_of_event(e) == "yc-mirrors"
    e2 = {"role_id": "C3X-ACME-1", "details": {"staging_file": "census3x-20260916-leads.json"}}
    assert syp.family_of_event(e2) == "census3x"
    e3 = {"role_id": "C3X-ACME-1", "details": {}}
    assert syp.family_of_event(e3) == "census3x"  # falls back to prefix
    e4 = {"role_id": "ZZZ-ACME-1", "details": {"staging_file": "misc.json"}}
    assert syp.family_of_event(e4) is None  # unknown -> never proposed on
    print("i ok: staging-file attribution with prefix fallback")


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
