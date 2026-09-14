from __future__ import annotations

from pathlib import Path
import socket
import time
from typing import Generator
import pytest


class NotifySocketReceiver:
    def __init__(self, sock: socket.socket, path: Path) -> None:
        self.sock = sock
        self.path = path

    def recv_one(self, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = self.sock.recvfrom(1024)
                return data
            except BlockingIOError:
                time.sleep(0.05)
        raise TimeoutError(f"No datagram received within {timeout}s")

    def drain(self) -> list[bytes]:
        received: list[bytes] = []
        while True:
            try:
                data, _ = self.sock.recvfrom(1024)
                received.append(data)
            except BlockingIOError:
                break
        return received


@pytest.fixture
def bound_notify_socket(tmp_path: Path) -> Generator[NotifySocketReceiver, None, None]:
    sock_path = tmp_path / "test_notify.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    sock.setblocking(False)
    receiver = NotifySocketReceiver(sock, sock_path)
    try:
        yield receiver
    finally:
        sock.close()
