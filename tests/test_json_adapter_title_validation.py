"""JSON discovery title contracts: reject types, never invent posting titles."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / "engines/ats_discovery"
FIELDS = {"bamboohr": "jobOpeningName", "breezy": "name", "pinpoint": "title",
          "rippling": "name", "smartrecruiters": "name", "workable": "title"}


def load(name):
    spec = importlib.util.spec_from_file_location("_title_contract_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADAPTERS = {name: load(name) for name in FIELDS}
NORMALIZE = load("normalize")


def offer(name, title):
    return {"id": "synthetic-role", "uuid": "synthetic-role",
            "url": "https://example.com/jobs/synthetic-role", FIELDS[name]: title}


class JsonAdapterTitleValidationTests(unittest.TestCase):
    def test_nonstring_titles_raise_sanitized_value_error_without_mutation(self):
        for name, adapter in ADAPTERS.items():
            for title in (123, 0, 1.5, True, False, [], {},
                          ["SYNTHETIC-CANARY"], {"SYNTHETIC-CANARY": "bad"}):
                with self.subTest(platform=name, kind=type(title).__name__, value=title):
                    raw = offer(name, title)
                    before = copy.deepcopy(raw)
                    with self.assertRaisesRegex(ValueError, "^invalid posting title type$"):
                        adapter.normalize(raw, "synthetic", "Example Company")
                    self.assertEqual(raw, before)

    def test_valid_string_titles_remain_exact(self):
        for name, adapter in ADAPTERS.items():
            for title in ("Operations Manager", "  Sales  ", "Opérations", "", " "):
                with self.subTest(platform=name, title=title):
                    raw = offer(name, title)
                    before = copy.deepcopy(raw)
                    result = adapter.normalize(raw, "synthetic", "Example Company")
                    self.assertEqual(result["title"], title)
                    self.assertEqual(raw, before)

    def test_missing_and_null_titles_preserve_empty_contract(self):
        for name, adapter in ADAPTERS.items():
            raw = offer(name, None)
            for missing in (False, True):
                with self.subTest(platform=name, missing=missing):
                    if missing:
                        del raw[FIELDS[name]]
                    self.assertEqual(adapter.normalize(raw, "synthetic", "Example Company")["title"], "")

    def test_adapter_error_boundary_preserves_later_valid_title(self):
        # Shared title normalization deliberately remains outside the adapter catch,
        # matching sweep_platform. Malformed input must fail inside the adapter.
        for name, adapter in ADAPTERS.items():
            for bad in (123, True, {"synthetic": "bad"}, ["bad"]):
                with self.subTest(platform=name, kind=type(bad).__name__):
                    titles, skips = [], []
                    for raw in (offer(name, bad), offer(name, "Operations Manager")):
                        try:
                            result = adapter.normalize(raw, "synthetic", "Example Company")
                        except ValueError as exc:
                            skips.append(str(exc))
                            continue
                        titles.append(NORMALIZE.norm_title(result["title"]))
                    self.assertEqual(titles, [NORMALIZE.norm_title("Operations Manager")])
                    self.assertEqual(skips, ["invalid posting title type"])

    def test_synthetic_fetch_preserves_title_types_for_adapter_validation(self):
        for name, adapter in ADAPTERS.items():
            for enrich in (False, True):
                with self.subTest(platform=name, enrichment=enrich):
                    rows = [offer(name, 123), offer(name, "Operations Manager")]
                    payload = {"bamboohr": {"result": rows}, "breezy": rows,
                               "pinpoint": {"data": rows}, "rippling": {"jobs": rows},
                               "smartrecruiters": {"content": rows, "totalFound": 2},
                               "workable": {"jobs": rows}}[name]
                    calls = []
                    def getter(url):
                        calls.append(url)
                        if len(calls) == 1:
                            return 200, "application/json", json.dumps(payload)
                        if name == "breezy":
                            return 200, "text/html", "<p>Synthetic posting</p>"
                        return 200, "application/json", "{}"
                    with patch.object(adapter, "ENRICH_DETAILS", enrich):
                        fetched = adapter.fetch_board("synthetic", getter)
                    self.assertEqual(len(fetched), 2)
                    with self.assertRaisesRegex(ValueError, "^invalid posting title type$"):
                        adapter.normalize(fetched[0], "synthetic", "Example Company")
                    self.assertEqual(adapter.normalize(fetched[1], "synthetic", "Example Company")["title"],
                                     "Operations Manager")


if __name__ == "__main__":
    unittest.main()
