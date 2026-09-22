"""Offline tests for ATS URL detection using placeholder employers."""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))
import ats


class TestDetectATS(unittest.TestCase):
    def test_workable_job_urls(self):
        urls = [
            "https://apply.workable.com/example-corp/j/ABC123DEF4/",
            "https://apply.workable.com/j/ABC123DEF4",
            "https://example-corp.workable.com/jobs/123456",
            "https://apply.workable.com/example-corp/j/ABC123DEF4/apply/",
            "https://APPLY.WORKABLE.COM/example-corp/j/ABC123DEF4/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(ats.detect_ats(url), "workable")

    def test_jazzhr_job_urls(self):
        urls = [
            "https://example-corp.jazzhr.com/jobs/123456",
            "https://example-corp.jazzhr.com/jobs/123456/apply",
            "https://EXAMPLE-CORP.JAZZHR.COM/jobs/123456",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(ats.detect_ats(url), "jazzhr")

    def test_non_ats_url(self):
        self.assertEqual(ats.detect_ats("https://example.com/jobs/123"), "unknown")

    def test_existing_platforms(self):
        urls = {
            "greenhouse": "https://boards.greenhouse.io/example/jobs/123",
            "lever": "https://jobs.lever.co/example/123",
            "ashby": "https://jobs.ashbyhq.com/example/123",
            "workday": "https://example.wd1.myworkdayjobs.com/jobs/123",
            "icims": "https://careers-example.icims.com/jobs/123",
            "smartrecruiters": "https://jobs.smartrecruiters.com/example/123",
            "jobvite": "https://jobs.jobvite.com/example/job/123",
            "breezy": "https://example.breezy.hr/p/123",
            "applytojob": "https://example.applytojob.com/apply/123",
            "workatastartup": "https://www.ycombinator.com/companies/example/jobs/123",
            "rippling": "https://ats.rippling.com/example/jobs/123",
            "tealhq": "https://www.tealhq.com/job/example-123",
        }
        for expected, url in urls.items():
            with self.subTest(ats=expected):
                self.assertEqual(ats.detect_ats(url), expected)


if __name__ == "__main__":
    unittest.main()
