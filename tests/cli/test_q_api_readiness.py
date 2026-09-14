from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config

from q_backend.observability.systemd import EX_CONFIG
from q_backend.storage.db.migrations import alembic_ini_path, schema_revision
from q_backend.storage.db.engine import get_engine


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _create_notify_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.setblocking(False)
    return sock


def _recv_datagram(sock: socket.socket, timeout: float = 30.0) -> bytes:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(1024)
            return data
        except BlockingIOError:
            time.sleep(0.05)
    raise TimeoutError(f"No datagram received within {timeout}s")


def _has_datagram(sock: socket.socket) -> bool:
    try:
        sock.recvfrom(1024)
        return True
    except BlockingIOError:
        return False


def _can_connect_tcp(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _q_api_command(port: int) -> list[str]:
    venv_bin = Path(__file__).resolve().parents[2] / ".venv/bin/q-api"
    if venv_bin.exists():
        return [str(venv_bin), "--port", str(port)]
    return [sys.executable, "-m", "q_backend.cli.q_api", "--port", str(port)]


@pytest.mark.integration
def test_q_api_readiness_migrated_postgres(tmp_path: Path):
    notify_path = tmp_path / "notify.sock"
    notify_sock = _create_notify_socket(notify_path)
    port = _find_free_port()

    env = os.environ.copy()
    env["NOTIFY_SOCKET"] = str(notify_path)
    cmd = _q_api_command(port)

    proc = subprocess.Popen(cmd, env=env)
    try:
        data = _recv_datagram(notify_sock, timeout=30.0)
        assert data == b"READY=1"
        assert _can_connect_tcp(port) is True
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        notify_sock.close()


@pytest.mark.integration
def test_q_api_readiness_redis_unreachable(tmp_path: Path):
    notify_path = tmp_path / "notify.sock"
    notify_sock = _create_notify_socket(notify_path)
    port = _find_free_port()

    env = os.environ.copy()
    env["NOTIFY_SOCKET"] = str(notify_path)
    env["Q_REDIS_URL"] = "redis://127.0.0.1:1/0"
    cmd = _q_api_command(port)

    proc = subprocess.Popen(cmd, env=env)
    try:
        data = _recv_datagram(notify_sock, timeout=30.0)
        assert data == b"READY=1"
        assert _can_connect_tcp(port) is True
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        notify_sock.close()


@pytest.mark.integration
def test_q_api_fails_when_schema_behind(tmp_path: Path):
    ini_path = alembic_ini_path()
    alembic_cfg = Config(str(ini_path))
    engine = get_engine()

    # Downgrade by 1 revision
    command.downgrade(alembic_cfg, "-1")
    try:
        rev_downgraded = schema_revision(engine, ini_path)
        assert rev_downgraded.current != rev_downgraded.head

        notify_path = tmp_path / "notify.sock"
        notify_sock = _create_notify_socket(notify_path)
        port = _find_free_port()

        env = os.environ.copy()
        env["NOTIFY_SOCKET"] = str(notify_path)
        cmd = _q_api_command(port)

        proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            stdout, stderr = proc.communicate(timeout=10.0)
            assert proc.returncode == EX_CONFIG
            assert rev_downgraded.current in stderr
            assert rev_downgraded.head in stderr
            assert _has_datagram(notify_sock) is False
        finally:
            notify_sock.close()
    finally:
        command.upgrade(alembic_cfg, "head")
