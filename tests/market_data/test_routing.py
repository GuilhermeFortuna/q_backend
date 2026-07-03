"""OHLCV routing when the MT5 package is present but the terminal is offline."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.market_data.routing import resolve_ohlcv_source
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import set_data_source


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    monkeypatch.delenv("Q_MT5_GATEWAY_URL", raising=False)
    monkeypatch.delenv("Q_MT5_GATEWAY_TOKEN", raising=False)
    set_data_source("auto")
    yield


class _StubRemote:
    """Minimal stand-in for RemoteMt5Client used to drive routing decisions."""

    def __init__(self, *, available: bool = False, supported: bool = True):
        self._available = available
        self._supported = supported
        self.is_available_calls = 0

    def is_available(self) -> bool:
        self.is_available_calls += 1
        return self._available

    def is_supported(self) -> bool:
        return self._supported


def test_auto_mode_uses_local_when_mt5_offline(market_root):
    local_store.write_ohlcv(
        "CCM$",
        "H1",
        [
            OHLCV(
                time=datetime(2024, 1, 1, 10),
                open=1,
                high=1,
                low=1,
                close=1,
                tick_volume=1,
            )
        ],
    )
    service = MarketDataService()

    service._remote_client = _StubRemote(available=False)
    with patch.object(service, "mt5_available", return_value=False):
        assert resolve_ohlcv_source(service, "CCM$", "H1") == "local"
        bars = service.get_ohlcv(
            "CCM$",
            "H1",
            datetime(2024, 1, 1),
            datetime(2024, 1, 2),
        )

    assert len(bars) == 1


def test_auto_native_up_remote_up_prefers_mt5(market_root, monkeypatch):
    service = MarketDataService()
    service._remote_client = _StubRemote(available=True)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: True,
    )
    assert resolve_ohlcv_source(service, "PETR4", "H1") == "mt5"
    # Native won outright — the remote client should not even be probed.
    assert service._remote_client.is_available_calls == 0


def test_auto_native_down_remote_up_prefers_remote(market_root, monkeypatch):
    service = MarketDataService()
    service._remote_client = _StubRemote(available=True)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )
    assert resolve_ohlcv_source(service, "PETR4", "H1") == "remote"


def test_auto_both_down_with_local_data_prefers_local(market_root, monkeypatch):
    local_store.write_ohlcv(
        "VALE3",
        "H1",
        [
            OHLCV(
                time=datetime(2024, 1, 1, 10),
                open=1,
                high=1,
                low=1,
                close=1,
                tick_volume=1,
            )
        ],
    )
    service = MarketDataService()
    service._remote_client = _StubRemote(available=False)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )
    assert resolve_ohlcv_source(service, "VALE3", "H1") == "local"


def test_explicit_remote_without_url_raises(market_root):
    set_data_source("remote")
    service = MarketDataService()
    # No Q_MT5_GATEWAY_URL configured -> the remote client is not "supported".
    assert service._remote_client.is_supported() is False
    with pytest.raises(ConnectionError, match="no gateway URL is configured"):
        service._resolve_ohlcv_provider("PETR4", "H1")


def test_explicit_local_never_touches_remote(market_root):
    local_store.write_ohlcv(
        "CCM$",
        "H1",
        [
            OHLCV(
                time=datetime(2024, 1, 1, 10),
                open=1,
                high=1,
                low=1,
                close=1,
                tick_volume=1,
            )
        ],
    )
    set_data_source("local")
    service = MarketDataService()
    stub = _StubRemote(available=True)
    service._remote_client = stub

    assert resolve_ohlcv_source(service, "CCM$", "H1") == "local"
    bars = service.get_ohlcv("CCM$", "H1", datetime(2024, 1, 1), datetime(2024, 1, 2))
    assert len(bars) == 1
    assert stub.is_available_calls == 0
