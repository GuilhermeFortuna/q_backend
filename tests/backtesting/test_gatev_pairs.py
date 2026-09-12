from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    build_position_sizer,
)


def _ohlc_pairs_frame(closes_a: list[float], closes_b: list[float]) -> pd.DataFrame:
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    n = len(closes_a)
    times = [base + timedelta(days=i) for i in range(n)]
    close_a = np.array(closes_a, dtype=float)
    close_b = np.array(closes_b, dtype=float)
    open_a = close_a.copy()
    open_b = close_b.copy()
    return pd.DataFrame(
        {
            "close_a": close_a,
            "close_b": close_b,
            "open_a": open_a,
            "open_b": open_b,
        },
        index=times,
    )


class TestGatevPairsStrategy:
    def test_indicator_calculation_short_entry_and_exit(self):
        strategy = build_strategy(
            "GatevPairs",
            {
                "col_a": "close_a",
                "col_b": "close_b",
                "open_col_a": "open_a",
                "open_col_b": "open_b",
                "formation_bars": 4,
                "trading_bars": 2,
                "open_threshold_sd": 2.0,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
            },
            "PAIR",
        )

        # Closes:
        # A remains flat at 100.0, B has variation
        closes_a = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        closes_b = [100.0, 101.0, 99.0, 100.0, 98.0, 100.0]

        df = _ohlc_pairs_frame(closes_a, closes_b)
        res = strategy.compute_indicators(df)

        # Check required columns
        assert "close" in res.columns
        assert "open" in res.columns
        assert "buy_signal" in res.columns
        assert "sell_signal" in res.columns
        assert "exit_signal" in res.columns
        assert "spread" in res.columns

        # Verify spread and signals
        # Index 0: normalized a = 1.0, b = 1.0 -> spread = 0.0
        # Index 1: normalized a = 1.0, b = 1.01 -> spread = -0.01
        # Index 2: normalized a = 1.0, b = 0.99 -> spread = 0.01
        # Index 3: normalized a = 1.0, b = 1.00 -> spread = 0.00
        # Mean = 0.0. Variance = (0.0^2 + 0.01^2 + 0.01^2 + 0.0^2)/4 = 0.00005. SD = sqrt(0.00005) = 0.007071
        # Threshold = 2 * 0.007071 = 0.01414

        # Index 4: normalized a = 1.0, b = 0.98 -> spread = 0.02
        # Since 0.02 > 0.01414, it should trigger sell_signal (short spread)
        assert res["spread"].iloc[4] == pytest.approx(0.02)
        assert res["sell_signal"].iloc[4]
        assert not res["buy_signal"].iloc[4]
        assert not res["exit_signal"].iloc[4]

        # Index 5: normalized a = 1.0, b = 1.0 -> spread = 0.0
        # Since spread crosses zero (is 0.0) and k = 5 is the end of the trading period, it should exit.
        assert res["spread"].iloc[5] == pytest.approx(0.0)
        assert res["exit_signal"].iloc[5]
        assert not res["sell_signal"].iloc[5]
        assert not res["buy_signal"].iloc[5]

    def test_indicator_calculation_long_entry_and_exit(self):
        strategy = build_strategy(
            "GatevPairs",
            {
                "col_a": "close_a",
                "col_b": "close_b",
                "open_col_a": "open_a",
                "open_col_b": "open_b",
                "formation_bars": 4,
                "trading_bars": 2,
                "open_threshold_sd": 2.0,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
            },
            "PAIR",
        )

        closes_a = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        closes_b = [100.0, 99.0, 101.0, 100.0, 102.0, 100.0]

        df = _ohlc_pairs_frame(closes_a, closes_b)
        res = strategy.compute_indicators(df)

        # Index 4: normalized a = 1.0, b = 1.02 -> spread = -0.02
        # Since -0.02 < -0.01414, it should trigger buy_signal (long spread)
        assert res["spread"].iloc[4] == pytest.approx(-0.02)
        assert res["buy_signal"].iloc[4]
        assert not res["sell_signal"].iloc[4]
        assert not res["exit_signal"].iloc[4]

        # Index 5: normalized a = 1.0, b = 1.0 -> spread = 0.0
        # Exit trigger since spread crosses zero.
        assert res["spread"].iloc[5] == pytest.approx(0.0)
        assert res["exit_signal"].iloc[5]


class TestGatevPairsIntegration:
    def test_backtest_execution(self):
        strategy = build_strategy(
            "GatevPairs",
            {
                "col_a": "close_a",
                "col_b": "close_b",
                "open_col_a": "open_a",
                "open_col_b": "open_b",
                "formation_bars": 10,
                "trading_bars": 10,
                "open_threshold_sd": 1.5,
                "vol_window": 5,
                "vol_estimator": "close_to_close",
            },
            "PAIR",
        )

        # Generate a synthetic converging/diverging dataset of 30 bars
        closes_a = [100.0] * 30
        closes_b = [100.0] * 30

        # Cycle 1: Formation 0..9. We make B fluctuate around 100
        for i in range(10):
            if i % 2 == 1:
                closes_b[i] = 101.0
            else:
                closes_b[i] = 99.0

        # Cycle 1: Trading 10..19. We make B diverge heavily, then converge
        closes_b[11] = 95.0  # Divergence: B underperforms. Spread = 1.0 - 0.95 = 0.05. Trigger sell spread.
        closes_b[12] = 95.0
        closes_b[13] = 100.0  # Convergence: Spread = 0.0. Trigger exit.

        # Cycle 2: Formation 20..29. B fluctuates.
        for i in range(20, 30):
            if i % 2 == 1:
                closes_b[i] = 101.0
            else:
                closes_b[i] = 99.0

        df = _ohlc_pairs_frame(closes_a, closes_b)

        sizer = build_position_sizer(FixedQuantityPositionSizing(quantity=2.0))

        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=100000.0,
            point_values={"PAIR": 1.0},
        )

        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)
        trades = registry.get_all_trades()

        assert len(trades) > 0, "Expected at least one trade to execute"

        for trade in trades:
            assert trade.quantity.is_integer()
            assert trade.quantity == 2.0
