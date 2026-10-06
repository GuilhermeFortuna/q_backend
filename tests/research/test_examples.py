"""Tests exercising research examples: offline Parquet RSI and mocked MT5 backtest."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from examples.research.mt5_backtest import run_mt5_workflow
from examples.research.rsi_reversion import RSIReversion, run_rsi_backtest
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import indicators


@pytest.fixture
def synthetic_rsi_parquet(tmp_path: Path) -> Path:
    # 40 bars with a clear dip below 30 and recovery above 50
    idx = pd.date_range("2026-09-01 09:00", periods=40, freq="5min", tz=BRASILIA_TZ, name="time")
    # Base price path: start 100, drop sharply, then rise
    closes = np.full(40, 100.0)
    for i in range(15, 25):
        closes[i] = 100.0 - (i - 14) * 3.0  # drop to 70
    for i in range(25, 40):
        closes[i] = 70.0 + (i - 24) * 2.5  # rise to 107.5

    opens = closes - 0.2
    highs = closes + 0.5
    lows = closes - 0.5
    tick_volumes = np.full(40, 100, dtype=np.int64)

    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": tick_volumes,
        },
        index=idx,
    )
    file_path = tmp_path / "bars.parquet"
    df.to_parquet(file_path)
    return file_path


def test_offline_rsi_example_execution(synthetic_rsi_parquet: Path) -> None:
    result = run_rsi_backtest(
        synthetic_rsi_parquet,
        symbol="WIN$",
        period=14,
        quantity=1,
        point_value=0.20,
        capital=10000.0,
    )
    assert "total_trades" in result.metrics
    assert "realized_equity" in result.equity.columns
    assert "rsi" in result.data.columns
    assert "q_signal_entry" not in result.data.columns


def test_rsi_prefix_stability_checkpoints(synthetic_rsi_parquet: Path) -> None:
    """Verify that RSIReversion decisions at fixed bar checkpoints match when evaluated on prefixes."""
    df = pd.read_parquet(synthetic_rsi_parquet)
    strat = RSIReversion(period=14)

    # Check bar 20, 25, 30
    checkpoints = [20, 25, 30]
    for cp in checkpoints:
        prefix = df.iloc[:cp].copy()
        prefix_aug = strat.compute_indicators(prefix)
        entry_dec = strat.entry_strategy(prefix_aug)
        exit_dec = strat.exit_strategy(prefix_aug)

        # Full frame evaluation
        full_aug = strat.compute_indicators(df.copy())
        full_prefix = full_aug.iloc[:cp].copy()
        entry_full = strat.entry_strategy(full_prefix)
        exit_full = strat.exit_strategy(full_prefix)

        # Verify rsi value at checkpoint matches exactly
        assert prefix_aug["rsi"].iloc[-1] == pytest.approx(full_aug["rsi"].iloc[cp - 1])
        assert entry_dec == entry_full
        assert exit_dec == exit_full


def test_mt5_example_mocked_gateway(synthetic_rsi_parquet: Path) -> None:
    mock_bars = pd.read_parquet(synthetic_rsi_parquet)

    with patch("examples.research.mt5_backtest.load_bars", return_value=mock_bars) as mock_load:
        # 1. Custom strategy
        res_custom = run_mt5_workflow(
            symbol="WIN$",
            timeframe="M5",
            start="2026-09-01",
            strategy_mode="custom",
        )
        assert mock_load.called
        assert "total_trades" in res_custom.metrics

        # 2. Builtin strategy
        res_builtin = run_mt5_workflow(
            symbol="WIN$",
            timeframe="M5",
            start="2026-09-01",
            strategy_mode="builtin",
        )
        assert "total_trades" in res_builtin.metrics
