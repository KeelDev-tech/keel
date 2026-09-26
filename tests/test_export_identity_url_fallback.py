#!/usr/bin/env python3
"""Regression tests for J-20260924-0725-feed-5624 (pulse-960 READY_COUNT_CONFLICT).

Two defects in export_flow_snapshot.py, both in the export layer (no gate
logic touched):

1. build_identity read only application_url/posting_url for the posting URL.
   The queue schema also stores the posting URL as ats_url / official_url.
   The lane's sole READY lead (SWP16 Anduril, URL only in ats_url/official_url)
   was dropped from the board's lead inventory as identity_unbuildable while
   the pool guardian counted pool.ready=1 -> keel_flow/board.py raised
   READY_COUNT_CONFLICT (pool.ready != nominal_ready), board UNVERIFIED,
   forecast UNKNOWN, perpetual REFRESH_CANONICAL_EXPORT.

2. The active-window predicate marked 00:00-09:59 PT inactive although the
   sleep window (job-pipeline/hidden_files/max-mode.json) is 01:30-10:00 PT.
   At pulse 960 (00:26 PDT) the snapshot carried active=False, so the board's
   forecast degraded to SCHEDULED_PAUSE during 90 genuine operating min/day
   (00:00-01:29). The forced FANOUT scan itself came from the pool guardian's
   ready_floor_breach (ready=1 < floor=5), not from this flag -- the predicate
   fix restores honest forecasting, it does not change any gate.
"""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(KEEL_DIR, "export_flow_snapshot.py")


def load_adapter():
    spec = importlib.util.spec_from_file_location("export_flow_snapshot_5624", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_flow_snapshot_5624"] = mod
    spec.loader.exec_module(mod)
    return mod


A = load_adapter()
LA = ZoneInfo("America/Los_Angeles")

SWP16_ENTRY = {
    "role_id": "SWP16-ANDURILINDUSTRIES-BUSINESS-OPERATIONS-LEAD-TECHNICAL-PROJECTS-20260923-5117374007",
    "company": "andurilindustries",
    "status": "READY",
    "fit_score": 90,
    "ats_url": "https://boards.greenhouse.io/andurilindustries/jobs/5117374007?gh_jid=5117374007",
    "official_url": "https://boards.greenhouse.io/andurilindustries/jobs/5117374007?gh_jid=5117374007",
}


def la(hour, minute):
    return datetime(2026, 9, 24, hour, minute, 0, tzinfo=LA)


# --- 1. identity URL-field fallback -------------------------------------------

class TestBuildIdentityUrlFallback:
    def test_application_url_still_builds(self):
        e = dict(SWP16_ENTRY)
        e["application_url"] = "https://example.com/apply"
        built = A.build_identity(e)
        assert built is not None and built[1] == "https://example.com/apply"

    def test_posting_url_still_builds(self):
        e = dict(SWP16_ENTRY)
        del e["ats_url"]; del e["official_url"]
        e["posting_url"] = "https://example.com/posting"
        built = A.build_identity(e)
        assert built is not None and built[1] == "https://example.com/posting"

    def test_ats_url_only_builds(self):
        # the pulse-960 failure shape: URL only in ats_url
        e = {k: v for k, v in SWP16_ENTRY.items() if k != "official_url"}
        built = A.build_identity(e)
        assert built is not None, "ats_url-only lead must build an identity"
        identity, posting = built
        assert posting == SWP16_ENTRY["ats_url"]
        assert isinstance(identity, str) and len(identity) == 64

    def test_official_url_only_builds(self):
        e = {k: v for k, v in SWP16_ENTRY.items() if k != "ats_url"}
        built = A.build_identity(e)
        assert built is not None, "official_url-only lead must build an identity"
        assert built[1] == SWP16_ENTRY["official_url"]

    def test_preference_order_preserved(self):
        e = dict(SWP16_ENTRY)
        e["application_url"] = "https://example.com/canonical"
        built = A.build_identity(e)
        assert built is not None and built[1] == "https://example.com/canonical"

    def test_no_url_still_fail_closed(self):
        e = {k: v for k, v in SWP16_ENTRY.items()
             if k not in ("application_url", "posting_url", "ats_url", "official_url")}
        assert A.build_identity(e) is None

    def test_missing_employer_still_fail_closed(self):
        e = dict(SWP16_ENTRY)
        del e["company"]
        assert A.build_identity(e) is None

    def test_lead_row_ready_ats_url_only(self):
        # end of the pulse-960 chain: lead_row must not return identity_unbuildable
        row, reason = A.lead_row(dict(SWP16_ENTRY), "2026-09-24T07:26:00+00:00")
        assert row is not None, f"SWP16-shaped READY lead dropped as {reason}"
        assert row["status"] == "READY"
        assert row["role_id"] == SWP16_ENTRY["role_id"]


# --- 2. active-window predicate ------------------------------------------------

class TestActiveOutsideSleepWindow:
    # sleep window 01:30-10:00 PT is inactive; everything else is active
    @pytest.mark.parametrize("hour,minute", [
        (0, 0), (0, 26), (1, 0), (1, 29), (10, 0), (10, 1), (12, 0), (23, 59),
    ])
    def test_active_hours(self, hour, minute):
        assert A.active_outside_sleep_window(la(hour, minute)) is True, \
            f"{hour:02d}:{minute:02d} PT should be ACTIVE"

    @pytest.mark.parametrize("hour,minute", [
        (1, 30), (1, 31), (2, 0), (5, 0), (9, 59),
    ])
    def test_sleep_window_inactive(self, hour, minute):
        assert A.active_outside_sleep_window(la(hour, minute)) is False, \
            f"{hour:02d}:{minute:02d} PT should be INACTIVE (sleep window)"

    def test_pulse960_0026_pdt_is_active(self):
        # pulse 960 ran 2026-09-24 00:26 PDT; the old predicate wrongly said inactive
        assert A.active_outside_sleep_window(la(0, 26)) is True

    def test_old_predicate_would_fail_this(self):
        # guard against regression to the old (la.hour < 10 ...) logic:
        # the new function must disagree with the old one inside 00:00-01:29
        old = not (la(0, 26).hour < 10 or (la(0, 26).hour == 1 and la(0, 26).minute >= 30)
                   or (la(0, 26).hour == 0))
        assert old is False
        assert A.active_outside_sleep_window(la(0, 26)) is True
