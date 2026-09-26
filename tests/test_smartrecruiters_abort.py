"""Abort-retention tests for the SmartRecruiters discovery adapter.

Synthetic http_get callables only — no network. Covers:
  - abort mid-pagination -> structured ABORT_LOG record (list-page + offset,
    exception class, completed-items watermark, UNKNOWN_TRANSPORT_ABORT)
  - RateLimited (429) still propagates as RateLimited (hard stop intact)
  - abort during a per-posting detail fetch -> detail-fetch + posting_id
  - exception carrying .status/.headers -> retained in the record
  - sweep_platform: aborted board -> summary["aborts"], items NOT counted
  - normal completion -> no abort record
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

# engines/ on sys.path gives `ats_discovery` (package) plus the flat engine
# modules (dedupe_gate, queue_intake, staging_ingest, keel_paths) that
# sweep.py imports at module top.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.abspath(os.path.join(_THIS_DIR, "..", "engines"))
sys.path.insert(0, _ENGINES)

from ats_discovery import smartrecruiters as sr  # noqa: E402
from ats_discovery import sweep  # noqa: E402


# ---------------------------------------------------------------------------
# synthetic HTTP fixtures
# ---------------------------------------------------------------------------

def _item(i):
    return {
        "id": "P-%d" % i,
        "company": {"name": "Acme"},
        "name": "Operations Manager",
        "location": {"city": "Napa", "region": "CA", "country": "US",
                     "remote": False},
        "releasedDate": "2026-09-10",
        "typeOfEmployment": "full-time",
        "function": "Operations",
        "industry": "Tech",
        "experienceLevel": "mid",
        "ref": "REF-%d" % i,
    }


def _page_body(items, total_found):
    return json.dumps({
        "totalFound": total_found,
        "limit": 100,
        "offset": 0,
        "content": items,
    })


def _detail_body(posting_id):
    return json.dumps({
        "postingUrl": "https://jobs.smartrecruiters.com/Acme/%s" % posting_id,
        "applyUrl": "https://jobs.smartrecruiters.com/Acme/%s" % posting_id,
        "active": True,
        "jobAd": "<p>do operational things</p>",
    })


def _make_getter(pages, total_found, raise_on=None):
    """pages: {offset: [items]}. raise_on(url) -> exception to raise or None."""
    def http_get(url):
        if raise_on is not None:
            exc = raise_on(url)
            if exc is not None:
                raise exc
        if "offset=" in url:
            off = int(url.split("offset=")[1].split("&")[0])
            return 200, "application/json", _page_body(pages.get(off, []),
                                                       total_found)
        posting_id = url.rsplit("/", 1)[-1]
        return 200, "application/json", _detail_body(posting_id)
    return http_get


@pytest.fixture(autouse=True)
def _clean_logs():
    sr.ABORT_LOG.clear()
    sr.SKIP_LOG.clear()
    yield
    sr.ABORT_LOG.clear()
    sr.SKIP_LOG.clear()


# ---------------------------------------------------------------------------
# fetch_board abort cases
# ---------------------------------------------------------------------------

def test_abort_mid_pagination_retains_stage_and_watermark():
    # totalFound=250 -> list pages at offsets 0, 100, 200; blow up on the
    # third page (unexplained transport failure: empty message).
    pages = {0: [_item(0), _item(1)], 100: [_item(2), _item(3)],
             200: [_item(4), _item(5)]}

    def raise_on(url):
        if "offset=200" in url:
            return ConnectionError()  # no message -> unexplained
        return None

    with pytest.raises(ConnectionError):
        sr.fetch_board("acme", _make_getter(pages, 250, raise_on))

    assert len(sr.ABORT_LOG) == 1
    rec = sr.ABORT_LOG[0]
    assert rec["platform"] == "smartrecruiters"
    assert rec["slug"] == "acme"
    assert rec["stage"] == "list-page"
    assert rec["offset"] == 200
    assert rec["exception_class"] == "ConnectionError"
    # watermark = fully completed items from pages 0 and 100 (2 each)
    assert rec["completed_items"] == 4
    assert rec["http_status"] is None
    assert rec["http_headers"] is None
    assert rec["reason_code"] == "UNKNOWN_TRANSPORT_ABORT"
    # records must survive the run-output JSON dump in sweep.main()
    json.dumps(rec)


def test_ratelimited_still_propagates_as_ratelimited():
    pages = {0: [_item(0)]}

    def http_get(url):
        return 429, "application/json", "{}"

    with pytest.raises(sr.RateLimited):
        sr.fetch_board("acme", http_get)


def test_abort_during_detail_fetch_records_posting_id():
    pages = {0: [_item(0), _item(1)]}

    def raise_on(url):
        if url.rstrip("/").endswith("/postings/P-1"):
            return ValueError("detail transport blew up")
        return None

    with pytest.raises(ValueError):
        sr.fetch_board("acme", _make_getter(pages, 2, raise_on))

    assert len(sr.ABORT_LOG) == 1
    rec = sr.ABORT_LOG[0]
    assert rec["stage"] == "detail-fetch"
    assert rec["posting_id"] == "P-1"
    assert rec["exception_class"] == "ValueError"
    # P-0 completed (detail fetched OK); P-1 never completed
    assert rec["completed_items"] == 1


def test_status_and_headers_retained_from_exception():
    class Httpish(Exception):
        def __init__(self):
            super().__init__("gateway hiccup")
            self.status = 503
            self.headers = {"retry-after": "30", "x-request-id": "abc-123"}

    pages = {0: [_item(0)], 100: [_item(1)]}

    def raise_on(url):
        if "offset=100" in url:
            return Httpish()
        return None

    with pytest.raises(Httpish):
        sr.fetch_board("acme", _make_getter(pages, 250, raise_on))

    assert len(sr.ABORT_LOG) == 1
    rec = sr.ABORT_LOG[0]
    assert rec["stage"] == "list-page"
    assert rec["offset"] == 100
    assert rec["http_status"] == 503
    assert rec["http_headers"] == {"retry-after": "30",
                                   "x-request-id": "abc-123"}
    assert rec["reason_code"] == "TRANSPORT_ABORT"
    json.dumps(rec)


def test_normal_completion_records_no_abort():
    # totalFound=150 -> pages at offsets 0 and 100, both succeed.
    pages = {0: [_item(0), _item(1)], 100: [_item(2), _item(3)]}
    results = sr.fetch_board("acme", _make_getter(pages, 150))
    assert len(results) == 4
    assert sr.ABORT_LOG == []


# ---------------------------------------------------------------------------
# sweep_platform integration
# ---------------------------------------------------------------------------

class _AbortingAdapter:
    SKIP_LOG = []
    ABORT_LOG = []

    @staticmethod
    def fetch_board(slug, getter):
        rec = {
            "platform": "smartrecruiters",
            "slug": slug,
            "stage": "list-page",
            "offset": 0,
            "exception_class": "ConnectionError",
            "exception_message": "",
            "completed_items": 0,
            "http_status": None,
            "http_headers": None,
            "reason_code": "UNKNOWN_TRANSPORT_ABORT",
        }
        _AbortingAdapter.ABORT_LOG.append(rec)
        raise ConnectionError()


def test_sweep_platform_abort_goes_to_aborts_not_raw(monkeypatch):
    monkeypatch.setattr(sweep, "PACE_BOARD", 0)
    boards = {"acme": {"status": "active", "employer": "Acme"}}
    ctx = {"blocked": [], "ledger": [], "queue": [], "staged": [],
           "desc_hashes": {}}
    args = SimpleNamespace(no_triage=True)

    summary = sweep.sweep_platform("smartrecruiters", _AbortingAdapter,
                                   boards, ctx, args)

    assert len(summary["aborts"]) == 1
    rec = summary["aborts"][0]
    assert rec["reason_code"] == "UNKNOWN_TRANSPORT_ABORT"
    assert rec["stage"] == "list-page"
    assert rec["completed_items"] == 0
    # aborted board contributes nothing as observations
    assert summary["raw"] == 0
    assert summary["staged"] == 0
    assert summary["completed_observations"] == 0
    assert summary["completed_boards"] == 0
    assert ctx["staged"] == []
    assert any("acme: fetch error" in s for s in summary["skipped"])
    # run output must stay JSON-serializable for main()
    json.dumps(summary)
