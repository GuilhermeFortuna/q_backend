from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis
import pytest

from q_backend.cli import q_market_publisher
from q_backend.observability.systemd import EX_CONFIG
from q_backend.streaming.market.publisher import MarketDataPublisher


def test_market_publisher_no_symbols_returns_ex_config(capsys):
    ret = q_market_publisher.main([])
    assert ret == EX_CONFIG
    assert "no symbols configured" in capsys.readouterr().err


def test_market_publisher_readiness_before_first_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sock_path = str(tmp_path / "notify.sock")
    notify_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify_sock.bind(sock_path)
    notify_sock.settimeout(2.0)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    # Point gateway to a stopped/unreachable port
    monkeypatch.setenv("Q_MT5_GATEWAY_URL", "http://127.0.0.1:1")

    # Use fakeredis for Redis client
    fake_client = fakeredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(q_market_publisher, "get_binary_redis", lambda: fake_client)

    run_forever_called = False

    def mock_run_forever(self, stop):
        nonlocal run_forever_called
        run_forever_called = True
        # Verify READY=1 was received before the first poll inside run_forever
        data, _ = notify_sock.recvfrom(1024)
        assert data == b"READY=1"

    monkeypatch.setattr(MarketDataPublisher, "run_forever", mock_run_forever)

    ret = q_market_publisher.main(["--symbols", "PETR4", "--timeframes", "M1"])
    assert ret == 0
    assert run_forever_called is True
    notify_sock.close()
