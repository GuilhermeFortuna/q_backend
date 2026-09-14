from __future__ import annotations

import socket
import uuid
from pathlib import Path
import pytest

from q_backend.observability.systemd import EX_CONFIG, notify, notify_ready, notify_status


def test_ex_config_constant():
    assert EX_CONFIG == 78


def test_notify_when_socket_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify_ready() is False
    assert notify_status("starting") is False
    assert notify("READY=1") is False


def test_notify_filesystem_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sock_path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(sock_path)
        server.settimeout(2.0)
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        assert notify_ready() is True
        data, _ = server.recvfrom(1024)
        assert data == b"READY=1"

        assert notify_status("serving requests") is True
        data, _ = server.recvfrom(1024)
        assert data == b"STATUS=serving requests"


def test_notify_abstract_socket(monkeypatch: pytest.MonkeyPatch):
    abstract_name = f"q-test-{uuid.uuid4().hex}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        # In Linux AF_UNIX, abstract addresses begin with a NUL byte (\0)
        server.bind("\0" + abstract_name)
        server.settimeout(2.0)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + abstract_name)

        assert notify_ready() is True
        data, _ = server.recvfrom(1024)
        assert data == b"READY=1"
