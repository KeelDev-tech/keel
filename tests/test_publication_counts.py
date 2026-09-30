"""Current publications must agree with the canonical, dated recount."""
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicationCountTests(unittest.TestCase):
    def test_current_publications_match_stats(self):
        stats = json.loads((ROOT / 'docs/geo/stats.json').read_text())
        count = stats['verified_submissions_now']
        date = stats['counted_at'][:10]
        evidence = stats['evidence']
        self.assertEqual(sum(evidence.values()), count)
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(f'**{count} verified submissions**', readme)
        self.assertIn(f'ledger-verified, as of {date}', readme)
        self.assertIn(f'holds {count} verified submissions as of {date}', readme)
        split = (f"{evidence['evidenced']} evidenced /\n  {evidence['pointer']} pointer / "
                 f"{evidence['url_only']} url-only / {evidence['unevidenced']} unevidenced")
        self.assertIn(split, readme)
        for name in ('index.html', 'llms.txt', 'llms-full.txt'):
            text = (ROOT / 'site' / name).read_text()
            figures = re.findall(r'(\d+) verified submissions\. Zero lies\.', text)
            self.assertTrue(figures, name)
            self.assertEqual(set(figures), {str(count)}, name)
        summary = (f'{count} verified submissions (recounted {date}; '
                   f"{evidence['evidenced']} with quoted confirmation evidence, "
                   f"{evidence['pointer']} with pointer evidence, "
                   f"{evidence['url_only']} URL-only, {evidence['unevidenced']} unevidenced")
        for name in ('index.html', 'llms.txt', 'honesty-report.html'):
            text = (ROOT / 'site' / name).read_text()
            self.assertIn(summary, re.sub(r'\s+', ' ', text), name)
        report = (ROOT / 'site/honesty-report.html').read_text()
        self.assertIn(f"The {evidence['unevidenced']} unevidenced rows are part of the {count}", report)
        self.assertIn(f'<h1>{count}</h1>', report)
        self.assertIn(f'<strong>{count} rows</strong>', report)
        self.assertEqual([int(n) for n in re.findall(r'class="num">(\d+)<', report)],
                         [evidence[k] for k in ('evidenced', 'pointer', 'url_only', 'unevidenced')])
        index = (ROOT / 'site/index.html').read_text()
        self.assertIn(f'<div class="proof-number">{count}</div>', index)
        self.assertIn(f'What does the {count}-submission evidence figure mean?', index)
        self.assertEqual(stats['verified_submissions_at_launch'], 55)

    def test_refresh_is_idempotent(self):
        spec = importlib.util.spec_from_file_location('refresh_site', ROOT / 'geo-pipeline/refresh_site.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        count, _ = module.load_count()
        for name in module.FILES:
            text = (ROOT / 'site' / name).read_text()
            refreshed = text
            for pattern, replacement, _ in module.RULES:
                refreshed = pattern.sub(replacement(count), refreshed)
            self.assertEqual(refreshed, text, name)
