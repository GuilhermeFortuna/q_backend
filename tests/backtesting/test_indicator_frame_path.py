"""Tests for the unified indicator augmentation path in engine and execution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategy import TradingStrategy
from tests.fixtures.indicators.export_pandas_baseline import synthetic_ohlcv


class DummyStrategy(TradingStrategy):
    def __init__(self):
        # Do not initialize exit_strategy; absent by default
        self.parameters = {}
        self.symbol = "DUMMY"

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        df["dummy_ind"] = 1.0
        return write_signal_columns(
            df,
            entry_long=pd.Series(False, index=df.index, dtype=bool),
            entry_short=pd.Series(False, index=df.index, dtype=bool),
            exit_long=False,
            exit_short=False,
            strategy_name=type(self).__name__,
        )

    def get_chart_indicators(self):
        return []


def test_augment_indicator_frame_same_object():
    from q_backend.backtesting.indicator_frame import augment_indicator_frame as bt_aug
    from q_backend.execution.indicator_frame import augment_indicator_frame as exec_aug

    assert exec_aug is bt_aug, "execution.indicator_frame must re-export backtesting.indicator_frame"


def test_sequential_run_calls_augment_indicator_frame_once():
    import q_backend.backtesting.engine as engine_mod

    df = synthetic_ohlcv(50, seed=20240609)
    strategy = DummyStrategy()
    engine = BacktestEngine(strategy, FixedQuantitySizer(quantity=1.0), initial_capital=10_000)

    mock_aug = MagicMock(wraps=augment_indicator_frame)
    with patch.object(engine_mod, "augment_indicator_frame", mock_aug):
        engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

    assert mock_aug.call_count == 1, f"Expected exactly 1 call in SEQUENTIAL mode, got {mock_aug.call_count}"


def test_day_trade_run_calls_augment_indicator_frame_per_day():
    import q_backend.backtesting.engine as engine_mod

    df = synthetic_ohlcv(72, freq="h", seed=20240609)  # 3 distinct days
    distinct_dates = len(set(df.index.date))
    assert distinct_dates >= 3

    strategy = DummyStrategy()
    engine = BacktestEngine(
        strategy,
        FixedQuantitySizer(quantity=1.0),
        initial_capital=10_000,
        day_trade=True,
    )

    mock_aug = MagicMock(wraps=augment_indicator_frame)
    with patch.object(engine_mod, "augment_indicator_frame", mock_aug):
        engine.run(df, parallel_mode=ParallelMode.DAY_TRADE)

    assert (
        mock_aug.call_count == distinct_dates
    ), f"Expected {distinct_dates} calls in DAY_TRADE mode, got {mock_aug.call_count}"


def test_strategy_with_exit_strategy_none_or_absent():
    df = synthetic_ohlcv(50, seed=20240609)
    strategy = DummyStrategy()

    # 1. When exit_strategy is explicitly None, augment_indicator_frame returns
    # strategy's own indicators and nothing else, without AttributeError.
    strategy.exit_strategy = None
    augmented = augment_indicator_frame(strategy, df)
    assert "dummy_ind" in augmented.columns
    assert not any(c.startswith("atr_") for c in augmented.columns)
    assert not any(c.startswith("donchian_") for c in augmented.columns)

    # 2. When exit_strategy is absent, the strategy runs through the engine without AttributeError.
    del strategy.exit_strategy
    engine = BacktestEngine(strategy, FixedQuantitySizer(quantity=1.0), initial_capital=10_000)
    registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)
    assert registry is not None
