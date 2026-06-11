from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.models import SignalAction
from q_backend.backtesting.position_sizing import FixedQuantitySizer


def _closes_frame(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(len(closes))]
    if opens is None:
        opens = closes.copy()
    return pd.DataFrame({"open": opens, "close": closes}, index=times)


def _signal_bar_indices(result: pd.DataFrame, column: str) -> list[int]:
    if "bar_index" in result.columns:
        return [int(result.loc[ts, "bar_index"]) for ts in result.index[result[column]]]
    return [result.index.get_loc(ts) for ts in result.index[result[column]]]


class TestVMAStrategy:
    def test_crossover_signals_on_expected_bars(self):
        strategy = build_strategy(
            "VMA", {"period": 3, "band_pct": 0.0, "ma_type": "sma"}, "TEST"
        )
        df = _closes_frame([100.0, 100.0, 100.0, 102.0, 97.0, 97.0, 97.0])
        result = strategy.compute_indicators(df)

        buy_bars = _signal_bar_indices(result, "buy_signal")
        sell_bars = _signal_bar_indices(result, "sell_signal")

        assert buy_bars == [3]
        assert sell_bars == [4]

    def test_band_suppresses_near_ma_cross(self):
        strategy = build_strategy(
            "VMA", {"period": 3, "band_pct": 1.0, "ma_type": "sma"}, "TEST"
        )
        df = _closes_frame([100.0, 100.0, 100.0, 100.5])
        result = strategy.compute_indicators(df)

        assert not result["buy_signal"].any()
        assert not result["sell_signal"].any()

    def test_zero_band_still_triggers_on_same_series(self):
        strategy = build_strategy(
            "VMA", {"period": 3, "band_pct": 0.0, "ma_type": "sma"}, "TEST"
        )
        df = _closes_frame([100.0, 100.0, 100.0, 100.5])
        result = strategy.compute_indicators(df)
        assert result["buy_signal"].iloc[3]


class TestFMAStrategy:
    def test_crossover_signals_on_expected_bars(self):
        strategy = build_strategy(
            "FMA",
            {
                "period": 3,
                "band_pct": 0.0,
                "ma_type": "sma",
                "holding_period": 10,
            },
            "TEST",
        )
        df = _closes_frame([100.0, 100.0, 100.0, 102.0, 97.0, 97.0, 97.0])
        result = strategy.compute_indicators(df)

        buy_bars = _signal_bar_indices(result, "buy_signal")
        sell_bars = _signal_bar_indices(result, "sell_signal")
        assert buy_bars == [3]
        assert sell_bars == [4]

    def test_fixed_holding_exit_via_engine(self):
        strategy = build_strategy(
            "FMA",
            {
                "period": 2,
                "band_pct": 0.0,
                "ma_type": "sma",
                "holding_period": 2,
            },
            "TEST",
        )
        closes = [10.0, 10.0, 10.0, 12.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        opens = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]
        df = _closes_frame(closes, opens)

        engine = BacktestEngine(strategy, FixedQuantitySizer(1.0), initial_capital=1000)
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

        closed = registry.get_closed_trades()
        assert len(closed) == 1
        trade = closed[0]
        # BUY signal at bar 3 -> entry at bar 4 open (12.0)
        assert trade.entry_time == df.index[4]
        assert trade.entry_price == 12.0
        # Holding exit at bar 6 -> fill at bar 7 open (15.0)
        assert trade.exit_time == df.index[7]
        assert trade.exit_price == 15.0

    def test_opposite_trigger_does_not_close_early(self):
        strategy = build_strategy(
            "FMA",
            {
                "period": 2,
                "band_pct": 0.0,
                "ma_type": "sma",
                "holding_period": 3,
            },
            "TEST",
        )
        closes = [10.0, 10.0, 10.0, 12.0, 8.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        opens = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]
        df = _closes_frame(closes, opens)
        result = strategy.compute_indicators(df)

        # Opposite sell trigger fires at bar 4 while position is still open.
        assert result["sell_signal"].iloc[4]

        engine = BacktestEngine(strategy, FixedQuantitySizer(1.0), initial_capital=1000)
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

        trade = registry.get_closed_trades()[0]
        # Entry bar 4; holding_period=3 -> exit evaluated bar 7, fill bar 8.
        assert trade.exit_time == df.index[8]
        assert trade.exit_price == 16.0


class TestTRBStrategy:
    def test_breakout_signals_on_expected_bars(self):
        strategy = build_strategy(
            "TRB",
            {"period": 3, "band_pct": 0.0, "holding_period": 10},
            "TEST",
        )
        df = _closes_frame([10.0, 11.0, 12.0, 13.0, 12.0, 8.0])
        result = strategy.compute_indicators(df)

        buy_bars = _signal_bar_indices(result, "buy_signal")
        sell_bars = _signal_bar_indices(result, "sell_signal")
        assert buy_bars == [3]
        assert sell_bars == [5]

    def test_current_close_excluded_from_channel(self):
        strategy = build_strategy(
            "TRB", {"period": 3, "band_pct": 0.0, "holding_period": 10}, "TEST"
        )
        df = _closes_frame([10.0, 11.0, 12.0, 13.0])
        result = strategy.compute_indicators(df)

        assert result.loc[df.index[3], "channel_high"] == pytest.approx(12.0)
        assert result.loc[df.index[3], "buy_signal"]

    def test_fixed_holding_exit_via_engine(self):
        strategy = build_strategy(
            "TRB",
            {"period": 2, "band_pct": 0.0, "holding_period": 2},
            "TEST",
        )
        closes = [10.0, 10.0, 10.0, 12.0, 10.0, 10.0, 10.0, 10.0]
        opens = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        df = _closes_frame(closes, opens)

        engine = BacktestEngine(strategy, FixedQuantitySizer(1.0), initial_capital=1000)
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

        trade = registry.get_closed_trades()[0]
        assert trade.entry_time == df.index[4]
        assert trade.entry_price == 12.0
        assert trade.exit_time == df.index[7]
        assert trade.exit_price == 15.0

    def test_opposite_trigger_does_not_close_early(self):
        strategy = build_strategy(
            "TRB",
            {"period": 2, "band_pct": 0.0, "holding_period": 3},
            "TEST",
        )
        closes = [10.0, 10.0, 10.0, 12.0, 8.0, 10.0, 10.0, 10.0, 10.0]
        opens = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
        df = _closes_frame(closes, opens)
        result = strategy.compute_indicators(df)

        assert result["sell_signal"].iloc[4]

        engine = BacktestEngine(strategy, FixedQuantitySizer(1.0), initial_capital=1000)
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

        trade = registry.get_closed_trades()[0]
        assert trade.exit_time == df.index[8]
        assert trade.exit_price == 16.0
