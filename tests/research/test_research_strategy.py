"""Tests for ResearchStrategy, TradeOrder, and ResearchStrategyAdapter."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

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
from q_backend.research import ChartIndicator, ResearchStrategy, TradeOrder
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


def test_trade_order_levels_are_validated_at_construction() -> None:
    order = TradeOrder.buy(stop_loss=9.0, take_profit=12.5)
    assert (order.stop_loss, order.take_profit) == (9.0, 12.5)
    assert TradeOrder.sell(stop_loss=12.0, take_profit=9.0).take_profit == 9.0
    assert TradeOrder.buy(take_profit=12.0).stop_loss is None
    assert TradeOrder.buy().stop_loss is None

    for bad in (0.0, -1.0, float("nan"), float("inf"), True, "9"):
        with pytest.raises(ValueError, match="stop_loss must be a finite positive price"):
            TradeOrder.buy(stop_loss=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="take_profit must be a finite positive price"):
            TradeOrder.sell(take_profit=bad)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="stop_loss must be below take_profit"):
        TradeOrder.buy(stop_loss=10.0, take_profit=10.0)
    with pytest.raises(ValueError, match="stop_loss must be above take_profit"):
        TradeOrder.sell(stop_loss=10.0, take_profit=10.0)
    with pytest.raises(ValueError, match="stop_loss must be above take_profit"):
        TradeOrder.sell(stop_loss=9.0, take_profit=10.0)

    with pytest.raises(TypeError):
        TradeOrder.close(stop_loss=9.0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="close orders carry no levels"):
        TradeOrder("close", stop_loss=9.0)
    for bad in (0.0, -1.0, float("nan"), float("inf"), True, "9"):
        with pytest.raises(ValueError, match="price must be a finite positive price"):
            TradeOrder.buy_stop(price=bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TradeOrder.buy(9.0)  # type: ignore[misc]


def _decide(hooks, bar: int) -> dict:
    """Decide one flat bar as the candle kernel does: exits, then entries."""
    return {**hooks.strategy(bar, ()), **hooks.entry(bar, ())}


def _first_bar_decisions(adapter: ResearchStrategyAdapter, bars: pd.DataFrame) -> dict:
    """Decide the first bar through the runtime callbacks, as the candle kernel does."""
    return _decide(adapter.runtime_callback(adapter.compute_indicators(bars)), 0)


def _all_bar_decisions(adapter: ResearchStrategyAdapter, bars: pd.DataFrame) -> list[dict]:
    hooks = adapter.runtime_callback(adapter.compute_indicators(bars))
    return [_decide(hooks, bar) for bar in range(len(bars))]


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
    hooks = adapter.runtime_callback(res)
    decisions = [_decide(hooks, bar) for bar in range(5)]

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

    # 4. Decisions by bar
    # Bar 1 (len=2): buy
    # Bar 2 (len=3): close
    # Bar 3 (len=4): sell
    assert [d.get("entry", 0) for d in decisions] == [0, 1, 0, -1, 0]
    assert [d.get("exit_long", False) for d in decisions] == [False, False, True, False, False]
    assert [d.get("exit_short", False) for d in decisions] == [False, False, True, False, False]
    assert [d.get("strength", 0.0) for d in decisions] == [0.0, 1.0, 0.0, 1.0, 0.0]
    assert "custom_ind" in res.columns
    assert "temp" in res.columns


def test_empty_frame_handling() -> None:
    class EmptyTestStrategy(ResearchStrategy):
        def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
            raise AssertionError("Should not be called for empty frame")

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


@pytest.mark.parametrize("column", ["tick_volume", "real_volume", "spread", "volume"])
@pytest.mark.parametrize("drop", [False, True])
def test_original_market_columns_preserved(sample_bars, column, drop):
    bars = sample_bars.assign(**{column: [100, 110, 120, 130, 140]})

    class CorruptMarket(ResearchStrategy):
        def compute_indicators(self, frame):
            if drop:
                return frame.drop(columns=[column])
            frame[column] = -123
            return frame

        def entry_strategy(self, frame):
            return None

    adapter = ResearchStrategyAdapter(CorruptMarket(), symbol="WIN$N")
    with pytest.raises(ValueError, match=column):
        adapter.compute_indicators(bars)


def test_empty_adapter_input_rejects_duplicate_columns(sample_bars):
    class NoEntry(ResearchStrategy):
        def entry_strategy(self, frame):
            return None

    empty = sample_bars.iloc[:0].assign(extra=0)
    empty = pd.concat([empty, empty[["extra"]]], axis=1)
    adapter = ResearchStrategyAdapter(NoEntry(), symbol="WIN$N")
    with pytest.raises(ValueError, match="duplicate column names"):
        adapter.compute_indicators(empty)


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
    with pytest.raises(TypeError, match="returned str, expected TradeOrder, a list of them, or None"):
        _first_bar_decisions(adapter, sample_bars)

    # 3. entry_strategy returns illegal action ('close')
    class IllegalEntryAction(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return TradeOrder.close()

    adapter = ResearchStrategyAdapter(IllegalEntryAction(), symbol="WIN$")
    with pytest.raises(ValueError, match="illegal action 'close'; permitted actions"):
        _first_bar_decisions(adapter, sample_bars)

    # 4. exit_strategy returns illegal action ('buy')
    class IllegalExitAction(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return None

        def exit_strategy(self, frame: pd.DataFrame):
            return TradeOrder.buy()

    adapter = ResearchStrategyAdapter(IllegalExitAction(), symbol="WIN$")
    with pytest.raises(ValueError, match="illegal action 'buy'; permitted actions"):
        _first_bar_decisions(adapter, sample_bars)


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
        _all_bar_decisions(adapter, sample_bars)
    assert isinstance(exc_info.value.__cause__, ZeroDivisionError)

    class FailingEntry(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            if len(frame) == 3:
                raise ValueError("math domain error in entry")
            return None

    adapter = ResearchStrategyAdapter(FailingEntry(), symbol="WIN$")
    with pytest.raises(RuntimeError, match=f"entry_strategy at bar {target_ts}") as exc_info:
        _all_bar_decisions(adapter, sample_bars)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_chart_indicator_defaults_label_to_column() -> None:
    indicator = ChartIndicator("short_ma")
    assert indicator.column == "short_ma"
    assert indicator.pane == "price"
    assert indicator.label == "short_ma"
    assert indicator.color is None
    assert indicator.line_style is None
    assert indicator.line_width is None


def test_chart_indicator_accepts_line_styling() -> None:
    indicator = ChartIndicator("rsi", color="#ff0000", line_style="dotted", line_width=2.5)
    assert (indicator.color, indicator.line_style, indicator.line_width) == ("#ff0000", "dotted", 2.5)


def test_chart_indicator_accepts_segment_kind_with_start_column() -> None:
    indicator = ChartIndicator("top", kind="segment", start_column="top_index", color="#26a69a")
    assert (indicator.kind, indicator.start_column, indicator.pane) == ("segment", "top_index", "price")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"kind": "segment"}, "requires start_column", id="segment without start_column"),
        pytest.param({"kind": "segment", "start_column": "i", "pane": "oscillator"}, "price pane", id="oscillator"),
        pytest.param({"start_column": "i"}, "do not apply to kind 'line'", id="start_column on a line"),
        pytest.param({"kind": "segment", "start_column": "i", "shape": "cross"}, "do not apply", id="shape"),
    ],
)
def test_chart_indicator_rejects_invalid_segment_declarations(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ChartIndicator("top", **kwargs)


def test_chart_indicator_accepts_study_plot_kinds() -> None:
    assert ChartIndicator("step", kind="step", fill_to="upper").kind == "step"
    assert ChartIndicator("entry", kind="markers", shape="triangle-up", position="below-bar").size == 8
    assert ChartIndicator("entry", kind="markers").position == "value"
    assert ChartIndicator("score", pane="oscillator", kind="histogram", negative_color="#f00").kind == "histogram"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "markers", "line_width": 2},
        {"kind": "markers", "shape": "hex"},
        {"kind": "markers", "size": 33},
        {"kind": "markers", "position": "above-bar", "pane": "oscillator"},
        {"kind": "histogram"},
        {"kind": "line", "baseline": 0},
        {"kind": "line", "fill_opacity": 0},
    ],
)
def test_chart_indicator_rejects_invalid_study_kind_fields(kwargs) -> None:
    with pytest.raises(ValueError):
        ChartIndicator("series", **kwargs)


def test_chart_indicator_rejects_unknown_line_style() -> None:
    with pytest.raises(ValueError, match="line_style"):
        ChartIndicator("rsi", line_style="wavy")  # type: ignore[arg-type]


@pytest.mark.parametrize("width", [0, -1, float("nan"), float("inf"), True])
def test_chart_indicator_rejects_invalid_line_width(width: float) -> None:
    with pytest.raises(ValueError, match="line_width"):
        ChartIndicator("rsi", line_width=width)


def test_chart_indicator_rejects_unknown_pane() -> None:
    with pytest.raises(ValueError, match="pane"):
        ChartIndicator("rsi", pane="volume")


def test_chart_indicator_is_immutable() -> None:
    indicator = ChartIndicator("rsi", pane="oscillator")
    with pytest.raises(FrozenInstanceError):
        indicator.pane = "price"  # type: ignore[misc]


def test_chart_indicators_hook_defaults_to_empty() -> None:
    class Plain(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame):
            return None

    assert tuple(Plain().chart_indicators()) == ()


def test_resting_order_constructors_set_kind_price_and_expiry() -> None:
    stop = TradeOrder.buy_stop(12.0, stop_loss=9.0, take_profit=15.0, expire_bars=2)
    assert (stop.action, stop.kind, stop.price, stop.expire_bars) == ("buy", "stop", 12.0, 2)
    assert (stop.stop_loss, stop.take_profit) == (9.0, 15.0)
    assert TradeOrder.buy_stop(12.0).expire_bars == 1
    assert TradeOrder.sell_stop(9.0, stop_loss=10.0).kind == "stop"
    assert TradeOrder.buy_limit(9.0, stop_loss=8.0).kind == "limit"
    assert TradeOrder.sell_limit(12.0, take_profit=10.0, expire_bars=None).expire_bars is None

    market = TradeOrder.buy()
    assert (market.kind, market.expire_bars) == ("market", None)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: TradeOrder("buy", kind="stop"), "stop order needs a price"),
        (lambda: TradeOrder("sell", kind="limit"), "limit order needs a price"),
        (lambda: TradeOrder("buy", kind="iceberg", price=10.0), "Invalid TradeOrder kind"),
        (lambda: TradeOrder("buy", expire_bars=1), "expire_bars applies to stop and limit orders only"),
        (lambda: TradeOrder.buy_stop(10.0, expire_bars=0), "expire_bars must be a positive integer"),
        (lambda: TradeOrder.buy_stop(10.0, expire_bars=-2), "expire_bars must be a positive integer"),
        (lambda: TradeOrder.buy_stop(10.0, expire_bars=1.5), "expire_bars must be a positive integer"),
        (lambda: TradeOrder.buy_stop(10.0, expire_bars=True), "expire_bars must be a positive integer"),
        (lambda: TradeOrder.buy_stop(10.0, stop_loss=10.0), "stop_loss must be below price for a buy"),
        (lambda: TradeOrder.sell_limit(10.0, take_profit=10.0), "take_profit must be below price for a sell"),
        (lambda: TradeOrder.buy_limit(float("nan")), "price must be a finite positive price"),
        (lambda: TradeOrder.amend(), "amend needs at least one of stop_loss or take_profit"),
        (lambda: TradeOrder.amend(stop_loss=-1.0), "stop_loss must be a finite positive price"),
        (lambda: TradeOrder("amend", price=10.0, stop_loss=9.0), "amend orders carry no price"),
        (lambda: TradeOrder("amend", kind="stop", stop_loss=9.0), "amend orders carry no order kind or expiry"),
        (lambda: TradeOrder("cancel", stop_loss=9.0), "cancel orders carry no levels"),
        (lambda: TradeOrder("cancel", price=10.0), "cancel orders carry no price"),
        (lambda: TradeOrder("close", expire_bars=1), "close orders carry no order kind or expiry"),
    ],
)
def test_resting_order_construction_errors(build, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_amend_and_cancel_are_immutable_management_orders() -> None:
    amend = TradeOrder.amend(take_profit=15.0)
    assert (amend.action, amend.stop_loss, amend.take_profit, amend.price) == ("amend", None, 15.0, None)
    assert TradeOrder.cancel().action == "cancel"
    with pytest.raises(AttributeError):
        amend.stop_loss = 9.0  # type: ignore[misc]


def test_hook_action_permissions_follow_hook_and_reject_removed_phase(sample_bars: pd.DataFrame) -> None:
    from q_backend.research.hooks import detect_hook_call, invoke_hook

    frame = sample_bars.iloc[:1]

    class EntryHook(ResearchStrategy):
        def __init__(self, order: TradeOrder) -> None:
            self.order = order

        def entry_strategy(self, frame: pd.DataFrame, positions=()) -> TradeOrder | None:
            return self.order

    def call(strategy: ResearchStrategy, name: str, **kwargs) -> TradeOrder | None:
        orders = invoke_hook(strategy, detect_hook_call(strategy, name), name, frame, (), **kwargs)
        assert len(orders) <= 1
        return orders[0] if orders else None

    assert call(EntryHook(TradeOrder.cancel()), "entry_strategy") is not None
    assert call(EntryHook(TradeOrder.buy_stop(12.0)), "entry_strategy").kind == "stop"
    with pytest.raises(ValueError, match="illegal action 'amend'"):
        call(EntryHook(TradeOrder.amend(stop_loss=9.0)), "entry_strategy")
    with pytest.raises(ValueError, match="illegal action 'close'"):
        call(EntryHook(TradeOrder.close()), "entry_strategy")

    class PlainExit(ResearchStrategy):
        def __init__(self, order: TradeOrder) -> None:
            self.order = order

        def entry_strategy(self, frame: pd.DataFrame, positions=()) -> TradeOrder | None:
            return None

        def exit_strategy(self, frame: pd.DataFrame, positions=()) -> TradeOrder | None:
            return self.order

    assert call(PlainExit(TradeOrder.amend(stop_loss=9.0)), "exit_strategy").action == "amend"
    with pytest.raises(ValueError, match="illegal action 'cancel'"):
        call(PlainExit(TradeOrder.cancel()), "exit_strategy")

    class OldExit(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions=()) -> TradeOrder | None:
            return None

        def exit_strategy(self, frame: pd.DataFrame, positions=(), *, phase: str = "bar") -> TradeOrder | None:
            return None

    with pytest.raises(TypeError, match="phase.*removed"):
        detect_hook_call(OldExit(), "exit_strategy")
