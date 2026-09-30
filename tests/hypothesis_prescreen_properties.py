"""Offline property regressions for the source-evidence prescreen invariant.

This file is invoked explicitly by CI and intentionally does not match the
stdlib unittest discovery pattern so Hypothesis remains test-only.
"""
import os
from pathlib import Path
import sys
import tempfile

from hypothesis import given, settings, strategies as st

ENGINES = Path(__file__).resolve().parents[1] / "engines"
sys.path.insert(0, str(ENGINES))
_field_protocol_dir = tempfile.TemporaryDirectory(prefix="keel-hypothesis-frp-")
os.environ["FIELD_PROTOCOL_DIR"] = _field_protocol_dir.name
import prescreen  # noqa: E402

URL = "https://fixture.example/jobs/1"
SAFE_POSTING = "Synthetic posting evidence for a standard remote role."


def complete_packet():
    return {
        "role_id": "TEST-HYPOTHESIS-1",
        "company": "Fixture",
        "title": "Synthetic role",
        "ats_url": URL,
        "brief": "Synthetic test brief.",
        "form_intel": {
            "ats": "fixture",
            "source_url": URL,
            "questions": [],
            "rendered_option_fetch_needed": [],
            "extraction_complete": True,
        },
        "form_intel_complete": True,
        "posting_text": SAFE_POSTING,
        "posting_text_complete": True,
        "posting_text_source": "synthetic fixture",
    }


PROPERTY_SETTINGS = settings(max_examples=60, deadline=None,
                              derandomize=True, database=None)


@PROPERTY_SETTINGS
@given(form_complete=st.booleans(), posting_complete=st.booleans(),
       noise=st.text(max_size=80))
def test_incomplete_evidence_flags_never_produce_clean(form_complete,
                                                        posting_complete,
                                                        noise):
    if form_complete and posting_complete:
        return
    packet = complete_packet()
    packet["form_intel_complete"] = form_complete
    packet["posting_text_complete"] = posting_complete
    packet["brief"] = noise
    verdict = prescreen.screen_packet(packet, {}, {})
    assert verdict["verdict"] == "PARK"
    assert verdict["reasons"]


@PROPERTY_SETTINGS
@given(questions=st.one_of(st.none(), st.booleans(), st.integers(),
                           st.text(max_size=80),
                           st.tuples(st.integers(), st.text(max_size=20))))
def test_malformed_form_question_container_never_produces_clean(questions):
    packet = complete_packet()
    packet["form_intel"]["questions"] = questions
    verdict = prescreen.screen_packet(packet, {}, {})
    assert verdict["verdict"] == "PARK"
    assert any("Form-question evidence" in reason for reason in verdict["reasons"])


@PROPERTY_SETTINGS
@given(extraction_complete=st.one_of(st.none(), st.just(False), st.integers()))
def test_missing_or_nontrue_extraction_marker_never_produces_clean(extraction_complete):
    packet = complete_packet()
    packet["form_intel"]["extraction_complete"] = extraction_complete
    verdict = prescreen.screen_packet(packet, {}, {})
    assert verdict["verdict"] == "PARK"
    assert any("Form-question evidence" in reason for reason in verdict["reasons"])


@PROPERTY_SETTINGS
@given(label=st.text(max_size=80))
def test_unresolved_rendered_options_never_produce_clean(label):
    packet = complete_packet()
    packet["form_intel"]["rendered_option_fetch_needed"] = [label]
    verdict = prescreen.screen_packet(packet, {}, {})
    assert verdict["verdict"] == "PARK"
    assert any("Form-question evidence" in reason for reason in verdict["reasons"])


@PROPERTY_SETTINGS
@given(path=st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
                    min_size=1, max_size=40))
def test_form_evidence_from_another_url_never_produces_clean(path):
    packet = complete_packet()
    packet["form_intel"]["source_url"] = "https://other.example/" + path
    verdict = prescreen.screen_packet(packet, {}, {})
    assert verdict["verdict"] == "PARK"
    assert any("not bound to this application URL" in reason
               for reason in verdict["reasons"])
