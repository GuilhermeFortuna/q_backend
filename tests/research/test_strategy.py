"""Tests for ResearchStrategy, TradeOrder, and ResearchStrategyAdapter."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.signal_columns import (
    SIGNAL_ENTRY,
    SIGNAL_EXIT_LONG,
    SIGNAL_EXIT_SHORT,
    SIGNAL_STRENGTH,
)
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy, TradeOrder
from q_backend.research.adapter import ResearchStrategyAdapter


@pytest.fixture
def sample_bars() -> pd.DataFrame:
    idx = pd.date_range("2026-09-01 09:00", periods=5, freq="5min", tz=BRASILIA_TZ, name="time")
    return pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0, 11.0, 10.0],
            "high": [12.0, 13.0, 14.0, 13.0, 12.0],
            "low": [9.0, 10.0, 11.0, 10.0, 9.0],
            "close": [11.0, 12.0, 11.0, 10.0, 11.0],
            "tick_volume": [100, 110, 120, 130, 140],
        },
        index=idx,
    )


def test_trade_order_immutability_and_validation() -> None:
    order = TradeOrder.buy()
    assert order.action == "buy"
    order_sell = TradeOrder.sell()
    assert order_sell.action == "sell"
    order_close = TradeOrder.close()
    assert order_close.action == "close"

    with pytest.raises(AttributeError):
        order.action = "sell"  # type: ignore[misc]

    with pytest.raises(ValueError, match="Invalid TradeOrder action"):
        TradeOrder("hold")  # type: ignore[arg-type]


def test_scripted_strategy_lifecycle_and_isolation(sample_bars: pd.DataFrame) -> None:
    calls = []

    class MockStrategy(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
            calls.append(("compute_indicators", len(frame)))
            frame = frame.copy()
            frame["custom_ind"] = frame["close"] * 2
            # Mutating a local frame column should not leak or fail
            frame["temp"] = 123
            return frame

        def exit_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            calls.append(("exit_strategy", len(frame), frame.index[-1]))
            # Verify no future range attrs
            assert "q_research" not in frame.attrs
            # Mutating frame in hook should not affect anything
            frame["temp"] = 999
            if len(frame) == 3:
                return TradeOrder.close()
            return None

        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            calls.append(("entry_strategy", len(frame), frame.index[-1]))
            assert "q_research" not in frame.attrs
            if len(frame) == 2:
                return TradeOrder.buy()
            if len(frame) == 4:
                return TradeOrder.sell()
            return None

    adapter = ResearchStrategyAdapter(MockStrategy(), symbol="WIN$")
    caller_bars = sample_bars.copy()
    caller_bars.attrs["q_research"] = {"dummy": "metadata"}

    res = adapter.compute_indicators(caller_bars)

    # 1. compute_indicators called once on full frame
    assert calls[0] == ("compute_indicators", 5)

    # 2. For each bar (1 to 5), exit_strategy then entry_strategy called
    expected_calls = [("compute_indicators", 5)]
    for i in range(1, 6):
        expected_calls.append(("exit_strategy", i, sample_bars.index[i - 1]))
        expected_calls.append(("entry_strategy", i, sample_bars.index[i - 1]))
    assert calls == expected_calls

    # 3. Caller frame unchanged
    assert "custom_ind" not in caller_bars.columns
    assert "temp" not in caller_bars.columns
    assert caller_bars.attrs["q_research"] == {"dummy": "metadata"}
    pd.testing.assert_frame_equal(caller_bars[["open", "high", "low", "close", "tick_volume"]], sample_bars)

    # 4. Result signal columns
    # Bar 1 (len=2): buy
    # Bar 2 (len=3): close
    # Bar 3 (len=4): sell
    np.testing.assert_array_equal(res[SIGNAL_ENTRY], [0, 1, 0, -1, 0])
    np.testing.assert_array_equal(res[SIGNAL_EXIT_LONG], [False, False, True, False, False])
    np.testing.assert_array_equal(res[SIGNAL_EXIT_SHORT], [False, False, True, False, False])
    np.testing.assert_array_equal(res[SIGNAL_STRENGTH], [0.0, 1.0, 0.0, 1.0, 0.0])
    assert "custom_ind" in res.columns
    assert "temp" in res.columns


def test_empty_frame_handling() -> None:
    class EmptyTestStrategy(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            raise AssertionError("Should not be called for empty frame")

    adapter = ResearchStrategyAdapter(EmptyTestStrategy(), symbol="WIN$")
    empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume"])
    empty_df.index = pd.DatetimeIndex([], tz=BRASILIA_TZ)

    res = adapter.compute_indicators(empty_df)
    assert len(res) == 0
    assert SIGNAL_ENTRY in res.columns
    assert SIGNAL_EXIT_LONG in res.columns
    assert SIGNAL_EXIT_SHORT in res.columns
    assert SIGNAL_STRENGTH in res.columns


def test_invalid_return_and_action_types(sample_bars: pd.DataFrame) -> None:
    # 1. compute_indicators returns non-DataFrame
    class BadIndicatorReturn(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame):
            return "not a dataframe"

        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            return None

    adapter = ResearchStrategyAdapter(BadIndicatorReturn(), symbol="WIN$")
    with pytest.raises(TypeError, match="must return a DataFrame"):
        adapter.compute_indicators(sample_bars)

    # 2. entry_strategy returns wrong type
    class BadEntryType(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return "buy"  # string instead of TradeOrder

    adapter = ResearchStrategyAdapter(BadEntryType(), symbol="WIN$")
    with pytest.raises(TypeError, match="returned str, expected TradeOrder or None"):
        adapter.compute_indicators(sample_bars)

    # 3. entry_strategy returns illegal action ('close')
    class IllegalEntryAction(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return TradeOrder.close()

    adapter = ResearchStrategyAdapter(IllegalEntryAction(), symbol="WIN$")
    with pytest.raises(ValueError, match="only 'buy' or 'sell' is permitted"):
        adapter.compute_indicators(sample_bars)

    # 4. exit_strategy returns illegal action ('buy')
    class IllegalExitAction(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return None

        def exit_strategy(self, frame: pd.DataFrame):
            return TradeOrder.buy()

    adapter = ResearchStrategyAdapter(IllegalExitAction(), symbol="WIN$")
    with pytest.raises(ValueError, match="only 'close' is permitted"):
        adapter.compute_indicators(sample_bars)


def test_reserved_columns_and_tampered_market_data(sample_bars: pd.DataFrame) -> None:
    # Input has reserved column
    bad_input = sample_bars.copy()
    bad_input["q_signal_entry"] = 0

    class Minimal(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return None

    adapter = ResearchStrategyAdapter(Minimal(), symbol="WIN$")
    with pytest.raises(ValueError, match="reserved column 'q_signal_entry'"):
        adapter.compute_indicators(bad_input)

    # compute_indicators adds reserved column
    class AddReserved(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
            frame = frame.copy()
            frame["bar_index"] = 0
            return frame

        def entry_strategy(self, frame: pd.DataFrame):
            return None

    adapter = ResearchStrategyAdapter(AddReserved(), symbol="WIN$")
    with pytest.raises(ValueError, match="returned reserved column 'bar_index'"):
        adapter.compute_indicators(sample_bars)

    # compute_indicators modifies row count
    class ModRow(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
            return frame.iloc[:-1].copy()

        def entry_strategy(self, frame: pd.DataFrame):
            return None

    adapter = ResearchStrategyAdapter(ModRow(), symbol="WIN$")
    with pytest.raises(ValueError, match="modified row count or index"):
        adapter.compute_indicators(sample_bars)

    # compute_indicators modifies market column values
    class MutateMarket(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
            frame = frame.copy()
            frame["close"] = frame["close"] + 10.0
            return frame

        def entry_strategy(self, frame: pd.DataFrame):
            return None

    adapter = ResearchStrategyAdapter(MutateMarket(), symbol="WIN$")
    with pytest.raises(ValueError, match="Market column 'close' was modified"):
        adapter.compute_indicators(sample_bars)


def test_hook_exceptions_preserve_cause_and_timestamp(sample_bars: pd.DataFrame) -> None:
    target_ts = sample_bars.index[2]

    class FailingExit(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return None

        def exit_strategy(self, frame: pd.DataFrame):
            if len(frame) == 3:
                raise ZeroDivisionError("division by zero in exit")
            return None

    adapter = ResearchStrategyAdapter(FailingExit(), symbol="WIN$")
    with pytest.raises(RuntimeError, match=f"exit_strategy at bar {target_ts}") as exc_info:
        adapter.compute_indicators(sample_bars)
    assert isinstance(exc_info.value.__cause__, ZeroDivisionError)

    class FailingEntry(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            if len(frame) == 3:
                raise ValueError("math domain error in entry")
            return None

    adapter = ResearchStrategyAdapter(FailingEntry(), symbol="WIN$")
    with pytest.raises(RuntimeError, match=f"entry_strategy at bar {target_ts}") as exc_info:
        adapter.compute_indicators(sample_bars)
    assert isinstance(exc_info.value.__cause__, ValueError)
