"""Synthetic final-PDF regressions; parsing never grants applicant authority."""
import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfbase.pdfmetrics import getAscentDescent

from engines.resume_tailor import build


def synthetic_profile():
    return {
        "synthetic": True,
        "semantic_approval": False,
        "name": "Synthetic Candidate Zeta",
        "contact": {
            "phone": "+1 202-555-0142",
            "email": "zeta@example.com",
            "location": "Sample City, ZZ",
            "linkedin": "https://example.com/zeta",
        },
        "experience": [
            {
                "org": "Synthetic Harbor Labs",
                "title": "Platform Analyst",
                "dates": "2021 - 2024",
                "bullets": ["Reviewed intake notes & prepared weekly summaries."],
            },
            {
                "org": "Synthetic Orchard Works",
                "title": "Support Specialist",
                "dates": "2017 - 2020",
                "bullets": ["Prepared service reports from synthetic records."],
            },
        ],
        "education": "Synthetic Example College — Sample Studies, 2013 - 2017",
    }


class ResumePdfTests(unittest.TestCase):
    def render_pdf(self, profile):
        role = {"headline": "Synthetic Operations Analyst", "hard_requirements": []}
        before = copy.deepcopy((profile, role))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-resume.pdf"
            with redirect_stdout(io.StringIO()):
                build(profile, role, str(path))
            pdf = path.read_bytes()
        self.assertEqual((profile, role), before)
        self.assertIs(profile["semantic_approval"], False)
        return pdf

    def render_text(self, profile):
        reader = PdfReader(io.BytesIO(self.render_pdf(profile)), strict=True)
        self.assertEqual(len(reader.pages), 1)
        return reader.pages[0].extract_text()

    def assert_ordered(self, text, values):
        # Whitespace may change at line wraps, but values and order must survive.
        text = " ".join(text.split())
        cursor = 0
        for value in values:
            value = " ".join(value.split())
            position = text.find(value, cursor)
            self.assertGreaterEqual(position, cursor, value)
            cursor = position + len(value)

    def assert_no_entity_leak(self, text):
        for entity in ("&nbsp;", "&amp;", "&lt;", "&gt;"):
            self.assertNotIn(entity, text)

    def test_final_pdf_preserves_contacts_dates_and_reading_order(self):
        profile = synthetic_profile()
        text = self.render_text(profile)
        expected = [profile["name"], *profile["contact"].values(),
                    "Synthetic Operations Analyst", "Experience"]
        for job in profile["experience"]:
            expected.extend([job["org"], job["title"], job["dates"], *job["bullets"]])
        expected.extend(["Education", profile["education"]])
        self.assert_ordered(text, expected)
        for value in (*profile["contact"].values(), "2021 - 2024", "2017 - 2020"):
            self.assertIn(value, text)
        self.assertEqual(text.count("|"), 3)
        self.assert_no_entity_leak(text)

    def test_final_pdf_escapes_contact_values_as_text(self):
        profile = synthetic_profile()
        profile["contact"].update({
            "email": "zeta&alias@example.com",
            "location": "Sample <b>North</b> & West",
            "linkedin": "https://example.com/zeta?a=1&b=2",
        })
        text = self.render_text(profile)
        self.assert_ordered(text, list(profile["contact"].values()))
        self.assertEqual(text.count("|"), 3)
        self.assert_no_entity_leak(text)

    def test_final_pdf_omits_empty_contact_separators(self):
        fields = ("phone", "email", "location", "linkedin")
        for contact in ({}, dict.fromkeys(fields), dict.fromkeys(fields, "")):
            with self.subTest(contact=contact):
                profile = synthetic_profile()
                profile["contact"] = contact
                text = self.render_text(profile)
                self.assertNotIn("|", text)
                self.assert_ordered(text, [profile["name"], "Experience",
                                          "2021 - 2024", "2017 - 2020", "Education"])
                self.assert_no_entity_leak(text)

    def test_final_pdf_joins_only_populated_contacts(self):
        profile = synthetic_profile()
        profile["contact"] = {"phone": None, "email": "zeta@example.com",
                              "location": "", "linkedin": "https://example.com/zeta"}
        text = self.render_text(profile)
        self.assertIn("zeta@example.com | https://example.com/zeta", text)
        self.assertEqual(text.count("|"), 1)
        self.assert_no_entity_leak(text)

    def test_final_pdf_preserves_wrapped_name_and_contact_values(self):
        profile = synthetic_profile()
        profile["name"] = (
            "Synthetic Candidate Zeta Demonstration Applicant With A Deliberately Wrapped Name"
        )
        profile["contact"].update({
            "email": "synthetic.candidate.roundtrip@example.com",
            "location": "Synthetic District North, Sample City, ZZ",
            "linkedin": "https://example.com/in/synthetic-candidate-roundtrip",
        })
        text = self.render_text(profile)
        self.assert_ordered(text, [profile["name"], *profile["contact"].values(),
                                  "Synthetic Operations Analyst", "Experience"])
        contact_block = text[text.index(profile["contact"]["phone"]):
                             text.index("Synthetic Operations Analyst")]
        self.assertGreaterEqual(len(contact_block.strip().splitlines()), 2)
        self.assertEqual(text.count("|"), 3)
        self.assert_no_entity_leak(text)

    def test_final_pdf_separates_single_line_name_from_first_contact_line(self):
        profile = synthetic_profile()
        page = PdfReader(io.BytesIO(self.render_pdf(profile)), strict=True).pages[0]
        spans = {}

        def capture(text, cm, tm, font, size):
            value = text.strip()
            label = ("name" if value == profile["name"] else
                     "contact" if value.startswith(profile["contact"]["phone"]) else None)
            if label is not None:
                self.assertEqual(cm[:4], [1, 0, 0, 1])
                self.assertEqual(tm[:4], [1, 0, 0, 1])
                spans[label] = (cm[5] + tm[5], str(font["/BaseFont"])[1:], size)

        page.extract_text(visitor_text=capture)
        self.assertEqual(set(spans), {"name", "contact"})
        name_y, name_font, name_size = spans["name"]
        contact_y, contact_font, contact_size = spans["contact"]
        self.assertEqual((name_font, contact_font), ("Helvetica-Bold", "Helvetica"))
        _, name_descent = getAscentDescent(name_font, name_size)
        contact_ascent, _ = getAscentDescent(contact_font, contact_size)
        # Nominal font clearance for this ordinary header, not painted glyph bounds.
        # pypdf's visitor coordinates for later wrapped lines are not used here.
        clearance = (name_y + name_descent) - (contact_y + contact_ascent)
        self.assertGreaterEqual(clearance, 3.0)


if __name__ == "__main__":
    unittest.main()
