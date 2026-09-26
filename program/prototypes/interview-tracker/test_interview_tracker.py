"""Tests for interview_tracker.py (prototype, G-4/B-26 + G-7/PP-12).

Covers the acceptance scenarios:
 1. 500-submission generated replay reconciles 1:1 with the ledger
     (no missing, no double-counted, state counts sum to 500)
 2. fabricated "congratulations" signal WITHOUT authorization cannot advance
 3. mailbox signals disabled until authorized (fail closed); work after auth
 4. expire_unknown marks stale SUBMITTED as EXPIRED_UNKNOWN with denominator
 5. funnel view labels unknowns honestly (never folded into successes),
     every figure carries a provenance label
"""

import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from interview_tracker import (  # noqa: E402
    Tracker,
    SUBMITTED, REJECTED, WITHDRAWN, INTERVIEW_SCHEDULED, OFFER, HIRED,
    EXPIRED_UNKNOWN,
    EVIDENCE_USER_TAP, EVIDENCE_AUTHORIZED_SIGNAL, EVIDENCE_COHORT_RECONCILIATION,
)


def tap(details=None):
    d = {"at": "2026-09-10T12:00:00Z"}
    if details:
        d.update(details)
    return {"kind": EVIDENCE_USER_TAP, "details": d}


def test_replay_500_reconciles_1_to_1():
    rng = random.Random(20260919)
    t = Tracker()
    base = datetime(2026, 6, 1)
    ledger_ids = []
    for i in range(500):
        sid = f"sub-{i:04d}"
        ledger_ids.append(sid)
        t.register(sid, f"bundle-{i:04d}", base + timedelta(days=rng.randint(0, 90)))
    # random but lawful lifecycle
    for sid in ledger_ids:
        r = rng.random()
        if r < 0.40:
            continue  # stays SUBMITTED
        elif r < 0.60:
            t.advance(sid, REJECTED, tap())
        elif r < 0.65:
            t.advance(sid, WITHDRAWN, tap())
        elif r < 0.85:
            t.advance(sid, INTERVIEW_SCHEDULED, tap())
            r2 = rng.random()
            if r2 < 0.5:
                t.advance(sid, REJECTED, tap())
            elif r2 < 0.7:
                t.advance(sid, OFFER, tap())
                if rng.random() < 0.6:
                    t.advance(sid, HIRED, tap())
        else:
            t.advance(sid, OFFER, tap())
    ok, missing, extra = t.reconcile(ledger_ids)
    assert ok, f"reconcile failed: missing={missing[:5]} extra={extra[:5]}"
    assert missing == [] and extra == []
    fv = t.funnel_view()
    total_states = (fv["rejected"]["count"] + fv["withdrawn"]["count"]
                    + fv["interviews"]["count"] + fv["submitted_pending"]["count"]
                    + fv["expired_unknown"]["count"])
    assert total_states == 500, total_states
    assert fv["applications"]["count"] == 500
    print("ok test_replay_500_reconciles_1_to_1")


def test_fabricated_congratulations_cannot_advance():
    t = Tracker()
    t.register("s1", "b1", datetime(2026, 9, 1))
    fabricated = {
        "kind": "inferred_signal",  # not a lawful evidence kind
        "details": {"source": "vendor_email", "text": "Congratulations! Interview invite"},
    }
    try:
        t.advance("s1", INTERVIEW_SCHEDULED, fabricated)
        raise AssertionError("fabricated signal was accepted")
    except ValueError:
        pass
    # vendor claims by name are equally rejected
    try:
        t.advance("s1", INTERVIEW_SCHEDULED,
                  {"kind": "vendor_claim", "details": {"text": "you got an interview!"}})
        raise AssertionError("vendor claim was accepted")
    except ValueError:
        pass
    assert t._subs["s1"].state == SUBMITTED
    print("ok test_fabricated_congratulations_cannot_advance")


def test_mailbox_signals_fail_closed_until_authorized():
    t = Tracker()  # mailbox NOT authorized by default
    t.register("s2", "b2", datetime(2026, 9, 1))
    ev = {"kind": EVIDENCE_AUTHORIZED_SIGNAL, "details": {"source": "mailbox"}}
    try:
        t.advance("s2", REJECTED, ev)
        raise AssertionError("unauthorized mailbox signal was accepted")
    except ValueError as e:
        assert "not authorized" in str(e)
    assert t._subs["s2"].state == SUBMITTED
    # after the user authorizes the connection, the same signal works
    t.authorize_source("mailbox")
    t.advance("s2", REJECTED, ev)
    assert t._subs["s2"].state == REJECTED
    print("ok test_mailbox_signals_fail_closed_until_authorized")


def test_expire_unknown_cohort_with_denominator():
    t = Tracker()
    as_of = datetime(2026, 9, 19)
    # 10 old submissions (stale), 5 fresh, 2 already rejected, 1 hired
    for i in range(10):
        t.register(f"old-{i}", f"b-old-{i}", datetime(2026, 7, 1))
    for i in range(5):
        t.register(f"new-{i}", f"b-new-{i}", datetime(2026, 9, 10))
    t.register("rej-1", "b-r1", datetime(2026, 7, 1))
    t.advance("rej-1", REJECTED, tap())
    t.register("hire-1", "b-h1", datetime(2026, 7, 1))
    t.advance("hire-1", INTERVIEW_SCHEDULED, tap())
    t.advance("hire-1", OFFER, tap())
    t.advance("hire-1", HIRED, tap())

    cohort = t.expire_unknown(as_of, window_days=30)
    assert sorted(cohort["submission_ids"]) == sorted(f"old-{i}" for i in range(10))
    assert cohort["denominator"] == 17
    assert cohort["state"] == EXPIRED_UNKNOWN
    # old submissions are EXPIRED_UNKNOWN, fresh ones still SUBMITTED
    assert all(t._subs[f"old-{i}"].state == EXPIRED_UNKNOWN for i in range(10))
    assert all(t._subs[f"new-{i}"].state == SUBMITTED for i in range(5))
    # terminal states were untouched
    assert t._subs["rej-1"].state == REJECTED
    assert t._subs["hire-1"].state == HIRED
    # unknowns are never counted as successes
    fv = t.funnel_view()
    assert fv["expired_unknown"]["count"] == 10
    assert fv["expired_unknown"]["denominator"] == 17
    assert fv["offers"]["count"] == 1  # hire-1 only
    assert "never counted as successes" in fv["expired_unknown"]["note"]
    print("ok test_expire_unknown_cohort_with_denominator")


def test_terminal_states_only_correctable_by_reconciliation():
    t = Tracker()
    t.register("s3", "b3", datetime(2026, 9, 1))
    t.advance("s3", REJECTED, tap())
    # user_tap cannot resurrect a rejected submission
    try:
        t.advance("s3", INTERVIEW_SCHEDULED, tap())
        raise AssertionError("terminal escape without reconciliation was accepted")
    except ValueError:
        pass
    # cohort_reconciliation can (honest correction, fully logged)
    t.advance("s3", INTERVIEW_SCHEDULED, {
        "kind": EVIDENCE_COHORT_RECONCILIATION,
        "details": {"correction": "employer re-contacted; prior rejection was an error"},
    })
    assert t._subs["s3"].state == INTERVIEW_SCHEDULED
    print("ok test_terminal_states_only_correctable_by_reconciliation")


def test_funnel_view_provenance_and_honest_unknowns():
    t = Tracker()
    t.register("a", "ba", datetime(2026, 8, 1))
    t.register("b", "bb", datetime(2026, 8, 1))
    t.register("c", "bc", datetime(2026, 8, 1))
    t.register("d", "bd", datetime(2026, 8, 1))
    t.advance("a", INTERVIEW_SCHEDULED, tap())
    t.advance("a", OFFER, tap())
    t.advance("b", REJECTED, tap())
    t.advance("c", WITHDRAWN, tap())
    # d stays SUBMITTED, then expires
    t.expire_unknown(datetime(2026, 9, 19), window_days=30)
    fv = t.funnel_view()
    for key in ("applications", "responses", "interviews", "offers", "hires",
                "rejected", "withdrawn", "expired_unknown", "submitted_pending"):
        cell = fv[key]
        assert "count" in cell and "denominator" in cell and "provenance" in cell, key
        assert cell["provenance"] != "UNMEASURED", key
        assert cell["denominator"] == 4, key
    assert fv["interviews"]["count"] == 1      # a only
    assert fv["responses"]["count"] == 2      # a + b
    assert fv["offers"]["count"] == 1         # a
    assert fv["expired_unknown"]["count"] == 1  # d
    assert fv["submitted_pending"]["count"] == 0
    assert "uncertainty_note" in fv
    print("ok test_funnel_view_provenance_and_honest_unknowns")


def test_reconcile_detects_missing_and_extra():
    t = Tracker()
    t.register("x1", "bx1", datetime(2026, 9, 1))
    t.register("x2", "bx2", datetime(2026, 9, 1))
    ok, missing, extra = t.reconcile(["x1", "x3"])  # x2 extra, x3 missing
    assert not ok
    assert missing == ["x3"] and extra == ["x2"]
    print("ok test_reconcile_detects_missing_and_extra")


if __name__ == "__main__":
    test_replay_500_reconciles_1_to_1()
    test_fabricated_congratulations_cannot_advance()
    test_mailbox_signals_fail_closed_until_authorized()
    test_expire_unknown_cohort_with_denominator()
    test_terminal_states_only_correctable_by_reconciliation()
    test_funnel_view_provenance_and_honest_unknowns()
    test_reconcile_detects_missing_and_extra()
    print("ALL 7 TESTS PASSED")
