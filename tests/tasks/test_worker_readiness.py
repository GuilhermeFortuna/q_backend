from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
import pytest


def _create_notify_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.setblocking(False)
    return sock


def _recv_datagram(sock: socket.socket, timeout: float = 60.0) -> bytes:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(1024)
            return data
        except BlockingIOError:
            time.sleep(0.1)
    raise TimeoutError(f"No datagram received within {timeout}s")


@pytest.mark.integration
def test_worker_readiness_and_shutdown(tmp_path: Path):
    notify_path = tmp_path / "notify.sock"
    notify_sock = _create_notify_socket(notify_path)

    worker_bin = Path(__file__).resolve().parents[2] / ".venv/bin/worker"
    cmd = [str(worker_bin)] if worker_bin.exists() else [sys.executable, "-m", "q_backend.cli.worker"]

    env = os.environ.copy()
    env["NOTIFY_SOCKET"] = str(notify_path)
    env["Q_WORKER_PROCESSES"] = "1"

    proc = subprocess.Popen(cmd, env=env)
    try:
        data = _recv_datagram(notify_sock, timeout=60.0)
        assert data == b"READY=1"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=60.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        notify_sock.close()

    assert proc.returncode == 0
