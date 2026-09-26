"""Actual local pipeline benchmark contract; standard library only."""
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch
from keel_next.benchmark import run_benchmark


class PipelineBenchmarkTests(unittest.TestCase):
    def test_namespace_only_keel_module_cannot_shadow_cli(self):
        namespace=types.ModuleType('keel')
        namespace.__path__=[]
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules,{'keel':namespace}):
            report=run_benchmark(Path(temp)/'new',boards=1,jobs_per_board=2,max_new=1)
            self.assertEqual(report['status'],'BENCHMARK_PASSED')
            self.assertIs(sys.modules['keel'],namespace)

    def test_real_pipeline_benchmark(self):
        with tempfile.TemporaryDirectory(prefix='keel-benchmark-test-') as temp:
            home = Path(temp)/'new'
            result = run_benchmark(home, boards=2, jobs_per_board=4, max_new=3)
            self.assertEqual(result['status'], 'BENCHMARK_PASSED', result['checks'])
            self.assertTrue(all(result['checks'].values()))
            self.assertEqual(result['counts']['first_added'], 3)
            self.assertEqual(result['counts']['remaining_added'], 5)
            self.assertEqual(result['counts']['events'], 8)
            self.assertEqual(result['network_calls'], 0)
            self.assertFalse(result['production_speedup_proven'])
            self.assertTrue((home/'benchmark-report.json').is_file())
            self.assertGreater(result['memory']['peak_bytes'], 0)

    def test_refuses_existing_home(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileExistsError): run_benchmark(temp)

    def test_validates_work_before_creating_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'new'
            for options in ({'boards':True},{'boards':0},{'jobs_per_board':201},
                            {'boards':10,'jobs_per_board':200},{'max_new':0}):
                with self.subTest(options=options):
                    with self.assertRaises(ValueError): run_benchmark(home, **options)
                    self.assertFalse(home.exists())

    def test_small_complete_first_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            report = run_benchmark(Path(temp)/'new', boards=1, jobs_per_board=1, max_new=2)
            self.assertEqual(report['status'], 'BENCHMARK_PASSED', report['checks'])
            self.assertEqual(report['counts']['remaining_added'], 0)


if __name__ == '__main__': unittest.main()
