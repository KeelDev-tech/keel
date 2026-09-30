"""Adapter contract tests must never reach real network transports."""
import os
import socket
import subprocess

import pytest


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch):
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append("external effect")
        raise AssertionError('MCP contract tests forbid network and child processes')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    yield
    assert not attempts, 'external effects were attempted even if the tool caught the denial'
