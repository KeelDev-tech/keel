"""Regression tests for parsed-identity ATS detection.

Whole-URL substring matching is retired: vendor tokens in a path, fragment
or unrelated query parameter must not attribute the URL to that vendor.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "engines"))
import ats


HOST_CASES = [
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse"),
    ("https://jobs.lever.co/acme/abc-123", "lever"),
    ("https://api.lever.co/v0/postings/acme", "lever"),
    ("https://jobs.ashbyhq.com/acme/123", "ashby"),
    ("https://wd5.myworkdayjobs.com/acme", "workday"),
    ("https://acme.myworkdayjobs.com/en-US/careers", "workday"),
    ("https://app.icims.com/jobs/123", "icims"),
    ("https://jobs.smartrecruiters.com/acme/123", "smartrecruiters"),
    ("https://jobs.jobvite.com/acme/job/123", "jobvite"),
    ("https://acme.breezy.hr/p/123", "breezy"),
    ("https://applytojob.com/apply/123", "applytojob"),
    ("https://ats.rippling.com/acme/jobs/123", "rippling"),
    ("https://tealhq.com/job/123", "tealhq"),
    ("https://egmn.fa.us2.oraclecloud.com/hcmUI/CandidateExperience", "oracle_recruiting_cloud"),
    ("https://www.comeet.co/jobs/acme/123", "comeet"),
    ("https://acme.teamtailor.com/jobs/123", "teamtailor"),
    ("https://careerpuck.com/jobs/123", "careerpuck"),
    ("https://apply.workable.com/acme/j/123", "workable"),
    ("https://acme.recruitee.com/o/123", "recruitee"),
    ("https://acme.pinpointhq.com/123", "pinpoint"),
    ("https://jobs.personio.com/acme/123", "personio"),
    ("https://acme.bamboohr.com/careers/123", "bamboohr"),
    ("https://acme.jazzhr.com/jobs/123", "jazzhr"),
    ("https://acme.freshteam.com/jobs/123", "freshteam"),
    ("https://www.ycombinator.com/companies/acme/jobs/123", "workatastartup"),
]


@pytest.mark.parametrize("url,expected", HOST_CASES)
def test_host_identity_detected(url, expected):
    assert ats.detect_ats(url) == expected


def test_greenhouse_query_param_identity_on_employer_page():
    url = "https://careers.acme.com/positions/7980600?gh_jid=7980600"
    assert ats.detect_ats(url) == "greenhouse"
    key, evidence = ats.detect_ats_explain(url)
    assert key == "greenhouse"
    assert any(kind == "query" for kind, _, _ in evidence)


def test_vendor_token_outside_host_does_not_attribute():
    # The whole-URL regex used to claim these for greenhouse.
    assert ats.detect_ats("https://acme.com/jobs?ref=greenhouse.io-board") == "unknown"
    assert ats.detect_ats("https://acme.com/greenhouse.io/jobs/123") == "unknown"
    assert ats.detect_ats("https://acme.com/jobs#greenhouse.io") == "unknown"


@pytest.mark.parametrize("vendor", ["jazzhr", "freshteam"])
def test_current_vendor_rules_preserve_host_boundaries(vendor):
    # Contract coverage for existing source rules, without contacting a host.
    for url in (f"https://{vendor}.com.example.invalid/jobs/123",
                f"https://not{vendor}.com/jobs/123",
                f"https://example.invalid/{vendor}.com/jobs/123",
                f"https://example.invalid/jobs?ref={vendor}.com"):
        assert ats.detect_ats(url) == "unknown"


def test_host_path_rule_requires_the_path():
    assert ats.detect_ats("https://www.ycombinator.com/companies/acme") == "workatastartup"
    assert ats.detect_ats("https://www.ycombinator.com/") == "unknown"


def test_unparseable_and_empty_inputs_are_unknown():
    for bad in ("", None, "   ", "://[bad", "http://"):
        assert ats.detect_ats(bad) == "unknown"
        assert ats.detect_ats_explain(bad) == ("unknown", [])


def test_host_normalization():
    assert ats.detect_ats("HTTPS://BOARDS.GREENHOUSE.IO/jobs/123") == "greenhouse"
    assert ats.detect_ats("https://user:pass@jobs.lever.co:443/abc") == "lever"
    assert ats.detect_ats("boards.greenhouse.io/jobs/123") == "greenhouse"


def test_ambiguity_is_recorded_not_silenced():
    url = "https://jobs.lever.co/acme/abc?gh_jid=123"
    key, evidence = ats.detect_ats_explain(url)
    assert len(evidence) == 2  # lever host + greenhouse query identifier
    assert {a for _, a, _ in evidence} == {"lever", "greenhouse"}
    assert key in ("lever", "greenhouse")  # stable single answer


def test_platform_key_vocabulary_unchanged():
    # record_outcome.VALID_ATS reads ATS_PATTERNS keys; the vocabulary must
    # not shrink or rename in this migration.
    assert set(ats.ATS_PATTERNS) == {c[1] for c in HOST_CASES}
