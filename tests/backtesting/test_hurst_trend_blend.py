from datetime import datetime, timedelta, timezone
import math
import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.models import SignalAction
from q_backend.backtesting.position_sizing import (
    InverseVolatilityPositionSizing,
    build_position_sizer,
)


def _ohlc_frame(closes: list[float]) -> pd.DataFrame:
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(days=i) for i in range(len(closes))]
    close = np.array(closes, dtype=float)
    open_ = close.copy()
    high = close + 0.01
    low = close - 0.01
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close},
        index=times,
    )


class TestHurstTrendBlendStrategy:
    def test_indicator_blend_calculation(self):
        strategy = build_strategy(
            "HurstTrendBlend",
            {
                "lookback_1": 2,
                "lookback_2": 4,
                "lookback_3": 6,
                "rebalance_bars": 2,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
                "risk_free_rate_annual": 0.0,
                "signal_lag_bars": 0,
                "rebalance_on_every_bar": "false",
            },
            "TEST",
        )

        # 8 bars: index 0 to 7
        closes = [100.0, 101.0, 102.0, 103.0, 102.0, 101.0, 102.0, 103.0]
        df = _ohlc_frame(closes)
        res = strategy.compute_indicators(df)

        # Check required columns
        assert "sig_1" in res.columns
        assert "sig_2" in res.columns
        assert "sig_3" in res.columns
        assert "momentum" in res.columns
        assert "signal_strength" in res.columns

        # Verify specific bar indicators (index 7, lookbacks 2, 4, 6)
        # Closes:
        # t=7 (103.0) vs t=5 (101.0) -> +2.0 (sig_1 = +1)
        # t=7 (103.0) vs t=3 (103.0) -> 0.0 (sig_2 = 0)
        # t=7 (103.0) vs t=1 (101.0) -> +2.0 (sig_3 = +1)
        # Blend should be (1 + 0 + 1) / 3 = 0.6666...
        # signal_strength should be 0.6666...
        assert res["sig_1"].iloc[7] == 1.0
        assert res["sig_2"].iloc[7] == 0.0
        assert res["sig_3"].iloc[7] == 1.0
        assert res["momentum"].iloc[7] == pytest.approx(2.0 / 3.0)
        assert res["signal_strength"].iloc[7] == pytest.approx(2.0 / 3.0)

    def test_signal_lag(self):
        strategy = build_strategy(
            "HurstTrendBlend",
            {
                "lookback_1": 2,
                "lookback_2": 3,
                "lookback_3": 4,
                "rebalance_bars": 2,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
                "risk_free_rate_annual": 0.0,
                "signal_lag_bars": 2,
                "rebalance_on_every_bar": "false",
            },
            "TEST",
        )

        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0]
        df = _ohlc_frame(closes)
        
        # Unlagged strategy
        strategy_no_lag = build_strategy(
            "HurstTrendBlend",
            {
                "lookback_1": 2,
                "lookback_2": 3,
                "lookback_3": 4,
                "rebalance_bars": 2,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
                "risk_free_rate_annual": 0.0,
                "signal_lag_bars": 0,
                "rebalance_on_every_bar": "false",
            },
            "TEST",
        )

        res_lag = strategy.compute_indicators(df.copy())
        res_no_lag = strategy_no_lag.compute_indicators(df.copy())

        # Check shift of 2 bars for valid non-NaN indices
        assert res_lag["momentum"].iloc[6] == res_no_lag["momentum"].iloc[4]
        assert res_lag["momentum"].iloc[7] == res_no_lag["momentum"].iloc[5]
        assert np.isnan(res_lag["momentum"].iloc[0])
        assert np.isnan(res_lag["momentum"].iloc[1])

    def test_risk_free_rate_subtraction(self):
        # High annual interest rate: 50400% (to make daily rf = 200% = 2.0 per bar)
        # Excess returns will be heavily penalized
        strategy = build_strategy(
            "HurstTrendBlend",
            {
                "lookback_1": 2,
                "lookback_2": 4,
                "lookback_3": 6,
                "rebalance_bars": 2,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
                "risk_free_rate_annual": 504.0,  # 50400% (rf_daily = 504 / 252 = 2.0)
                "signal_lag_bars": 0,
                "rebalance_on_every_bar": "false",
            },
            "TEST",
        )

        # Closes: returns are positive but smaller than interest rate penalty
        # t=2 (102.0) vs t=0 (100.0) -> +2.0% return
        # rf_daily * lookback_1 = 2.0 * 2 = 4.0% interest rate penalty
        # excess return = 2.0% - 4.0% = -2.0% -> sig_1 should be -1.0 (despite positive raw return)
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        df = _ohlc_frame(closes)
        res = strategy.compute_indicators(df)

        assert res["sig_1"].iloc[2] == -1.0


class TestHurstTrendBlendIntegration:
    def test_backtest_execution_and_scaling(self):
        strategy = build_strategy(
            "HurstTrendBlend",
            {
                "lookback_1": 2,
                "lookback_2": 4,
                "lookback_3": 6,
                "rebalance_bars": 3,
                "vol_window": 3,
                "vol_estimator": "close_to_close",
                "risk_free_rate_annual": 0.0,
                "signal_lag_bars": 0,
                "rebalance_on_every_bar": "true",
            },
            "TEST",
        )

        # 12 bars to allow rebalancing and signals
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 103.0, 102.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        df = _ohlc_frame(closes)

        # Setup sizer with signal strength scaling enabled
        sizer = build_position_sizer(
            InverseVolatilityPositionSizing(
                target_volatility_pct=10.0,
                scale_by_signal_strength=True,
            )
        )

        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=100000.0,
            point_values={"TEST": 1.0},
        )

        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)
        trades = registry.get_all_trades()

        assert len(trades) > 0, "expected at least one trade"
        
        # Verify that all executed trades have integer-valued quantities
        for trade in trades:
            assert trade.quantity.is_integer()
            assert trade.quantity > 0.0
