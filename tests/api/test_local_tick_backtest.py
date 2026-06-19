"""Tick backtest over local tick store without MT5 (WO50)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
from fastapi import HTTPException

from q_backend.api.main import BacktestRequest, run_backtest
from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc
from q_backend.storage.runtime_config import set_data_source


def _synthetic_ticks(n: int = 500) -> dict[str, np.ndarray]:
    base = datetime(2024, 1, 2, 10, 0, 0)
    time_msc = np.array(
        [_naive_local_to_time_msc(base + timedelta(seconds=i)) for i in range(n)],
        dtype=np.int64,
    )
    prices = 100.0 + np.cumsum(np.sin(np.linspace(0, 12, n)) * 0.05)
    return {
        "time_msc": time_msc,
        "bid": (prices - 0.01).astype(np.float64),
        "ask": (prices + 0.01).astype(np.float64),
        "last": prices.astype(np.float64),
        "volume": np.ones(n, dtype=np.float64),
        "flags": np.zeros(n, dtype=np.int32),
    }


@pytest.fixture
def local_tick_market(tmp_path, monkeypatch):
    market = tmp_path / "market"
    runtime = tmp_path / "runtime_config.json"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(market))
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(runtime))
    local_store.write_ticks("WIN$", _synthetic_ticks())
    set_data_source("local")
    yield


def test_tick_backtest_local_mode_without_mt5(local_tick_market):
    request = BacktestRequest(
        symbol="WIN$",
        engine="tick",
        display_timeframe="M1",
        start=datetime(2024, 1, 2, 10, 0, 0),
        end=datetime(2024, 1, 2, 10, 8, 0),
        initial_capital=100_000.0,
        point_value=0.2,
        strategy="TickMaBreakout",
        strategy_params={
            "short_period": 5,
            "long_period": 20,
            "threshold": 0.0,
            "sl_points": 1.0,
            "tp_points": 2.0,
        },
    )

    with patch(
        "q_backend.api.main.market_data_service.mt5_client.get_ticks_columnar",
        side_effect=AssertionError("MT5 must not be called in local mode"),
    ):
        response = run_backtest(request)

    assert "total_pnl" in response["metrics"]
    assert isinstance(response["bars"], list)
    assert len(response["bars"]) > 0


def test_tick_backtest_local_mode_missing_ticks_returns_404(local_tick_market):
    request = BacktestRequest(
        symbol="MISSING",
        engine="tick",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 1, 2),
        strategy="TickMaBreakout",
        strategy_params={"short_period": 5, "long_period": 10},
    )

    with pytest.raises(HTTPException) as exc:
        run_backtest(request)

    assert exc.value.status_code == 404
    assert "Ingest ticks" in exc.value.detail
