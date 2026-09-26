"""Block external connections; Windows asyncio needs loopback socket pairs."""
import socket

import pytest


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)
        raise AssertionError("External network access is forbidden in tests")

    def guarded_ex(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect_ex(sock, address)
        raise AssertionError("External network access is forbidden in tests")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_ex)
