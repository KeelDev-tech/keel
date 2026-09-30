"""Notification recurrence is scoped; active obligations stay authoritative."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from engines import input_tray_digest as digest
from engines import queue_io
from engines import tray_answer as actuator
from engines.qresolve_semantics import FACT_QUESTION_ALIASES, canonical_fingerprint


def lead(question="Email address", **changes):
    row = {"role_id": "fixture-1", "employer": "ExampleCo", "title": "Operations",
           "fit_score": 85, "status": "PARKED-NEEDS-INPUT",
           "status_updated": "2026-09-28 12:00 UTC", "unresolved": [question],
           "queue_notes": "Applicant question", "source_pointer": "form:field-1"}
    row.update(changes)
    return row


class QuestionIdentityTests(unittest.TestCase):
    def test_ten_reviewed_families_have_stable_alias_keys(self):
        self.assertGreaterEqual(len(FACT_QUESTION_ALIASES), 10)
        for aliases in FACT_QUESTION_ALIASES.values():
            with self.subTest(aliases=aliases):
                self.assertGreaterEqual(len(aliases), 3)
                self.assertEqual(len({digest.card_key_for(None, q) for q in aliases}), 1)

    def test_family_and_qualifiers_preserve_scope(self):
        key = digest.card_key_for(None, "Email address")
        for family, question in (("profile/work", "Email address"),
                                 (None, "Email address [work only]"),
                                 (None, "Do not provide your email address"),
                                 (None, "Email address for your reference")):
            self.assertNotEqual(digest.card_key_for(family, question), key)

    def test_unreviewed_questions_retain_legacy_keys(self):
        question = "Can you travel 25% [scheduled only]?"
        expected = hashlib.sha256(("travel/general|" + question.lower()).encode()).hexdigest()[:16]
        self.assertEqual(digest.card_key_for("travel/general", question), expected)

    def test_word_overlap_does_not_dedupe_qualified_obligation(self):
        a = "What is your email address?"
        b = "What is your email address? [work only]"
        self.assertEqual(digest.dedupe([("a", a), ("b", b)]), [("a", a), ("b", b)])

    def test_distinct_short_facts_do_not_merge(self):
        blocks = digest.genuine_blockers(lead(unresolved=["Email address", "Phone number"]))[1]
        self.assertEqual([text for _, text in blocks], ["Email address", "Phone number"])

    def test_resolution_clears_all_reviewed_aliases_and_keeps_qualifier(self):
        aliases = FACT_QUESTION_ALIASES["email"][:3]
        qualified = "Email address [work only]"
        key = digest.card_key_for(None, aliases[0])
        removed, kept = actuator.resolve_unresolved([*aliases, qualified], key, [aliases[0]])
        self.assertEqual(removed, list(aliases))
        self.assertEqual(kept, [qualified])

    def test_fragment_resolution_requires_exact_current_group(self):
        raw = ["Applicant must decide", "never pre-authorize", "Email address"]
        merged = "Applicant must decide never pre-authorize"
        key = digest.card_key_for(None, merged)
        removed, kept = actuator.resolve_unresolved(raw, key, [merged])
        self.assertEqual(removed, raw[:2])
        self.assertEqual(kept, raw[2:])
        # The same shorter wording cannot clear a longer unapproved prompt.
        removed, kept = actuator.resolve_unresolved(
            [merged + " [for another employer]"], key, [merged])
        self.assertEqual(removed, [])
        self.assertEqual(kept, [merged + " [for another employer]"])


class RecurrenceDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.qdir = self.home / "data" / "queues"
        self.qdir.mkdir(parents=True)
        self.hdir = self.home / "hidden_files"
        self.hdir.mkdir()
        patch = mock.patch.multiple(
            digest, BASE=str(self.home), QDIR=str(self.qdir), HDIR=str(self.hdir),
            BANK=str(self.home / "data" / "answer_bank.json"),
            WM=str(self.hdir / "input-tray-logged.json"),
            TRAY_JSON=str(self.hdir / "input-tray.json"),
            FAM_HIST=str(self.hdir / "input-tray-families.json"))
        patch.start()
        self.addCleanup(patch.stop)
        decoration = mock.patch.object(digest, "_decorate_cards", side_effect=lambda cards: cards)
        decoration.start()
        self.addCleanup(decoration.stop)
        environment = mock.patch.dict(os.environ, {"KEEL_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("KEEL_TRAY_MIN_FIT", None)
        previous = queue_io.get_lock_path()
        queue_io.set_lock_path(str(self.hdir / "queue.lock"))
        self.addCleanup(queue_io.set_lock_path, previous)
        Path(digest.BANK).write_text('{"answers": {}}')
        self.write_rows([lead()])

    def write_rows(self, rows):
        (self.qdir / "needs_input-queue.json").write_text(json.dumps(rows))
        (self.qdir / "standard-queue.json").write_text("[]")

    def run_digest(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(digest.main(list(args)), 0)
        return output.getvalue().strip()

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.home.rglob("*") if p.is_file()}

    def test_alias_only_rewording_stays_quiet_but_active_card_remains(self):
        original = self.run_digest("--deliver")
        self.assertEqual(json.loads(original)["fresh_cards"], 1)
        first = json.loads(Path(digest.TRAY_JSON).read_text())["cards"][0]
        for alias in FACT_QUESTION_ALIASES["email"][1:3]:
            self.write_rows([lead(alias)])
            self.assertEqual(self.run_digest("--deliver"), "TRAY-QUIET")
            active = json.loads(Path(digest.TRAY_JSON).read_text())["cards"][0]
            self.assertEqual(active["key"], first["key"])
            self.assertEqual(active["status"], "NEEDS-YOU")
            self.assertEqual(active["times_seen"], 1)
            self.assertFalse(active["fresh"])

    def test_changed_role_employer_repark_and_evidence_are_fresh(self):
        self.run_digest("--deliver")
        for changes in ({"role_id": "fixture-2"}, {"employer": "OtherCo"},
                        {"status_updated": "2026-09-29 12:00 UTC"},
                        {"source_pointer": "form:field-2"},
                        {"queue_notes": "Current applicant response required"}):
            with self.subTest(changes=changes):
                self.write_rows([lead(**changes)])
                self.assertEqual(json.loads(self.run_digest())["fresh_cards"], 1)

    def test_resolved_fingerprint_cannot_hide_new_role_or_qualifier(self):
        self.run_digest("--deliver")
        (self.hdir / "qresolve-resolved.json").write_text(json.dumps({
            canonical_fingerprint("Email address"): {"answer": "old", "target_role_ids": ["fixture-1"]}}))
        self.write_rows([lead(role_id="fixture-2"), lead("Email address [work only]", role_id="fixture-3")])
        summary = json.loads(self.run_digest("--deliver"))
        self.assertEqual(summary["fresh_cards"], 2)
        cards = json.loads(Path(digest.TRAY_JSON).read_text())["cards"]
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(card["status"] == "NEEDS-YOU" for card in cards))

    def test_preview_does_not_advance_delivery_history(self):
        before = self.snapshot()
        self.assertEqual(json.loads(self.run_digest())["fresh_cards"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads(self.run_digest())["fresh_cards"], 1)

    def test_observed_absence_makes_identical_repark_fresh_again(self):
        self.run_digest("--deliver")
        self.write_rows([])
        self.assertEqual(self.run_digest("--deliver"), "TRAY-QUIET")
        self.assertEqual(json.loads(Path(digest.WM).read_text()), [])
        self.write_rows([lead()])
        self.assertEqual(json.loads(self.run_digest("--deliver"))["fresh_cards"], 1)
        card = json.loads(Path(digest.TRAY_JSON).read_text())["cards"][0]
        self.assertEqual(card["times_seen"], 2)


if __name__ == "__main__":
    unittest.main()
