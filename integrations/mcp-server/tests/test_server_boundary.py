"""Entrypoint boundary tests with a transport double, not an SDK handshake."""
import argparse
import importlib.util
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'mcp-server'))
from safety import loopback_host


@pytest.mark.parametrize('host', ['0.0.0.0', '::', '192.168.1.3', '8.8.8.8',
                                 'localhost', 'example.com', '::ffff:127.0.0.1',
                                 '::1%lo', '', '127.0.0.1:8765'])
def test_nonlocal_or_ambiguous_hosts_refused(host):
    with pytest.raises(argparse.ArgumentTypeError):
        loopback_host(host)


@pytest.mark.parametrize('host', ['127.0.0.1', '127.0.0.2', '::1'])
def test_literal_loopback_allowed(host):
    assert loopback_host(host) == host


def load_server(monkeypatch):
    class TransportDouble:
        def __init__(self, **kwargs):
            self.calls = []
        def tool(self, *args):
            return lambda function: function
        resource = prompt = tool
        def run(self, **kwargs):
            self.calls.append(kwargs)
    module = types.ModuleType('mcp.server.mcpserver')
    module.MCPServer = TransportDouble
    monkeypatch.setitem(sys.modules, 'mcp.server.mcpserver', module)
    spec = importlib.util.spec_from_file_location('keel_mcp_boundary_test',
        Path(__file__).resolve().parent.parent / 'mcp-server/server.py')
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


def test_rejected_host_never_initializes_data_or_runs_transport(monkeypatch):
    server = load_server(monkeypatch)
    def forbidden(*args):
        pytest.fail('invalid host must be refused before data initialization')
    monkeypatch.setattr(server, 'resolve_data_dir', forbidden)
    monkeypatch.setattr(sys, 'argv', ['server.py', '--host', '0.0.0.0'])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    assert server.mcp.calls == []


@pytest.mark.parametrize('args,host', [([], '127.0.0.1'), (['--host', '::1'], '::1')])
def test_entrypoint_passes_only_validated_host_to_transport(monkeypatch, args, host):
    server = load_server(monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['server.py', *args])
    monkeypatch.setattr(server.keel_bridge, 'set_data_dir', lambda path: None)
    server.main()
    assert server.mcp.calls == [{'transport': 'streamable-http', 'host': host, 'port': 8765}]
