#!/usr/bin/env python3
"""Tests for mine_learnings.py retune (ARM 86, J-20260915-1422-meth-133).

(a) junk stopword themes are filtered (ARM-72 triage kill of
    P-2026-09-15 12:05-5 / -6: theme keywords were common stopwords,
    "reports" cited prior mining runs);
(b) a class already drafted in learning-proposals.md is skipped
    (dedupe across the watermark — manual arms draft directly);
(c) genuinely novel classes still draft.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "worker-charter"))
import mine_learnings as ml


def _ev(gate, n=1, note=""):
    evs = []
    for _ in range(n):
        evs.append({"event_type": "gate_blocked",
                    "details": {"gate": gate, "note": note}})
    return evs


def test_a_junk_theme_filtered():
    # The exact junk clusters from the triage: blocks about mining runs with
    # stopword-heavy themes must not draft.
    reviews = (
        "## x\n2026-09-15 10:02 — mining run (events scanned since 09:00 UTC: 368)\n"
        "constraints applied: C-01, C-02, C-03, C-11 (read-only mining; no log_event.py calls)\n"
        "## y\n2026-09-15 11:15 — ARM 75 mining run (events since 09:10 UTC: 2,255; window covers the 1053 staging-ingest burst, verify_retry runs, 4 submissions)\n"
        "constraints applied: C-01, C-02, C-03, C-11 (read-only mining)\n"
    )
    body, proposed = ml.mine([], reviews, "")
    assert proposed == 0, f"junk theme drafted: {body}"
    assert "edge-case theme" not in body
    print("a ok: junk stopword/mining-run theme filtered")


def test_a_designed_sink_gate_killed():
    # ARM-72 KILL: P-2026-09-15 12:05-1 (travel, 6x) and -4 (needs_input, 3x):
    # "gate counts alone are not a proposal".
    body, proposed = ml.mine(_ev("travel", 6) + _ev("needs_input", 5), "", "")
    assert proposed == 0, f"designed-sink gate drafted: {body}"
    print("a2 ok: designed-sink gate counts killed")


def test_a_specific_theme_still_drafts():
    reviews = (
        "## a\n2026-09-15 10:02 — lever refusal double-emission: every lever refusal "
        "fires two gate_blocked rows, inner gate and wrapper, duplicate lever double rows.\n"
        "## b\n2026-09-15 11:56 — ARM 68: lever double-emission again on calstart: duplicate "
        "gate_blocked rows for one lever refusal, inner and wrapper pair.\n"
    )
    body, proposed = ml.mine([], reviews, "")
    assert proposed == 1, f"expected 1 theme draft, got {proposed}: {body}"
    assert "edge-case theme" in body
    print("a3 ok: specific theme drafts")


def test_b_already_drafted_class_skipped():
    proposals = (
        "## P-2026-09-15 12:05-3: recurring gate `materials_demoted` (3x since last run)\n"
        "- Evidence: 3 gate events with details.gate=materials_demoted.\n"
    )
    # materials_demoted recurrence must not re-draft (ARM-72 KILL -3: duplicate).
    body, proposed = ml.mine(_ev("materials_demoted", 4), "", proposals)
    assert proposed == 0, f"already-drafted gate re-drafted: {body}"
    print("b ok: already-drafted gate class skipped")


def test_b_new_fingerprint_line_dedupes():
    proposals = "## P-x: recurring gate `ashby_spam_guard` (5x)\n- Fingerprint: gate:ashby_spam_guard\n"
    body, proposed = ml.mine(_ev("ashby_spam_guard", 5), "", proposals)
    assert proposed == 0, f"fingerprint re-drafted: {body}"
    print("b2 ok: new fingerprint lines dedupe")


def test_c_novel_class_drafts():
    body, proposed = ml.mine(_ev("lever_preflight", 4), "", "")
    assert proposed == 1, f"expected 1 novel draft, got {proposed}: {body}"
    assert "lever_preflight" in body and "Fingerprint: gate:lever_preflight" in body
    print("c ok: novel gate class drafts with fingerprint")


def test_c_novel_error_drafts():
    evs = [{"event_type": "error",
            "details": {"note": "form_intel timeout on greenhouse board API"}}] * 2
    body, proposed = ml.mine(evs, "", "")
    assert proposed == 1, f"expected 1 novel error draft, got {proposed}: {body}"
    print("c2 ok: novel error class drafts")


if __name__ == "__main__":
    test_a_junk_theme_filtered()
    test_a_designed_sink_gate_killed()
    test_a_specific_theme_still_drafts()
    test_b_already_drafted_class_skipped()
    test_b_new_fingerprint_line_dedupes()
    test_c_novel_class_drafts()
    test_c_novel_error_drafts()
    print("ALL MINE_LEARNINGS RETUNE TESTS GREEN")
