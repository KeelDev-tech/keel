"""A word-prefix similarity must not widen employer-scoped answer authority."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import answer_resolver as resolver
import answer_reuse
import packet_contract


class EmployerPrefixScopeTests(unittest.TestCase):
    def resolve(self, scoped, requesting, **extra):
        entry = {"value": "Yes", "scope": "employer:" + scoped, **extra}
        return resolver.resolve("background_check_consent", entry,
                                employer=requesting, role_context={"company": requesting,
                                "role_id": "synthetic-role"}, registry=set())

    def test_distinct_word_prefix_employers_abstain_in_both_directions(self):
        for base in ("Atlas", "Cedar Works", "North Star"):
            for suffix in ("Robotics", "Staffing", "Research"):
                for scoped, requesting in ((base, base + " " + suffix),
                                          (base + " " + suffix, base)):
                    with self.subTest(scoped=scoped, requesting=requesting):
                        result = self.resolve(scoped, requesting)
                        self.assertEqual(result.status, resolver.STATUS_ABSTAIN)
                        self.assertEqual(result.value, "")

    def test_existing_exact_normalized_and_legal_suffix_matches_remain(self):
        for base in ("Atlas", "Cedar Works", "North Star"):
            for equivalent in (base, base.upper(), "  " + base + "  ", base + " Inc.",
                               base + " LLC", base + " Ltd.", base + " Corporation",
                               base.replace(" ", "-")):
                with self.subTest(base=base, equivalent=equivalent):
                    self.assertEqual(self.resolve(base, equivalent).status, resolver.STATUS_RESOLVED)
                    self.assertEqual(self.resolve(equivalent, base).status, resolver.STATUS_RESOLVED)

    def test_no_matching_by_substring_or_missing_name(self):
        for name in ("AtlasX", "At", "", "X", None):
            with self.subTest(name=name):
                self.assertEqual(self.resolve("Atlas", name).status, resolver.STATUS_ABSTAIN)

    def test_packet_receipt_does_not_broaden_embedded_employer_scope(self):
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        value = {"value": "Yes", "scope": "employer:Atlas"}
        bank = {"answers": {"background_check_consent": value}, "_provenance": {
            "background_check_consent": packet_contract.answer_receipt(
                value, "Synthetic applicant assertion for Atlas only", role_id="synthetic-role", now=now)}}
        before = copy.deepcopy(bank)
        for company in ("Atlas Robotics", "Atlas Staffing"):
            with self.subTest(company=company):
                approved, problems = packet_contract.confirmed_answers(
                    bank, role_id="synthetic-role", employer=company,
                    role_context={"company": company}, now=now)
                self.assertEqual(approved, {})
                self.assertEqual(problems, ["background_check_consent: scoped resolver abstained"])
        approved, problems = packet_contract.confirmed_answers(
            bank, role_id="synthetic-role", employer="Atlas", role_context={"company": "Atlas"}, now=now)
        self.assertEqual(approved, {"background_check_consent": "Yes"})
        self.assertEqual(problems, [])
        self.assertEqual(bank, before)

    def test_reuse_session_abstains_without_extra_registry_loads(self):
        with patch.object(answer_reuse, "_known_employers", return_value=set()) as registry:
            session = answer_reuse.ReuseSession(
                {"background_check_consent": {"value": "Yes", "scope": "employer:Atlas"}},
                employer="Atlas Robotics", role_context={"company": "Atlas Robotics"})
            for _ in range(4):
                result = session.reuse("background_check_consent")
                self.assertEqual(result.status, resolver.STATUS_ABSTAIN)
                self.assertEqual(result.value, "")
            self.assertEqual(registry.call_count, 1)
            self.assertEqual(session.cost_report()["abstained"], 4)
            self.assertTrue(all(e.status == resolver.STATUS_ABSTAIN for e in session.evidence()))

    def test_expired_refused_and_ambiguous_authority_stays_closed(self):
        for extra in ({"expires_at": "2000-01-01T00:00:00Z"},
                      {"value": "DO NOT CERTIFY - personal takeover"}, {"scope": "ambiguous"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.resolve("Atlas", "Atlas", **extra).status, resolver.STATUS_ABSTAIN)

    def test_explicit_global_scope_unchanged(self):
        result = self.resolve("Atlas", "Atlas Robotics", scope="global")
        self.assertEqual(result.status, resolver.STATUS_RESOLVED)
        self.assertEqual(result.value, "Yes")


if __name__ == "__main__":
    unittest.main()
