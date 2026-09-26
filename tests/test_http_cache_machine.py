"""Bounded cache and real thread interleavings; all network exchanges are fakes."""
import contextlib
import multiprocessing
import threading
import time
from urllib.error import HTTPError

import pytest

import http_cache as cache
import host_cooldowns
import safe_http


URL = 'https://cache-machine.example.org/jobs'
FINAL = 'https://redirect-machine.example.org/jobs'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('KEEL_HOME', str(tmp_path))
    monkeypatch.setenv('JOB_PIPELINE_HTTP_COOLDOWN_DIR', str(tmp_path / 'legacy'))
    cache.clear()
    cache.enable()
    cache.reset_stats()
    safe_http._backoff.clear()
    host_cooldowns._backoff.clear()
    yield
    assert cache.stats()['retained_flight_slots'] == 0
    cache.clear()
    cache.enable()
    safe_http._backoff.clear()
    host_cooldowns._backoff.clear()


def spawn(function):
    result = {}
    def run():
        try:
            result['value'] = function()
        except BaseException as exc:
            result['error'] = exc
    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def join(job):
    thread, result = job
    thread.join(3)
    assert not thread.is_alive(), 'test request remained blocked'
    return result


def until(predicate):
    end = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < end, 'test synchronization deadline exceeded'
        time.sleep(.001)


def blocked_transport(monkeypatch, *, body=b'fixture', error=None):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def get(url, timeout, limit):
        calls.append((url, timeout, limit))
        entered.set()
        assert release.wait(2), 'synthetic transport was not released'
        if error is not None:
            raise error
        return body, FINAL
    monkeypatch.setattr(cache, '_network_get', get)
    return entered, release, calls


def test_many_same_key_misses_share_one_exchange(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    followers = [spawn(lambda: cache.fetch(URL)) for _ in range(8)]
    try:
        until(lambda: cache.stats()['waiting_followers'] == 8)
    finally:
        release.set()
    assert join(leader)['value'] == (b'fixture', FINAL, False)
    assert [join(job)['value'] for job in followers] == [(b'fixture', FINAL, True)] * 8
    assert len(calls) == 1
    stats = cache.stats()
    assert stats['coalesced_hits'] == stats['followers_joined'] == 8
    assert stats['network_attempts'] == stats['network_calls'] == 1
    assert stats['served_bytes'] == 8 * len(b'fixture')


def test_different_keys_execute_without_holding_global_lock(monkeypatch):
    release = threading.Event()
    entries = []
    def network(url, timeout, limit):
        entries.append(url)
        assert release.wait(2)
        return b'x', url
    monkeypatch.setattr(cache, '_network_get', network)
    left = spawn(lambda: cache.fetch(URL + '/left'))
    right = spawn(lambda: cache.fetch(URL + '/right'))
    try:
        until(lambda: len(entries) == 2)
        assert cache.stats()['inflight_requests'] == 2
    finally:
        release.set()
    assert 'value' in join(left)
    assert 'value' in join(right)


def test_follower_uses_own_short_deadline_without_cancelling_leader(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL, timeout=1))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL, timeout=.03))
    try:
        result = join(follower)
        assert isinstance(result['error'], TimeoutError)
        assert cache.stats()['inflight_requests'] == 1
        assert cache.stats()['waiting_followers'] == 0
    finally:
        release.set()
    assert 'value' in join(leader)
    assert len(calls) == 1
    assert cache.stats()['follower_timeouts'] == 1


def test_smaller_follower_limit_is_enforced_without_truncating_or_retry(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL, limit=7))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL, limit=2))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
    finally:
        release.set()
    assert join(leader)['value'][0] == b'fixture'
    assert isinstance(join(follower)['error'], safe_http.NetworkPolicyError)
    assert calls[0][2] == 7
    assert len(calls) == 1


@pytest.mark.parametrize('error', [ValueError('do not copy arbitrary failure prose'), SystemExit(7)])
def test_leader_failure_and_baseexception_wake_followers_and_free_slots(monkeypatch, error):
    entered, release, calls = blocked_transport(monkeypatch, error=error)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
    finally:
        release.set()
    assert join(leader)['error'] is error
    failure = join(follower)['error']
    assert isinstance(failure, safe_http.NetworkPolicyError)
    assert 'arbitrary failure prose' not in str(failure)
    assert len(calls) == 1
    assert cache.cache_size() == 0
    assert cache.stats()['network_errors'] == 1


def test_failed_low_limit_flight_does_not_retry_for_larger_follower(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL, limit=2))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL, limit=100))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
    finally:
        release.set()
    assert 'error' in join(leader)
    assert 'error' in join(follower)
    assert len(calls) == 1
    assert cache.cache_size() == 0
    # A separate caller can explicitly request again; failures were not cached.
    assert cache.fetch(URL, limit=100)[0] == b'fixture'
    assert len(calls) == 2


@pytest.mark.parametrize('mode', ['ttl_zero', 'disabled'])
def test_bypass_means_no_reuse_or_coalescing(monkeypatch, mode):
    if mode == 'disabled':
        cache.disable()
    entered, release, calls = blocked_transport(monkeypatch)
    options = {'ttl': 0} if mode == 'ttl_zero' else {}
    jobs = [spawn(lambda: cache.fetch(URL, **options)) for _ in range(2)]
    try:
        until(lambda: len(calls) == 2)
        assert cache.stats()['waiting_followers'] == 0
    finally:
        release.set()
    assert all(join(job)['value'][2] is False for job in jobs)
    assert cache.cache_size() == 0


def test_entry_lru_evicts_unused_key_not_recently_read_key(monkeypatch):
    monkeypatch.setattr(cache, 'MAX_ENTRIES', 2)
    monkeypatch.setattr(cache, '_network_get', lambda url, timeout, limit: (b'x', url))
    for tail in ('a', 'b', 'a', 'c'):
        cache.fetch(URL + tail)
    assert list(cache._store) == [URL + 'a', URL + 'c']
    assert cache.stats()['evictions'] == 1
    assert cache.stats()['cache_bytes'] == 2


def test_byte_budget_and_oversized_single_entry_are_bounded(monkeypatch):
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', 5)
    monkeypatch.setattr(cache, '_network_get', lambda url, timeout, limit: (b'abc', url))
    cache.fetch(URL + '/a')
    cache.fetch(URL + '/b')
    assert cache.stats()['cache_bytes'] == 3
    assert list(cache._store) == [URL + '/b']
    monkeypatch.setattr(cache, '_network_get', lambda url, timeout, limit: (b'abcdef', url))
    assert cache.fetch(URL + '/uncached')[0] == b'abcdef'
    assert cache.stats()['cache_bytes'] == 3
    assert cache.stats()['oversize_uncached'] == 1


@pytest.mark.parametrize('trigger', ['fetch', 'stats', 'cache_size'])
def test_expired_rows_are_removed_and_accounted_eagerly(monkeypatch, trigger):
    monkeypatch.setattr(cache, '_network_get', lambda url, timeout, limit: (b'expire', url))
    cache.fetch(URL)
    cache._store[URL]['expires'] = time.monotonic() - 1
    if trigger == 'fetch':
        cache.fetch(URL + '/other')
    else:
        getattr(cache, trigger)()
    assert URL not in cache._store
    assert cache.stats()['expired'] == 1
    assert cache.stats()['cache_bytes'] == (6 if trigger == 'fetch' else 0)


def test_request_and_follower_saturation_fail_without_extra_network(monkeypatch):
    monkeypatch.setattr(cache, 'MAX_INFLIGHT', 1)
    monkeypatch.setattr(cache, 'MAX_FOLLOWERS_PER_KEY', 1)
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
        with pytest.raises(safe_http.NetworkPolicyError, match='follower capacity'):
            cache.fetch(URL)
        with pytest.raises(safe_http.NetworkPolicyError, match='request capacity'):
            cache.fetch(URL + '/other')
        with pytest.raises(safe_http.NetworkPolicyError, match='request capacity'):
            cache.fetch(URL, ttl=0)
    finally:
        release.set()
    assert 'value' in join(leader)
    assert 'value' in join(follower)
    assert len(calls) == 1
    assert cache.stats()['saturation_rejections'] == 3


@pytest.mark.parametrize('invalidate', ['clear', 'disable'])
def test_invalidation_fences_inflight_publication_and_shared_delivery(monkeypatch, invalidate):
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
        getattr(cache, invalidate)()
    finally:
        release.set()
    assert 'value' in join(leader)
    assert isinstance(join(follower)['error'], safe_http.NetworkPolicyError)
    assert cache.cache_size() == 0


def test_completed_flight_retained_by_follower_still_consumes_a_slot(monkeypatch):
    monkeypatch.setattr(cache, 'MAX_INFLIGHT', 1)
    entered, release, calls = blocked_transport(monkeypatch)
    follower_check, let_follower_finish = threading.Event(), threading.Event()
    original = cache._cached_result
    def check(*args, **kwargs):
        if kwargs.get('coalesced'):
            follower_check.set()
            assert let_follower_finish.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(cache, '_cached_result', check)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
        release.set()
        assert follower_check.wait(1)
        assert 'value' in join(leader)
        assert cache.stats()['inflight_requests'] == 0
        assert cache.stats()['retained_flight_slots'] == 1
        with pytest.raises(safe_http.NetworkPolicyError, match='capacity'):
            cache.fetch(URL + '/another')
    finally:
        release.set()
        let_follower_finish.set()
    assert 'value' in join(follower)


def test_follower_rechecks_actual_redirect_hold_before_delivery(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    original = cache._cached_result
    def check(*args, **kwargs):
        if kwargs.get('coalesced'):
            host_cooldowns.record_429('redirect-machine.example.org', '60')
        return original(*args, **kwargs)
    monkeypatch.setattr(cache, '_cached_result', check)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
    finally:
        release.set()
    assert 'value' in join(leader)
    assert isinstance(join(follower)['error'], host_cooldowns.HostCoolingDown)
    assert len(calls) == 1


def test_429_exception_wakes_followers_and_does_not_retry_redirect(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def open_fake(request, timeout):
        calls.append(request.full_url)
        entered.set()
        assert release.wait(2)
        raise HTTPError(FINAL, 429, 'rate limited', {'Retry-After': '60'}, None)
    monkeypatch.setattr(cache._OPENER, 'open', open_fake)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
    finally:
        release.set()
    assert isinstance(join(leader)['error'], HTTPError)
    assert isinstance(join(follower)['error'], host_cooldowns.HostCoolingDown)
    assert len(calls) == 1
    assert cache.cache_size() == 0


def test_hold_arriving_during_network_response_blocks_publication(monkeypatch):
    def network(url, timeout, limit):
        host_cooldowns.record_429('cache-machine.example.org', '60')
        return b'no delivery', url
    monkeypatch.setattr(cache, '_network_get', network)
    with pytest.raises(host_cooldowns.HostCoolingDown):
        cache.fetch(URL)
    assert cache.cache_size() == 0


def test_late_network_result_is_not_published(monkeypatch):
    def network(url, timeout, limit):
        time.sleep(.025)
        return b'late', url
    monkeypatch.setattr(cache, '_network_get', network)
    with pytest.raises(TimeoutError):
        cache.fetch(URL, timeout=.01)
    assert cache.cache_size() == 0
    assert cache.stats()['network_errors'] == 1


def test_follower_maximum_age_is_rechecked_after_wakeup(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    original = cache._cached_result
    def check(*args, **kwargs):
        if kwargs.get('coalesced'):
            time.sleep(.02)
        return original(*args, **kwargs)
    monkeypatch.setattr(cache, '_cached_result', check)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    follower = spawn(lambda: cache.fetch(URL, ttl=.005))
    try:
        until(lambda: cache.stats()['waiting_followers'] == 1)
    finally:
        release.set()
    assert 'value' in join(leader)
    assert 'age limit' in str(join(follower)['error'])
    assert len(calls) == 1


@pytest.mark.parametrize('status', [300, 404, 500])
def test_non_success_is_never_cached(monkeypatch, status):
    monkeypatch.setattr(cache._OPENER, 'open', lambda *args, **kwargs: safe_http.Response(b'x', status, {}, URL))
    with pytest.raises(HTTPError):
        cache.fetch(URL)
    assert cache.cache_size() == 0


def test_legacy_admission_receives_remaining_caller_budget(monkeypatch):
    budgets = []
    original = host_cooldowns.check_host
    def check(host, *, timeout=5.0):
        budgets.append(timeout)
        return original(host, timeout=timeout)
    monkeypatch.setattr(host_cooldowns, 'check_host', check)
    monkeypatch.setattr(cache._OPENER, 'open', lambda *args, **kwargs: safe_http.Response(b'x', 200, {}, URL))
    assert cache.fetch(URL, timeout=.5)[0] == b'x'
    assert all(0 < b <= .5 for b in budgets)
    assert len(budgets) >= 2


@pytest.mark.parametrize('timeout', [0, -1, 61, True, float('nan'), float('inf'), '1'])
def test_legacy_timeout_extension_validates_before_admission(timeout):
    with pytest.raises(ValueError):
        host_cooldowns.check_host('cache-machine.example.org', timeout=timeout)


def test_legacy_default_stays_five_seconds_and_late_reads_fail(monkeypatch):
    calls = []
    @contextlib.contextmanager
    def locked(path, timeout):
        calls.append(timeout)
        yield
    monkeypatch.setattr(host_cooldowns, '_locked', locked)
    host_cooldowns.check_host('cache-machine.example.org')
    assert 4.9 < calls[0] <= 5
    monkeypatch.setattr(host_cooldowns, '_read_state_locked', lambda *args: (time.sleep(.02), None)[1])
    with pytest.raises(TimeoutError):
        host_cooldowns.check_host('cache-machine.example.org', timeout=.005)


def test_reset_statistics_does_not_drop_live_flight(monkeypatch):
    entered, release, calls = blocked_transport(monkeypatch)
    leader = spawn(lambda: cache.fetch(URL))
    assert entered.wait(1)
    try:
        cache.reset_stats()
        assert cache.stats()['inflight_requests'] == 1
        assert cache.stats()['network_attempts'] == 0
    finally:
        release.set()
    assert 'value' in join(leader)
    assert cache.stats()['network_calls'] == 1


def test_real_legacy_lock_contention_respects_cache_call_budget(monkeypatch):
    monkeypatch.setattr(cache, '_network_get', lambda *args, **kwargs: pytest.fail('network after expired admission'))
    path = host_cooldowns._cooldown_path('cache-machine.example.org')
    with host_cooldowns._locked(path + '.lock'):
        job = spawn(lambda: cache.fetch(URL, timeout=.03))
        result = join(job)
        assert isinstance(result['error'], TimeoutError)
    assert cache.stats()['network_attempts'] == 0


def test_delayed_initial_admission_does_not_receive_fresh_transport_budget(monkeypatch):
    monkeypatch.setattr(cache.safe_http, 'check_host_cooldown', lambda *args, **kwargs: time.sleep(.02))
    monkeypatch.setattr(cache, '_network_get', lambda *args, **kwargs: pytest.fail('network after expired admission'))
    with pytest.raises(TimeoutError):
        cache.fetch(URL, timeout=.005)
    assert cache.stats()['network_attempts'] == 0


def test_transport_redirect_admission_inherits_thread_local_remaining_budget(monkeypatch):
    seen = []
    original = host_cooldowns.check_host
    def check(host, *, timeout=5.0):
        seen.append((host, timeout))
        return original(host, timeout=timeout)
    def network(request, timeout):
        time.sleep(.01)
        cache._legacy_admission(FINAL)  # The exact before_request callback used by safe_http.
        return safe_http.Response(b'ok', 200, {}, FINAL)
    monkeypatch.setattr(host_cooldowns, 'check_host', check)
    monkeypatch.setattr(cache._OPENER, 'open', network)
    assert cache.fetch(URL, timeout=.5)[0] == b'ok'
    final_budgets = [budget for host, budget in seen if host == 'redirect-machine.example.org']
    assert final_budgets and all(0 < budget < .49 for budget in final_budgets)
    assert not hasattr(cache._request_budget, 'deadline')


def test_indefinite_hold_survives_failed_persistence(monkeypatch):
    host = 'cache-machine.example.org'
    monkeypatch.setattr(host_cooldowns, '_atomic_write_json', lambda *args: (_ for _ in ()).throw(OSError('synthetic storage failure')))
    with pytest.raises(OSError):
        host_cooldowns.record_429(host, '9' * 1000)
    assert host in host_cooldowns._backoff and host_cooldowns._backoff[host] is None
    with pytest.raises(host_cooldowns.HostCoolingDown) as caught:
        host_cooldowns.check_host(host)
    assert caught.value.until is None
    monkeypatch.setattr(cache, '_network_get', lambda *args, **kwargs: pytest.fail('429 hold bypassed'))
    with pytest.raises(host_cooldowns.HostCoolingDown):
        cache.fetch(URL)
    assert cache.stats()['network_attempts'] == 0


@pytest.mark.parametrize('existing', ['600', '9' * 1000])
@pytest.mark.parametrize('separate_worker', [False, True])
def test_shorter_record_cannot_reduce_longer_or_indefinite_hold(existing, separate_worker):
    host = 'cache-machine.example.org'
    before = host_cooldowns.record_429(host, existing)
    if separate_worker:
        host_cooldowns._backoff.clear()  # Independent worker reads the durable maximum.
    after = host_cooldowns.record_429(host, '60')
    if before is None:
        assert after is None
        assert host_cooldowns.status(host)['state'] == 'indefinite'
    else:
        assert after >= before
        assert host_cooldowns.status(host)['until'] >= before
    with pytest.raises(host_cooldowns.HostCoolingDown):
        host_cooldowns.check_host(host)


def test_failed_stronger_memory_hold_is_later_persisted_by_shorter_record(monkeypatch):
    host = 'cache-machine.example.org'
    write = host_cooldowns._atomic_write_json
    monkeypatch.setattr(host_cooldowns, '_atomic_write_json', lambda *args: (_ for _ in ()).throw(OSError('synthetic storage failure')))
    with pytest.raises(OSError):
        host_cooldowns.record_429(host, '9' * 1000)
    monkeypatch.setattr(host_cooldowns, '_atomic_write_json', write)
    assert host_cooldowns.record_429(host, '60') is None
    host_cooldowns._backoff.clear()
    with pytest.raises(host_cooldowns.HostCoolingDown) as caught:
        host_cooldowns.check_host(host)
    assert caught.value.until is None


def _record_process(host, delay, start):
    host_cooldowns._backoff.clear()
    if not start.wait(2):
        raise AssertionError('synthetic worker synchronization failed')
    host_cooldowns.record_429(host, delay)


@pytest.mark.parametrize('longer', ['600', '9' * 1000])
def test_overlapping_worker_processes_merge_durable_holds(longer):
    host = 'cache-machine.example.org'
    before = time.time()
    context = multiprocessing.get_context('fork')
    start = context.Event()
    children = [context.Process(target=_record_process, args=(host, delay, start)) for delay in (longer, '60')]
    try:
        for child in children:
            child.start()
        start.set()
        for child in children:
            child.join(3)
            assert not child.is_alive()
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(2)
    result = host_cooldowns.status(host)
    if longer == '600':
        assert result['until'] >= before + 600
    else:
        assert result['state'] == 'indefinite'
    with pytest.raises(host_cooldowns.HostCoolingDown):
        host_cooldowns.check_host(host)


def test_zero_persistence_budget_installs_hold_before_raising():
    host = 'cache-machine.example.org'
    with pytest.raises(TimeoutError):
        host_cooldowns.record_429(host, '60', timeout=0)
    with pytest.raises(host_cooldowns.HostCoolingDown):
        host_cooldowns.check_host(host)
    assert host_cooldowns.status(host) is None


def test_redirected_429_persistence_contention_uses_remaining_fetch_budget(monkeypatch):
    host = 'redirect-machine.example.org'
    entered, release = threading.Event(), threading.Event()
    seen = []
    def network(request, timeout):
        entered.set()
        assert release.wait(2)
        raise HTTPError(FINAL, 429, 'synthetic rate limit', {'Retry-After': '60'}, None)
    original = host_cooldowns.record_429
    def record(*args, **kwargs):
        seen.append(kwargs['timeout'])
        return original(*args, **kwargs)
    monkeypatch.setattr(cache._OPENER, 'open', network)
    monkeypatch.setattr(host_cooldowns, 'record_429', record)
    path = host_cooldowns._cooldown_path(host)
    with host_cooldowns._locked(path + '.lock'):
        job = spawn(lambda: cache.fetch(URL, timeout=.1))
        assert entered.wait(1)
        release.set()
        outcome = join(job)  # Must finish while persistence lock is still owned.
        assert isinstance(outcome['error'], TimeoutError)
        assert seen and 0 < seen[0] <= .1
        with pytest.raises(host_cooldowns.HostCoolingDown):
            host_cooldowns.check_host(host)
    assert cache.cache_size() == 0
    assert cache.stats()['network_attempts'] == 1


def test_429_after_exhausted_transport_budget_still_installs_hold(monkeypatch):
    def network(request, timeout):
        time.sleep(.02)
        raise HTTPError(FINAL, 429, 'synthetic rate limit', {'Retry-After': '9' * 1000}, None)
    monkeypatch.setattr(cache._OPENER, 'open', network)
    with pytest.raises(TimeoutError):
        cache.fetch(URL, timeout=.005)
    with pytest.raises(host_cooldowns.HostCoolingDown) as caught:
        host_cooldowns.check_host('redirect-machine.example.org')
    assert caught.value.until is None
    assert cache.cache_size() == 0


@pytest.mark.parametrize('timeout', [-1, 61, True, float('nan'), float('inf'), '1'])
def test_invalid_persistence_timeout_is_rejected(timeout):
    with pytest.raises(ValueError):
        host_cooldowns.record_429('cache-machine.example.org', timeout=timeout)
