"""PricedOrderDeprecationWarning: once per run with same-candle priced orders, never otherwise (Q-139)."""

from __future__ import annotations

import warnings
from datetime import time

import pandas as pd

from q_backend.research import PricedOrderDeprecationWarning, ResearchStrategy, TradeOrder
from tests.research.test_resting_orders_backtest import _m10_frame, _run, _write_session

# Bars 10:00, 10:10 and 10:20 each reach 106, so a priced 105 entry on either of the later bars is in range.
ROWS = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:12:00", 106.0), ("10:20:00", 100.0), ("10:22:00", 106.0)]


class PricedEntries(ResearchStrategy):
    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if len(frame) in (2, 3) and not positions:
            return TradeOrder.buy(price=105.0)
        return None


class PricedClose(ResearchStrategy):
    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy() if len(frame) == 1 else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.close(price=float(frame.iloc[-1]["open"])) if positions and len(frame) == 3 else None


class LivePlaceable(ResearchStrategy):
    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy_stop(105.0, expire_bars=1) if len(frame) == 1 else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.close(at=time(10, 25)) if positions else None


def _deprecations(caught) -> list[warnings.WarningMessage]:
    return [w for w in caught if issubclass(w.category, PricedOrderDeprecationWarning)]


def _run_recording(strategy, store):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _run(_m10_frame(store), strategy, store)
    return result, _deprecations(caught)


def test_the_warning_is_a_future_warning_so_it_shows_by_default():
    assert issubclass(PricedOrderDeprecationWarning, FutureWarning)


def test_a_run_with_several_priced_entries_warns_once_naming_the_entry_hook(tmp_path):
    store = _write_session(tmp_path, ROWS)
    result, deprecations = _run_recording(PricedEntries(), store)
    assert "priced_entry" in result.order_kinds
    [warning] = deprecations
    message = str(warning.message)
    assert "entry_strategy" in message
    assert "exit_strategy" not in message
    assert "live_placeable_orders.rst" in message


def test_a_priced_close_warns_naming_the_exit_hook(tmp_path):
    store = _write_session(tmp_path, ROWS)
    result, deprecations = _run_recording(PricedClose(), store)
    assert "priced_close" in result.order_kinds
    [warning] = deprecations
    assert "exit_strategy" in str(warning.message)


def test_each_run_with_priced_orders_warns_once(tmp_path):
    store = _write_session(tmp_path, ROWS)
    _, first = _run_recording(PricedEntries(), store)
    _, second = _run_recording(PricedEntries(), store)
    assert len(first) == len(second) == 1


def test_runs_without_priced_orders_never_warn(tmp_path):
    store = _write_session(tmp_path, ROWS)
    result, deprecations = _run_recording(LivePlaceable(), store)
    assert deprecations == []
    assert not {"priced_entry", "priced_close"} & set(result.order_kinds)


def test_a_run_without_any_order_warns_nothing(tmp_path):
    class DoNothing(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            return None

    store = _write_session(tmp_path, ROWS)
    result, deprecations = _run_recording(DoNothing(), store)
    assert result.trades.empty
    assert deprecations == []
