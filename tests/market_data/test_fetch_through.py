"""Fetch-through cache: gateway reads are persisted into the local parquet store.

The remote client is stubbed (no HTTP). Write-behind must be best-effort: a
`write_ohlcv`/`write_ticks` failure is swallowed + logged and the caller still
receives the data returned by the gateway (WO185 guardrail).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
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
    set_data_source("remote")
    yield


class _StubRemote:
    """Gateway stand-in resolved by `data_source='remote'`."""

    def __init__(self, *, bars=None, arrays=None):
        self._bars = bars
        self._arrays = arrays

    def is_supported(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def get_ohlcv(self, symbol, timeframe, start, end):
        return list(self._bars or [])

    def get_ticks_columnar(self, symbol, start, end, flags=None, use_cache=True):
        return self._arrays


def _bars() -> list[OHLCV]:
    return [
        OHLCV(
            time=datetime(2024, 3, day),
            open=40.0 + day,
            high=41.0 + day,
            low=39.0 + day,
            close=40.5 + day,
            tick_volume=1000 + day,
            spread=1,
            real_volume=0,
        )
        for day in range(1, 4)
    ]


def _ticks() -> dict[str, np.ndarray]:
    base = int(datetime(2024, 3, 1).timestamp() * 1000)
    return {
        "time_msc": np.array([base, base + 1000, base + 2000], dtype=np.int64),
        "bid": np.array([40.0, 40.1, 40.2], dtype=np.float64),
        "ask": np.array([40.2, 40.3, 40.4], dtype=np.float64),
        "last": np.array([40.1, 40.2, 40.3], dtype=np.float64),
        "volume": np.array([1, 2, 3], dtype=np.float64),
        "flags": np.array([0, 0, 0], dtype=np.int32),
    }


def test_ohlcv_fetch_through_persists_to_local_store(market_root):
    service = MarketDataService()
    bars = _bars()
    service._remote_client = _StubRemote(bars=bars)

    result = service.get_ohlcv("PETR4", "D1", datetime(2024, 3, 1), datetime(2024, 3, 4))

    assert result == bars
    stored = local_store.read_ohlcv("PETR4", "D1", datetime(2024, 3, 1), datetime(2024, 3, 4))
    assert stored == bars


def test_ohlcv_fetch_through_write_failure_is_swallowed(market_root, monkeypatch):
    service = MarketDataService()
    bars = _bars()
    service._remote_client = _StubRemote(bars=bars)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(local_store, "write_ohlcv", _boom)

    result = service.get_ohlcv("PETR4", "D1", datetime(2024, 3, 1), datetime(2024, 3, 4))

    # Caller still gets the gateway bars even though caching failed.
    assert result == bars


def test_tick_fetch_through_persists_to_local_store(market_root):
    service = MarketDataService()
    arrays = _ticks()
    service._remote_client = _StubRemote(arrays=arrays)

    result = service.get_ticks_columnar("PETR4", datetime(2024, 3, 1), datetime(2024, 3, 2), use_cache=False)

    assert result is arrays
    stored = local_store.read_ticks_columnar("PETR4", datetime(2024, 3, 1), datetime(2024, 3, 2))
    assert len(stored["time_msc"]) == 3


def test_tick_fetch_through_write_failure_is_swallowed(market_root, monkeypatch):
    service = MarketDataService()
    arrays = _ticks()
    service._remote_client = _StubRemote(arrays=arrays)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(local_store, "write_ticks", _boom)

    result = service.get_ticks_columnar("PETR4", datetime(2024, 3, 1), datetime(2024, 3, 2), use_cache=False)

    assert result is arrays
