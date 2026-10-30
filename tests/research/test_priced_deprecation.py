"""Compatibility of the historical priced-order warning."""

from __future__ import annotations

import warnings
from datetime import time

import pandas as pd

from q_backend.research import PricedOrderDeprecationWarning, ResearchStrategy, TradeOrder
from tests.research.test_resting_orders_backtest import _m10_frame, _run, _write_session

# The resting entry reaches 105 on the next bar.
ROWS = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:12:00", 106.0), ("10:20:00", 100.0), ("10:22:00", 106.0)]


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
