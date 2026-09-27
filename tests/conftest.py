import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that tries to open a real network connection."""
    def guard(*args, **kwargs):
        raise RuntimeError("network access attempted during tests")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
