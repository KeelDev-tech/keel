"""Malformed feed titles must be rejected before shared title normalization."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "engines/ats_discovery/recruitee.py"
SPEC = importlib.util.spec_from_file_location("_recruitee_title_contract", SOURCE)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class RecruiteeTitleValidationTests(unittest.TestCase):
    def offer(self, title):
        return {"slug": "synthetic-role", "title": title, "status": "published"}

    def test_nonstring_json_titles_are_sanitized_value_errors(self):
        for title in (123, 0, 1.5, True, False, [], {},
                      ["PRIVATE-SYNTHETIC-CANARY"], {"PRIVATE-SYNTHETIC-CANARY": 1}):
            with self.subTest(kind=type(title).__name__, value=title):
                raw = self.offer(title)
                before = copy.deepcopy(raw)
                with self.assertRaisesRegex(ValueError, "^invalid offer title type$"):
                    adapter.normalize(raw, "synthetic", "Example Company")
                self.assertEqual(raw, before)

    def test_string_titles_are_preserved_without_coercion(self):
        for title in ("Operations Manager", "  Operations Manager  ", "Opérations", "", " "):
            with self.subTest(title=title):
                raw = self.offer(title)
                before = copy.deepcopy(raw)
                normalized = adapter.normalize(raw, "synthetic", "Example Company")
                self.assertEqual(normalized["title"], title)
                self.assertEqual(raw, before)
                self.assertEqual(normalized["posting_url"],
                                 "https://jobs.synthetic.recruitee.com/o/synthetic-role")
                self.assertFalse(normalized["dead"])

    def test_absent_and_null_title_keep_existing_empty_contract(self):
        for raw in ({"slug": "synthetic-role"}, self.offer(None)):
            with self.subTest(raw=raw):
                self.assertEqual(adapter.normalize(raw, "synthetic", "Example Company")["title"], "")

    def test_malformed_offer_does_not_prevent_later_valid_offer(self):
        # This is the existing sweep adapter-error boundary, not live discovery.
        offers = [self.offer({"PRIVATE-SYNTHETIC-CANARY": "bad"}), self.offer("Operations Manager")]
        rows, skipped = [], []
        for raw in offers:
            try:
                rows.append(adapter.normalize(raw, "synthetic", "Example Company"))
            except ValueError as exc:
                skipped.append(str(exc))
        self.assertEqual(skipped, ["invalid offer title type"])
        self.assertEqual([row["title"] for row in rows], ["Operations Manager"])

    def test_fetch_preserves_rows_and_normalizer_rejects_invalid_title(self):
        calls = []
        def getter(url):
            calls.append(url)
            return 200, "application/json", json.dumps({"offers": [self.offer(123), self.offer("Operations Manager")]})
        rows = adapter.fetch_board("synthetic", getter)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(rows), 2)
        with self.assertRaisesRegex(ValueError, "^invalid offer title type$"):
            adapter.normalize(rows[0], "synthetic", "Example Company")
        self.assertEqual(adapter.normalize(rows[1], "synthetic", "Example Company")["title"], "Operations Manager")

    def test_existing_missing_url_and_explicit_dead_status_are_preserved(self):
        with self.assertRaisesRegex(ValueError, "cannot construct posting_url"):
            adapter.normalize({"title": "Operations Manager"}, "synthetic", "Example Company")
        raw = self.offer("Operations Manager")
        raw["status"] = "closed"
        self.assertTrue(adapter.normalize(raw, "synthetic", "Example Company")["dead"])


if __name__ == "__main__":
    unittest.main()
