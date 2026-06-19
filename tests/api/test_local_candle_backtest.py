"""Candle backtest over local OHLCV store without MT5 (WO48)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from q_backend.api.routers.backtest import run_backtest
from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.storage.runtime_config import set_data_source


def _daily_bars() -> list[OHLCV]:
    bars: list[OHLCV] = []
    for day in range(1, 61):
        close = 100.0 + day * 0.5
        bars.append(
            OHLCV(
                time=datetime(2024, 1, 1) + timedelta(days=day - 1),
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                tick_volume=1000,
            )
        )
    return bars


@pytest.fixture
def local_market(tmp_path, monkeypatch):
    market = tmp_path / "market"
    runtime = tmp_path / "runtime_config.json"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(market))
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(runtime))
    local_store.write_ohlcv("PETR4", "D1", _daily_bars())
    set_data_source("local")
    yield


def test_candle_backtest_local_mode_without_mt5(local_market):
    request = BacktestRequest(
        symbol="PETR4",
        timeframe="D1",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 2, 29),
        strategy="MACrossover",
        strategy_params={"fast_period": 5, "slow_period": 20},
    )

    with patch(
        "q_backend.api.dependencies.market_data_service.mt5_client.get_ohlcv",
        side_effect=AssertionError("MT5 must not be called in local mode"),
    ):
        response = run_backtest(request)

    assert response["metrics"]["total_trades"] >= 0
    assert response["bars"]
