"""Tests for blocker_inbox.py (prototype, G-3/B-24).

Covers the B-24 acceptance scenarios:
 1. one card per question: dedupe across leads with gate + recurrence
 2. machine-suggested answers can never enter the bank, never fill a packet
 3. user-confirmed answers bank with provenance + source message trace
 4. retro-resolution unblocks exactly the blocked set (no more, no fewer)
 5. rejected suggestions leave the blocker open
 6. recurrence view surfaces repeating questions first
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blocker_inbox import (  # noqa: E402
    Blocker, ProposedAnswer, BlockerInbox,
    HIS_WORDS, MACHINE_SUGGESTED, USER_EDITED,
    question_hash,
)


def make_inbox():
    inbox = BlockerInbox()
    # 5 blockers, 3 leads, 2 distinct questions
    inbox.add_blocker(Blocker("b1", "lead-a", "Why do you want to work here?", "needs_input:essay"))
    inbox.add_blocker(Blocker("b2", "lead-b", "Why do you want to work here?", "needs_input:essay"))
    inbox.add_blocker(Blocker("b3", "lead-c", "Why do you  want to work here?", "needs_input:essay"))  # spacing variant
    inbox.add_blocker(Blocker("b4", "lead-a", "Describe your Salesforce experience.", "needs_input:fact"))
    inbox.add_blocker(Blocker("b5", "lead-b", "Describe your Salesforce experience.", "needs_input:fact"))
    return inbox


def test_one_card_per_question_with_recurrence():
    inbox = make_inbox()
    cards = inbox.cards()
    assert len(cards) == 2, cards  # spacing variant dedupes to the same question
    by_rec = {c["recurrence"]: c for c in cards}
    assert by_rec[3]["gate"] == "needs_input:essay"
    assert by_rec[3]["lead_ids"] == ["lead-a", "lead-b", "lead-c"]
    assert by_rec[2]["lead_ids"] == ["lead-a", "lead-b"]
    print("ok test_one_card_per_question_with_recurrence")


def test_machine_suggestion_never_banked_never_fills_packet():
    inbox = make_inbox()
    qh = question_hash("Why do you want to work here?")
    sugg = ProposedAnswer(qh, "I love your mission...", MACHINE_SUGGESTED)
    try:
        inbox.bank.bank(sugg, "2026-09-19T03:10:00")
        raise AssertionError("bank accepted a machine suggestion")
    except ValueError:
        pass
    assert inbox.bank.packet_fill(qh) is None  # resolver abstains
    print("ok test_machine_suggestion_never_banked_never_fills_packet")


def test_user_confirmed_answer_banked_with_trace():
    inbox = make_inbox()
    qh = question_hash("Why do you want to work here?")
    result = inbox.answer_question(
        ProposedAnswer(qh, "Trent's own words here.", HIS_WORDS, source_message_id="msg-4821"),
        "2026-09-19T03:11:00",
    )
    assert result["banked_provenance"] == HIS_WORDS
    assert result["source_message_id"] == "msg-4821"  # traces to the exact message
    assert inbox.bank.packet_fill(qh) == "Trent's own words here."
    print("ok test_user_confirmed_answer_banked_with_trace")


def test_retro_resolution_unblocks_exactly_the_blocked_set():
    inbox = make_inbox()
    qh = question_hash("Why do you want to work here?")
    result = inbox.answer_question(
        ProposedAnswer(qh, "Trent's own words.", HIS_WORDS, source_message_id="msg-4822"),
        "2026-09-19T03:12:00",
    )
    assert result["blockers_before"] == 5
    assert result["unblocked_leads"] == ["lead-a", "lead-b", "lead-c"]  # exactly the essay-blocked set
    assert result["unblocked_count"] == 3
    assert result["blockers_after"] == 2  # the 2 Salesforce blockers remain
    remaining = inbox.cards()
    assert len(remaining) == 1 and remaining[0]["recurrence"] == 2
    print("ok test_retro_resolution_unblocks_exactly_the_blocked_set")


def test_user_edited_suggestion_banked_as_edited():
    inbox = make_inbox()
    qh = question_hash("Describe your Salesforce experience.")
    result = inbox.answer_question(
        ProposedAnswer(qh, "Edited by Trent: hands-on user, no admin.", USER_EDITED,
                       source_message_id="msg-4823"),
        "2026-09-19T03:13:00",
    )
    assert result["banked_provenance"] == USER_EDITED
    assert inbox.bank.packet_fill(qh) == "Edited by Trent: hands-on user, no admin."
    print("ok test_user_edited_suggestion_banked_as_edited")


def test_rejected_suggestion_leaves_blocker_open():
    inbox = make_inbox()
    qh = question_hash("Why do you want to work here?")
    inbox.reject_suggestion(qh, "I love your mission...")
    assert len(inbox.blockers) == 5  # nothing closed
    assert inbox.rejected[qh] == "I love your mission..."
    assert inbox.bank.packet_fill(qh) is None  # still abstains
    print("ok test_rejected_suggestion_leaves_blocker_open")


def test_recurrence_view_surfaces_patterns_first():
    inbox = make_inbox()
    view = inbox.recurrence_view()
    assert view[0]["recurrence"] >= view[1]["recurrence"]
    assert view[0]["recurrence"] == 3
    print("ok test_recurrence_view_surfaces_patterns_first")


def test_bank_requires_source_message():
    inbox = make_inbox()
    qh = question_hash("Why do you want to work here?")
    try:
        inbox.bank.bank(ProposedAnswer(qh, "words", HIS_WORDS, source_message_id=None),
                        "2026-09-19T03:14:00")
        raise AssertionError("bank accepted an answer with no source message")
    except ValueError:
        pass
    print("ok test_bank_requires_source_message")


if __name__ == "__main__":
    test_one_card_per_question_with_recurrence()
    test_machine_suggestion_never_banked_never_fills_packet()
    test_user_confirmed_answer_banked_with_trace()
    test_retro_resolution_unblocks_exactly_the_blocked_set()
    test_user_edited_suggestion_banked_as_edited()
    test_rejected_suggestion_leaves_blocker_open()
    test_recurrence_view_surfaces_patterns_first()
    test_bank_requires_source_message()
    print("ALL 8 TESTS PASSED")
