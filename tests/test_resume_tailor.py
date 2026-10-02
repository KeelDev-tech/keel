"""Synthetic coverage of advisory requirement feedback; no rendering or authority."""
import copy

import pytest

from engines.resume_tailor import truthfulness_check


@pytest.mark.parametrize("status", ["MISSING", "UNKNOWN", "PARTIAL", None, "", "invalid", 7, [], " partial "])
def test_unresolved_requirement_requests_review(status):
    gaps = truthfulness_check({}, {"hard_requirements": [{"name": "Synthetic skill", "status": status}]})
    assert len(gaps) == 1
    assert "Synthetic skill" in gaps[0]
    assert "verified capability" in gaps[0]


def test_absent_status_is_unknown():
    assert truthfulness_check({}, {"hard_requirements": [{"name": "Synthetic skill"}]})


@pytest.mark.parametrize("status", ["VERIFIED", " verified ", "Verified"])
def test_verified_requirement_needs_no_gap(status):
    assert truthfulness_check({}, {"hard_requirements": [{"name": "Synthetic skill", "status": status}]}) == []


@pytest.mark.parametrize("status", ["MISSING", "UNKNOWN", "PARTIAL", None])
def test_exact_capability_after_whitespace_normalization_suppresses_gap(status):
    assert truthfulness_check({"verified_capabilities": [" Synthetic   skill\t"]},
                              {"hard_requirements": [{"name": "Synthetic skill", "status": status}]}) == []


def test_matching_is_not_fuzzy_and_does_not_change_inputs():
    profile = {"verified_capabilities": ["Synthetic skill basics"]}
    role = {"hard_requirements": [{"name": "Synthetic skill", "status": "PARTIAL"}]}
    before = copy.deepcopy((profile, role))
    assert truthfulness_check(profile, role)
    assert (profile, role) == before


@pytest.mark.parametrize("requirement", [{"name": "  "}, {"name": None}, None, "Synthetic skill"])
def test_unusable_requirement_requests_review(requirement):
    assert truthfulness_check({"verified_capabilities": ["", None]}, {"hard_requirements": [requirement]})
