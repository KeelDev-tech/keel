"""Shared admission and deadline regressions; no network or paid dependencies."""
import multiprocessing
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import safe_http as http
import safe_io

HOST = 'shared-test.example.org'
URL = 'https://' + HOST + '/'


def write_hold(home):
    os.environ['KEEL_HOME'] = home
    http._backoff.clear()
    response = http.Response(b'', 429, {'Retry-After': '180'}, URL)
    with patch.object(http, 'resolve_public', return_value=[]), patch.object(http, '_exchange', return_value=response):
        try:
            http.urlopen(URL)
        except http.HTTPError as exc:
            assert exc.code == 429
            exc.close()


def race_hold(home, start):
    os.environ['KEEL_HOME'] = home
    http._backoff.clear()
    def exchange(*args):
        (Path(home) / ('exchanged-' + str(os.getpid()))).write_text('one request')
        return http.Response(b'', 429, {'Retry-After': '120'}, URL)
    start.wait(3)
    with patch.object(http, 'resolve_public', return_value=[]), patch.object(http, '_exchange', side_effect=exchange):
        try:
            http.urlopen(URL)
        except http.HTTPError as exc:
            assert exc.code == 429
            exc.close()
        except http.NetworkPolicyError:
            pass


class SharedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'KEEL_HOME': self.temp.name})
        env.start(); self.addCleanup(env.stop)
        http._backoff.clear(); self.addCleanup(http._backoff.clear)

    def state(self, until):
        safe_io.atomic_json(http._cooldown_path(HOST), {'schema_version': 1, 'host': HOST, 'until': until})

    def test_exited_worker_hold_blocks_next_process_without_dns(self):
        process = multiprocessing.get_context('fork').Process(target=write_hold, args=(self.temp.name,))
        process.start(); process.join(5)
        if process.is_alive():
            process.terminate(); process.join(); self.fail('worker did not exit')
        self.assertEqual(process.exitcode, 0)
        with patch.object(http, 'resolve_public') as dns, self.assertRaises(http.NetworkPolicyError):
            http.urlopen(URL)
        dns.assert_not_called()

    def test_corrupt_state_cannot_be_treated_as_empty(self):
        path = http._cooldown_path(HOST); path.parent.mkdir(parents=True)
        path.write_text('{broken')
        with patch.object(http, 'resolve_public') as dns, self.assertRaises(ValueError):
            http.urlopen(URL)
        dns.assert_not_called()

    def test_simultaneous_workers_observe_first_429_before_another_request(self):
        ctx = multiprocessing.get_context('fork')
        start = ctx.Event()
        workers = [ctx.Process(target=race_hold, args=(self.temp.name, start)) for _ in range(4)]
        try:
            for worker in workers: worker.start()
            start.set()
            for worker in workers:
                worker.join(5)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(len(list(Path(self.temp.name).glob('exchanged-*'))), 1)
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join()

    def test_wrong_host_and_schema_records_deny_admission(self):
        for version, host in ((True, HOST), (2, HOST), (1, 'other.example.org')):
            safe_io.atomic_json(http._cooldown_path(HOST), {'schema_version': version, 'host': host, 'until': 0})
            with patch.object(http, 'resolve_public') as dns, self.assertRaises(http.NetworkPolicyError): http.urlopen(URL)
            dns.assert_not_called()

    def test_invalid_expiry_and_schema_fail_closed(self):
        for until in (True, -1, 'tomorrow'):
            self.state(until)
            with self.subTest(until=until), self.assertRaises(http.NetworkPolicyError):
                http.urlopen(URL)

    def test_expired_hold_allows_request(self):
        self.state(time.time() - 1)
        with patch.object(http, 'resolve_public', return_value=[]), patch.object(http, '_exchange', return_value=http.Response(b'ok', 200, {}, URL)):
            self.assertEqual(http.urlopen(URL).read(), b'ok')

    def test_unrepresentably_long_provider_hold_is_not_shortened(self):
        self.assertIsNone(http._retry_delay('9' * 500))
        self.state(None)
        with self.assertRaises(http.NetworkPolicyError): http.urlopen(URL)

    def test_retry_after_requires_integer_or_date(self):
        for value in ('nan', '1e9', '-1', '60.5'):
            self.assertEqual(http._retry_delay(value), 60)
        self.assertEqual(http._retry_delay('180'), 180)
        self.assertGreater(http._retry_delay('Wed, 01 Jan 2099 00:00:00 GMT'), 180)
        self.assertGreater(http._retry_delay('Sun Nov  6 08:49:37 2099'), 180)
        with patch.object(http, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 18, tzinfo=timezone.utc)
            self.assertGreater(http._retry_delay('Sunday, 06-Nov-75 08:49:37 GMT'), 180)
            self.assertEqual(http._retry_delay('Sunday, 06-Nov-99 08:49:37 GMT'), 60)
            self.assertGreater(http._retry_delay('Wed, 01 Jan 2099 00:00:60 GMT'), 180)

    def test_bool_timeout_is_rejected_before_io(self):
        with self.assertRaises(http.NetworkPolicyError): http.urlopen(URL, timeout=True)

    def test_resolver_wait_respects_callers_short_budget(self):
        finish = threading.Event()
        def blocked(*args, **kwargs):
            finish.wait(2)
            return []
        start = time.monotonic()
        try:
            with patch.object(http.socket, 'getaddrinfo', side_effect=blocked), self.assertRaises(TimeoutError):
                http.resolve_public(HOST, timeout=.02)
            self.assertLess(time.monotonic() - start, .5)
        finally:
            finish.set()

    def test_queued_same_host_request_times_out_without_network(self):
        entered = threading.Event(); release = threading.Event()
        def hold():
            with safe_io.file_lock(str(http._cooldown_path(HOST)) + '.lock'):
                entered.set(); release.wait(2)
        thread = threading.Thread(target=hold); thread.start(); entered.wait(1)
        try:
            with patch.object(http, 'resolve_public') as dns, self.assertRaises(TimeoutError):
                http.urlopen(URL, timeout=.03)
            dns.assert_not_called()
        finally:
            release.set(); thread.join(2)

    def test_failed_cooldown_write_retains_process_hold(self):
        response = http.Response(b'', 429, {'Retry-After': '120'}, URL)
        with patch.object(http, 'resolve_public', return_value=[]), patch.object(http, '_exchange', return_value=response), patch.object(http, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): http.urlopen(URL)
        self.assertTrue(response.closed)
        with patch.object(http, 'resolve_public') as dns, self.assertRaises(http.NetworkPolicyError): http.urlopen(URL)
        dns.assert_not_called()
