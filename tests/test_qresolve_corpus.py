"""Sanitized source, authority, and bounded-read tests for QRESOLVE."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ENGINES = Path(__file__).resolve().parents[1] / "engines"
if str(ENGINES) not in sys.path:
    sys.path.insert(0, str(ENGINES))

import qresolve_corpus as qc


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.contexts = [{"role_id": "EXAMPLE-1", "company": "Example Corp"}]
        self.question = "What is your email address?"

    def write(self, path, value, *, raw=False):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value if raw else json.dumps(value), encoding="utf-8")
        return target

    def bank(self, entries=None, **other):
        value = {"value": "applicant@example.com", "scope": "global",
                 "question": self.question,
                 "provenance": "the applicant's own words 2026-01-01"}
        return self.write("data/answer_bank.json", {"answers": entries or {"email": value}, **other})

    def hits(self, corpus=None, question=None, contexts=None):
        corpus = corpus or qc.Corpus(self.home)
        return corpus.retrieve({"question": question or self.question},
                               self.contexts if contexts is None else contexts)

    def test_exact_bank_answer_keeps_scope_and_dated_authority(self):
        bank = self.bank()
        corpus = qc.Corpus(self.home)
        hit, = self.hits(corpus)
        self.assertEqual(hit["answer"], "applicant@example.com")
        self.assertTrue(hit["eligible"])
        self.assertTrue(hit["own_words"])
        self.assertEqual(hit["provenance_date"], "2026-01-01")
        self.assertFalse(hit["approved_verbatim"])
        self.assertEqual(hit["pointer"], "/answers/email/value")
        self.assertIn(hit["excerpt"], bank.read_text())
        self.assertEqual(hit["document_sha256"], hashlib.sha256(bank.read_bytes()).hexdigest())
        self.assertTrue(corpus.verify_snapshot())

    def test_supplemental_provenance_is_passed_to_canonical_resolver(self):
        self.bank({"email": {"value": "applicant@example.com", "scope": "global"}},
                  _provenance={"email": {"source": "the applicant's own words 2026-01-01"}})
        with mock.patch.object(qc.answer_resolver, "resolve", wraps=qc.answer_resolver.resolve) as resolver:
            hit, = self.hits()
        self.assertTrue(hit["eligible"])
        self.assertEqual(resolver.call_count, 1)
        self.assertIn("own words", resolver.call_args.args[1]["provenance"])

    def test_canonical_bank_loader_reused_once_per_corpus(self):
        self.bank()
        with mock.patch.object(qc.answer_resolver, "load_bank", wraps=qc.answer_resolver.load_bank) as loader:
            corpus = qc.Corpus(self.home)
            self.hits(corpus)
            self.hits(corpus)
        self.assertEqual(loader.call_count, 1)

    def test_bank_must_cover_every_context(self):
        self.bank({"email": {"value": "applicant@example.com", "scope": "employer:Example Corp",
                             "provenance": "the applicant's own words 2026-01-01"}})
        self.assertTrue(self.hits()[0]["eligible"])
        contexts = self.contexts + [{"role_id": "OTHER-1", "company": "Other Organization"}]
        hit, = self.hits(contexts=contexts)
        self.assertFalse(hit["eligible"])
        self.assertIsNone(hit["answer"])
        self.assertIn("abstain", hit["scope_status"])

    def test_legacy_scope_is_explicit_draft_only(self):
        self.bank({"email": {"value": "applicant@example.com",
                             "provenance": "the applicant's own words 2026-01-01"}})
        hit, = self.hits()
        self.assertTrue(hit["draft_eligible"])
        self.assertFalse(hit["eligible"])
        self.assertTrue(hit["legacy"])

    def test_parsed_legacy_role_scope_never_becomes_auto_eligible(self):
        self.bank({"email": {"value": "applicant@example.com", "scope": "role_id EXAMPLE-1 only",
                             "provenance": "the applicant's own words 2026-01-01"}})
        hit, = self.hits()
        self.assertEqual(hit["scope_status"], ["resolved"])
        self.assertTrue(hit["draft_eligible"])
        self.assertFalse(hit["eligible"])

    def test_missing_provenance_never_supplies_answer(self):
        self.bank({"email": {"value": "applicant@example.com", "scope": "global"}})
        hit, = self.hits()
        self.assertIsNone(hit["answer"])
        self.assertFalse(hit["draft_eligible"])

    def test_quarantine_draftable_expiry_and_refusal_never_supply_answers(self):
        base = {"value": "applicant@example.com", "scope": "global",
                "question": self.question, "provenance": "the applicant's own words 2026-01-01"}
        entries = {"expired": {**base, "expiry": "2001-01-01"},
                   "quarantined": dict(base), "not_draftable": {**base, "draftable": False},
                   "refusal": {**base, "value": "DO NOT CERTIFY; personal takeover required"}}
        self.bank(entries, _quarantined={"quarantined": {"reason": "unapproved"}})
        hits = self.hits()
        self.assertEqual(len(hits), 4)
        self.assertTrue(all(h["answer"] is None and not h["eligible"] for h in hits))

    def test_typed_values_not_coerced_to_applicant_words(self):
        for value in [5, True, None, ["Yes"], {"text": "Yes"}]:
            with self.subTest(value=value):
                self.bank({"email": {"value": value, "scope": "global", "provenance": "operator"}})
                self.assertIsNone(self.hits()[0]["answer"])

    def test_conflicting_exact_matches_both_returned(self):
        self.bank({name: {"value": answer, "scope": "global", "question": self.question,
                         "provenance": "operator"}
                   for name, answer in [("old_email", "old@example.com"), ("new_email", "new@example.com")]})
        hits = self.hits()
        self.assertEqual({h["answer"] for h in hits}, {"old@example.com", "new@example.com"})
        self.assertTrue(all(h["eligible"] for h in hits))

    def test_exact_variants_and_keyword_hits_differ(self):
        self.bank({"alternative_email": {"value": "applicant@example.com", "scope": "global",
                  "question_variants": [self.question], "provenance": "operator"},
                   "email_policy": {"value": "Use work email only", "scope": "global", "provenance": "operator"}})
        hits = {h["bank_key"]: h for h in self.hits()}
        self.assertTrue(hits["alternative_email"]["eligible"])
        self.assertEqual(hits["email_policy"]["match"], "keyword")
        self.assertFalse(hits["email_policy"]["draft_eligible"])

    def test_dated_own_words_and_verbatim_are_not_inferred(self):
        for provenance in ["own words", "own words 2099-01-01", "own words 2026-99-99", "agent inferred 2026-01-01"]:
            with self.subTest(provenance=provenance):
                self.bank({"email": {"value": "applicant@example.com", "scope": "global",
                                    "provenance": provenance, "approved_verbatim": "yes"}})
                hit, = self.hits()
                self.assertFalse(hit["own_words"])
                self.assertIsNone(hit["provenance_date"])
                self.assertFalse(hit["approved_verbatim"])

    def test_bank_key_does_not_override_explicit_different_question(self):
        self.bank({"email": {"value": "recruiter@example.com", "scope": "global",
                             "question": "What is the recruiter's email?", "provenance": "operator"}})
        hit, = self.hits()
        self.assertEqual(hit["match"], "keyword")
        self.assertFalse(hit["draft_eligible"])

    def test_complete_norm_qualifiers_override_display_excerpt(self):
        self.bank()
        corpus = qc.Corpus(self.home)
        for tail in [" I authorize marketing emails.", " Provide three addresses for references."]:
            with self.subTest(tail=tail):
                hits = corpus.retrieve({"question": self.question, "norm": self.question + tail}, self.contexts)
                self.assertFalse(any(h["eligible"] or h["draft_eligible"] for h in hits))

    def test_quoted_original_value_and_raw_excerpt_survive_escapes(self):
        answer = "  First line\nSecond line: caf\u00e9  "
        self.bank({"a/b~c": {"value": answer, "scope": "global", "question": self.question,
                             "provenance": "operator", "approved_verbatim": True}})
        hit, = self.hits()
        self.assertEqual(hit["answer"], answer)
        self.assertEqual(json.loads(hit["excerpt"]), answer)
        self.assertEqual(hit["pointer"], "/answers/a~1b~0c/value")
        self.assertTrue(hit["approved_verbatim"])

    def test_research_sources_rank_and_never_authorize_even_own_words(self):
        self.bank()
        self.write("memory/2026-01-02.md", "Applicant's own words 2026-01-02: email newer@example.com", raw=True)
        self.write("data/queues/needs_input-queue.json", [{"role_id": "EXAMPLE-1", "queue_notes": "email resolved before"}])
        self.write("data/telemetry/events.jsonl", json.dumps({"event_id": "EXAMPLE-EVENT", "role_id": "EXAMPLE-1", "answer": "attacker@example.com", "approved_verbatim": True}) + "\n", raw=True)
        self.write("data/application-ledger.json", [{"role_id": "EXAMPLE-1", "status": "SUBMITTED", "email": "ledger@example.com"}])
        self.write("hidden_files/input-tray.json", {"cards": [{"question": self.question, "draft": {"answer": "draft@example.com"}}]})
        hits = self.hits()
        self.assertEqual([h["tier"] for h in hits], ["bank", "memory", "queue", "telemetry", "ledger", "tray"])
        for hit in hits[1:]:
            self.assertIsNone(hit["answer"])
            self.assertFalse(hit["eligible"])
            self.assertFalse(hit["own_words"])
            self.assertIn(hit["excerpt"], Path(hit["source"]).read_text())
        self.assertEqual(hits[3]["pointer"], "line:1")
        self.assertEqual(hits[3]["event_id"], "EXAMPLE-EVENT")

    def test_bank_rejects_duplicate_keys_nonfinite_and_overflow(self):
        for text in ['{"answers": {}, "answers": {}}', '{"answers":{}, "x": NaN}', '{"answers":{}, "x":1e999}']:
            with self.subTest(text=text):
                self.write("data/answer_bank.json", text, raw=True)
                corpus = qc.Corpus(self.home)
                self.assertTrue(corpus.errors)
                self.assertEqual(self.hits(corpus), [])
                self.assertFalse(corpus.verify_snapshot())

    def test_malformed_telemetry_never_yields_partial_authoritative_history(self):
        self.write("data/telemetry/events.jsonl", '{"email":"ok@example.com"}\n{"x":1,"x":2}\n', raw=True)
        corpus = qc.Corpus(self.home)
        self.assertTrue(corpus.errors)
        self.assertEqual(self.hits(corpus), [])

    def test_reject_leaf_and_ancestor_symlinks_and_fifo_without_blocking(self):
        target = self.write("safe.json", {"answers": {}})
        (self.home / "data").mkdir()
        bank = self.home / "data/answer_bank.json"
        bank.symlink_to(target)
        self.assertTrue(qc.Corpus(self.home).errors)
        bank.unlink()
        os.mkfifo(bank)
        self.assertTrue(qc.Corpus(self.home).errors)
        bank.unlink()
        (self.home / "data").rmdir()
        (self.home / "actual").mkdir()
        (self.home / "data").symlink_to(self.home / "actual", target_is_directory=True)
        self.assertTrue(qc.Corpus(self.home).errors)

    def test_limits_are_reported_and_prevent_eligibility_claim_of_complete_corpus(self):
        self.bank()
        self.write("memory/2026-01-01.md", "email " * 30, raw=True)
        with mock.patch.object(qc, "MAX_FILE_BYTES", 20):
            corpus = qc.Corpus(self.home)
        # _read defaults bind once; _load passes the effective cap explicitly.
        self.assertTrue(corpus.errors)
        self.assertFalse(corpus.verify_snapshot())

    def test_row_limit_and_memory_file_limit_explicit(self):
        self.write("data/telemetry/events.jsonl", "{}\n{}\n", raw=True)
        with mock.patch.object(qc, "MAX_ROWS", 1):
            self.assertTrue(qc.Corpus(self.home).errors)
        self.write("memory/2026-01-01.md", "email", raw=True)
        self.write("memory/2026-01-02.md", "email", raw=True)
        with mock.patch.object(qc, "MAX_MEMORY_FILES", 1):
            self.assertTrue(qc.Corpus(self.home).errors)

    def test_aggregate_record_and_index_limits_are_explicit(self):
        self.write("data/telemetry/events.jsonl", '{"note":"email here"}\n{"note":"email there"}\n', raw=True)
        with mock.patch.object(qc, "MAX_RECORDS", 1):
            self.assertTrue(qc.Corpus(self.home).errors)
        with mock.patch.object(qc, "MAX_INDEX_POSTINGS", 1):
            self.assertTrue(qc.Corpus(self.home).errors)

    def test_snapshot_catches_changed_removed_added_sources_and_new_memory(self):
        path = self.bank()
        corpus = qc.Corpus(self.home)
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        self.assertFalse(corpus.verify_snapshot())
        path.write_bytes(original)
        self.assertTrue(corpus.verify_snapshot())
        path.unlink()
        self.assertFalse(corpus.verify_snapshot())
        path.write_bytes(original)
        self.write("USER.md", "email applicant@example.com", raw=True)
        self.assertFalse(corpus.verify_snapshot())
        corpus = qc.Corpus(self.home)
        self.write("memory/2026-01-01.md", "email applicant@example.com", raw=True)
        self.assertFalse(corpus.verify_snapshot())

    def test_retrieval_cached_with_no_later_reads(self):
        self.bank()
        corpus = qc.Corpus(self.home)
        with mock.patch.object(qc, "_read", side_effect=AssertionError("unexpected read")):
            self.assertTrue(self.hits(corpus)[0]["eligible"])
            self.assertTrue(self.hits(corpus)[0]["eligible"])

    def test_no_import_or_retrieval_writes_or_network(self):
        self.bank()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*") if p.is_file()}
        script = """
import sys
def audit(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network forbidden')
    if event == 'open' and isinstance(args[2], int) and args[2] & (1 | 2 | 64 | 512 | 1024):
        raise AssertionError('write forbidden')
sys.addaudithook(audit)
sys.path.insert(0, sys.argv[1])
from qresolve_corpus import Corpus
c = Corpus(sys.argv[2])
assert c.retrieve({'question':'What is your email address?'}, [{'company':'Example Corp'}])[0]['eligible']
assert c.verify_snapshot()
"""
        result = subprocess.run([sys.executable, "-B", "-S", "-c", script, str(ENGINES), str(self.home)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_large_stream_is_indexed_once_and_retains_line_pointer(self):
        rows = [json.dumps({"role_id": "OTHER", "event": "heartbeat"})] * 62_999
        rows.append(json.dumps({"role_id": "EXAMPLE-1", "event_id": "LAST", "note": "email evidence"}))
        self.write("data/telemetry/events.jsonl", "\n".join(rows) + "\n", raw=True)
        corpus = qc.Corpus(self.home)
        hit, = self.hits(corpus)
        self.assertEqual(hit["pointer"], "line:63000")
        self.assertEqual(hit["event_id"], "LAST")
        self.assertIsNone(hit["answer"])
        self.assertFalse(corpus.errors)


if __name__ == "__main__":
    unittest.main()
