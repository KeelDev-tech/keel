"""Real read-admission path with fake DNS/TLS; no external traffic."""
import io
import socket
from urllib.error import HTTPError

import pytest

from engines import safe_http as http


URL = "https://bounded.example.org/jobs"
RECORDS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


def install_transport(monkeypatch, responses):
    seen = {"connects": [], "reads": 0, "closed": 0}

    class Socket:
        def settimeout(self, value): pass
        def connect(self, address): seen["connects"].append(address)
        def shutdown(self, how): pass
        def close(self): pass

    class Context:
        def set_alpn_protocols(self, value): pass
        def wrap_socket(self, value, server_hostname):
            assert self.minimum_version == http.ssl.TLSVersion.TLSv1_2
            return value

    class Response:
        def __init__(self, spec):
            self.status, self.headers, body = spec
            self.body = body if callable(body) else io.BytesIO(body)

        def getheader(self, name, default=None): return self.headers.get(name, default)

        def read1(self, count):
            seen["reads"] += 1
            return self.body(count) if callable(self.body) else self.body.read(count)

        def close(self): seen["closed"] += 1

    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response(responses.pop(0))
        def close(self): pass

    monkeypatch.setattr(http, "resolve_public", lambda *a, **kw: RECORDS)
    monkeypatch.setattr(http.socket, "socket", lambda *a, **kw: Socket())
    monkeypatch.setattr(http.ssl, "create_default_context", Context)
    monkeypatch.setattr(http.http.client, "HTTPSConnection", Connection)
    return seen


@pytest.fixture(autouse=True)
def private_hold_store(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEL_HOME", str(tmp_path))
    http._backoff.clear()
    yield
    http._backoff.clear()


@pytest.mark.parametrize("headers", [
    {"Content-Length": "999999999999"},
    {"Content-Length": "malformed"},
    {"Content-Encoding": "gzip"},
    {},
])
def test_rate_limit_persists_before_any_untrusted_body_read(monkeypatch, headers):
    def stalled_body(count):
        raise TimeoutError("body never completes")

    seen = install_transport(monkeypatch, [(429, {**headers, "Retry-After": "300"}, stalled_body)])
    with pytest.raises(HTTPError) as failure:
        http.urlopen(URL)
    assert failure.value.code == 429
    assert failure.value.read() == b""
    assert seen["reads"] == 0
    assert seen["closed"] == 1

    # Model restart by removing only process memory: the durable hold must remain.
    http._backoff.clear()
    with pytest.raises(http.HostRateLimited):
        http.urlopen(URL)
    assert len(seen["connects"]) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_ignores_unneeded_body_but_preserves_per_hop_admission(monkeypatch, status):
    def unread_body(count):
        pytest.fail("redirect bodies are never consumed")

    seen = install_transport(monkeypatch, [
        (status, {"Location": "/new", "Content-Encoding": "gzip", "Content-Length": "999999999"}, unread_body),
        (200, {}, b"job"),
    ])
    admitted = []
    result = http.urlopen(URL, before_request=admitted.append)
    assert result.read() == b"job"
    assert admitted == [URL, "https://bounded.example.org/new"]
    assert len(seen["connects"]) == seen["closed"] == 2


def test_redirect_cannot_bypass_destination_restrictions(monkeypatch):
    seen = install_transport(monkeypatch, [(302, {"Location": "http://127.0.0.1/private"}, b"")])
    with pytest.raises(http.NetworkPolicyError):
        http.urlopen(URL)
    assert len(seen["connects"]) == 1


def test_success_body_caps_remain_enforced(monkeypatch):
    seen = install_transport(monkeypatch, [(200, {}, b"12345")])
    with pytest.raises(http.NetworkPolicyError):
        http.urlopen(URL, max_bytes=4)
    assert seen["closed"] == 1
