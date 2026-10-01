"""Existing handler response policy, exercised without opening a socket.

Does not qualify browser enforcement or transport-level parser/overload errors
that do not pass through Handler.send_payload.
"""
from http.client import parse_headers
from io import BytesIO
import json
import socket
from types import SimpleNamespace

import pytest

from keel_workbench import server
from keel_workbench.demo import make_demo, NOW
from keel_workbench.service import Workbench, Conflict


class MemoryConnection:
    def __init__(self, request):
        self.request = request
        self.response = bytearray()

    def makefile(self, mode, *args):
        assert mode == 'rb'
        return BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)


@pytest.fixture
def respond(monkeypatch):
    def no_socket(*args, **kwargs):
        pytest.fail('in-memory handler test must not open a socket')
    monkeypatch.setattr(socket, 'socket', no_socket)
    host = SimpleNamespace(authority='127.0.0.1:8765', token='synthetic-token-' * 3,
        app=Workbench(make_demo(), workspace_id='keel-demo', synthetic=True, host_clock=lambda: NOW))

    def request(path='/', *, authenticated=True, headers=None):
        fields = {'Host': host.authority, **(headers or {})}
        if authenticated:
            fields['Authorization'] = 'Bearer ' + host.token
        wire = 'GET ' + path + ' HTTP/1.0\r\n' + ''.join(k + ': ' + v + '\r\n' for k, v in fields.items()) + '\r\n'
        connection = MemoryConnection(wire.encode('ascii'))
        server.Handler(connection, ('127.0.0.1', 12345), host)
        stream = BytesIO(connection.response)
        status = int(stream.readline().split()[1])
        response_headers = parse_headers(stream)
        body = stream.read()
        assert int(response_headers['Content-Length']) == len(body)
        return status, response_headers, body
    return request


def assert_existing_csp(headers):
    values = headers.get_all('Content-Security-Policy')
    assert values is not None and len(values) == 1
    directives = [part.strip().split() for part in values[0].split(';') if part.strip()]
    assert len({part[0] for part in directives}) == len(directives)
    assert {part[0]: part[1:] for part in directives} == {
        'default-src': ["'none'"], 'script-src': ["'self'"], 'style-src': ["'self'"],
        'connect-src': ["'self'"], 'img-src': ["'self'"], 'base-uri': ["'none'"],
        'frame-ancestors': ["'none'"], 'form-action': ["'self'"],
    }


@pytest.mark.parametrize('path,authenticated,headers,status,kind,error', [
    ('/', False, {}, 200, 'text/html', None),
    ('/health', False, {}, 200, 'application/json', None),
    ('/api/v1/overview', True, {}, 200, 'application/json', None),
    ('/api/v1/overview', False, {}, 401, 'application/json', 'TOKEN_REQUIRED'),
    ('/', False, {'Host': 'foreign.example'}, 403, 'application/json', 'HOST_REJECTED'),
    ('/missing', True, {}, 404, 'application/json', 'NOT_FOUND'),
    ('/?unexpected=query', False, {}, 400, 'application/json', 'PATH_REJECTED'),
])
def test_html_json_and_request_errors_retain_csp(respond, path, authenticated, headers, status, kind, error):
    actual, received, body = respond(path, authenticated=authenticated, headers=headers)
    assert actual == status and received.get_content_type() == kind
    assert_existing_csp(received)
    if kind == 'text/html':
        assert b'Keel' in body
    else:
        payload = json.loads(body)
        if error:
            assert payload['error']['code'] == error
        else:
            assert 'error' not in payload


@pytest.mark.parametrize('exception,status,code', [
    (Conflict('synthetic conflict'), 409, 'CONFLICT'),
    (ValueError('synthetic invalid input'), 400, 'INVALID_INPUT'),
    (RuntimeError('PRIVATE_SYNTHETIC_EXCEPTION'), 500, 'INTERNAL_ERROR'),
])
def test_application_error_responses_retain_csp(respond, monkeypatch, exception, status, code):
    def fail(*args):
        raise exception
    monkeypatch.setattr(server, 'dispatch', fail)
    actual, headers, body = respond('/api/v1/overview')
    assert actual == status and headers.get_content_type() == 'application/json'
    assert_existing_csp(headers)
    assert json.loads(body)['error']['code'] == code
    assert b'PRIVATE_SYNTHETIC_EXCEPTION' not in body
