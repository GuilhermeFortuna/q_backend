from datetime import datetime, timedelta, timezone

import math

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.models import SignalAction
from q_backend.backtesting.models import Signal, SignalAction
from q_backend.backtesting.position_sizing import (
    InverseVolatilityPositionSizing,
    build_position_sizer,
)


def _ohlc_frame(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(days=i) for i in range(len(closes))]
    if opens is None:
        opens = closes.copy()
    close = np.array(closes, dtype=float)
    open_ = np.array(opens, dtype=float)
    high = np.maximum(open_, close) + 0.01
    low = np.minimum(open_, close) - 0.01
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close},
        index=times,
    )


def _signal_bars(result: pd.DataFrame, column: str) -> list[int]:
    return [int(result.loc[ts, "bar_index"]) for ts in result.index[result[column]]]


class TestTSMOMStrategy:
    def test_trending_series_goes_long_after_warmup(self):
        strategy = build_strategy(
            "TSMOM",
            {
                "lookback_bars": 5,
                "rebalance_bars": 3,
                "vol_window": 5,
                "vol_estimator": "close_to_close",
            },
            "TEST",
        )
        closes = [100.0] * 6 + [100.0 + i for i in range(1, 16)]
        result = strategy.compute_indicators(_ohlc_frame(closes))

        assert not result["buy_signal"].iloc[:5].any()
        buy_bars = _signal_bars(result, "buy_signal")
        assert buy_bars == [6]
        assert not result["sell_signal"].iloc[:6].any()

    def test_flip_occurs_on_rebalance_not_mid_cycle(self):
        strategy = build_strategy(
            "TSMOM",
            {
                "lookback_bars": 4,
                "rebalance_bars": 5,
                "vol_window": 4,
                "vol_estimator": "close_to_close",
            },
            "TEST",
        )
        # Warm up flat, trend up through bar 9, then reverse hard from bar 10 onward.
        closes = [100.0] * 5 + [101.0, 102.0, 103.0, 104.0, 105.0]
        closes += [90.0, 88.0, 86.0, 84.0, 82.0, 80.0, 78.0, 76.0]
        result = strategy.compute_indicators(_ohlc_frame(closes))

        buy_bars = _signal_bars(result, "buy_signal")
        sell_bars = _signal_bars(result, "sell_signal")
        assert buy_bars == [5]
        assert sell_bars == [10]
        assert 10 % 5 == 0
        assert not result.loc[result.index[7:10], "sell_signal"].any()

    def test_no_signals_during_momentum_warmup(self):
        strategy = build_strategy(
            "TSMOM",
            {
                "lookback_bars": 10,
                "rebalance_bars": 2,
                "vol_window": 5,
                "vol_estimator": "close_to_close",
            },
            "TEST",
        )
        closes = [100.0 + i for i in range(15)]
        result = strategy.compute_indicators(_ohlc_frame(closes))
        warmup = result.iloc[:10]
        assert not warmup["buy_signal"].any()
        assert not warmup["sell_signal"].any()

    def test_close_only_falls_back_from_yang_zhang(self):
        strategy = build_strategy(
            "TSMOM",
            {
                "lookback_bars": 5,
                "rebalance_bars": 3,
                "vol_window": 5,
                "vol_estimator": "yang_zhang",
            },
            "TEST",
        )
        closes = [100.0] * 8 + [101.0, 102.0, 103.0, 104.0, 105.0]
        close_only = pd.DataFrame({"close": closes}, index=_ohlc_frame(closes).index)
        result = strategy.compute_indicators(close_only)
        assert result["volatility"].notna().any()


class TestInverseVolatilitySizer:
    def test_exact_contract_count(self):
        sizer = build_position_sizer(
            InverseVolatilityPositionSizing(
                target_volatility_pct=10.0,
                min_contracts=0,
            ),
            point_value=0.2,
        )
        sig = Signal(symbol="WIN$", action=SignalAction.BUY)
        row = pd.Series({"volatility": 0.20, "close": 100.0})
        order = sizer.size_signal(sig, 100.0, 100_000.0, current_data=row)
        # floor(0.10 * 100000 / (0.20 * 100 * 0.2)) = floor(2500) = 2500
        assert order is not None
        assert order.quantity == 2500.0

    def test_missing_vol_returns_none(self):
        sizer = build_position_sizer(
            InverseVolatilityPositionSizing(target_volatility_pct=10.0)
        )
        sig = Signal(symbol="WIN$", action=SignalAction.BUY)
        assert sizer.size_signal(sig, 100.0, 100_000.0, current_data=None) is None
        assert (
            sizer.size_signal(
                sig, 100.0, 100_000.0, current_data=pd.Series({"close": 100.0})
            )
            is None
        )

    def test_clamps_respected(self):
        sizer = build_position_sizer(
            InverseVolatilityPositionSizing(
                target_volatility_pct=10.0,
                min_contracts=2,
                max_contracts=3,
            ),
            point_value=1.0,
        )
        sig = Signal(symbol="ES", action=SignalAction.BUY)
        row = pd.Series({"volatility": 0.01})
        order = sizer.size_signal(sig, 100.0, 10_000.0, current_data=row)
        assert order is not None
        assert order.quantity == 3.0

    def test_existing_sizers_accept_current_data_kwarg(self):
        from q_backend.backtesting.models import Signal, SignalAction
        from q_backend.backtesting.position_sizing import (
            FixedQuantitySizer,
            FixedSafetyMarginSizer,
        )

        sig = Signal(symbol="ES", action=SignalAction.BUY)
        row = pd.Series({"volatility": 0.5})
        fq = FixedQuantitySizer(quantity=2.0)
        fsm = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
        assert fq.size_signal(sig, 100.0, 10_000.0, current_data=row).quantity == 2.0
        assert fsm.size_signal(sig, 100.0, 15_000.0, current_data=row).quantity == 3.0


class TestTSMOMInverseVolIntegration:
    def test_engine_trade_quantities_match_signal_bar_volatility(self):
        lookback = 5
        rebalance = 5
        vol_window = 5
        n = 30
        base = datetime(2023, 1, 1, tzinfo=timezone.utc)
        idx = [base + timedelta(days=i) for i in range(n)]
        rng = np.random.default_rng(0)
        noise = rng.normal(0, 2.0, n)
        close = 100.0 + np.cumsum(noise)
        open_ = close - rng.normal(0, 0.5, n)
        df = pd.DataFrame(
            {
                "open": open_,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
            },
            index=idx,
        )

        strategy = build_strategy(
            "TSMOM",
            {
                "lookback_bars": lookback,
                "rebalance_bars": rebalance,
                "vol_window": vol_window,
                "vol_estimator": "close_to_close",
            },
            "TEST",
        )
        capital = 50_000.0
        point_value = 1.0
        target_pct = 10.0
        sizer = build_position_sizer(
            InverseVolatilityPositionSizing(
                target_volatility_pct=target_pct,
                min_contracts=1,
                max_contracts=10_000,
            ),
            point_value=point_value,
        )
        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=capital,
            point_values={"TEST": point_value},
        )
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)
        indicators = strategy.compute_indicators(df.copy())

        closed = registry.get_closed_trades()
        open_trades = registry.get_open_trades()
        trades = closed + open_trades
        assert trades, "expected at least one entry"

        first_trade = min(trades, key=lambda t: t.entry_time)
        fill_row = indicators.loc[first_trade.entry_time]
        vol = float(fill_row["volatility"])
        fill_price = float(fill_row["open"])
        expected = math.floor(
            (target_pct / 100.0) * capital / (vol * fill_price * point_value)
        )
        assert first_trade.quantity == pytest.approx(float(expected), rel=0, abs=0)
