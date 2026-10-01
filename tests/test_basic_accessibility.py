"""Offline structural regression checks; rendered checks live in tools/."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from tests.test_build_dashboard import bd


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class BasicAccessibilityTests(unittest.TestCase):
    def test_dashboard_has_focusable_skip_target_and_table_semantics(self):
        with tempfile.TemporaryDirectory() as home:
            html = bd.render(bd.collect(home=home))
        elements = Elements(html).tags
        self.assertEqual([attrs for tag, attrs in elements if tag == 'main'],
                         [{'id': 'main', 'tabindex': '-1'}])
        self.assertIn(('a', {'class': 'skip', 'href': '#main'}), elements)
        self.assertEqual(sum(tag == 'caption' for tag, _ in elements), 1)
        self.assertEqual([attrs for tag, attrs in elements if tag == 'th'],
                         [{'scope': 'col'}, {'scope': 'col'}])
        self.assertIn('<tbody>', html)
        self.assertIn("colspan='2'", html)

    def test_review_skip_target_is_focusable_without_extra_tab_stop(self):
        html = (Path(__file__).resolve().parents[1] / 'keel_live/static/review.html').read_text()
        elements = Elements(html).tags
        self.assertIn(('main', {'id': 'main', 'tabindex': '-1'}), elements)
        self.assertIn(('a', {'href': '#main', 'class': 'skip'}), elements)
