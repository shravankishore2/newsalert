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


@pytest.fixture(autouse=True)
def token_authority(monkeypatch):
    """Tests act as the VM (the one machine allowed to generate Dhan tokens);
    tests of the refusal clear this themselves."""
    import newsalert.auth as auth
    monkeypatch.setenv(auth.TOKEN_AUTHORITY_ENV, "1")
    monkeypatch.setattr(auth, "_override", False)
