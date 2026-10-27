"""Candle prices for research backtests come from the native session tick source (Q-121)."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import TickStore, TradeOrder
from q_backend.research.errors import NoMarketDataError
from tests.research.test_stop_target_backtest import (
    FLAT_0,
    _m10_frame,
    _run,
    _stamp,
    _write_session,
)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


def test_candle_prices_come_from_the_native_source_not_the_store(root, monkeypatch):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)])
    frame = _m10_frame(store)

    from q_backend.research import engine as research_engine

    original_execute = research_engine._execute_backtest
    original_prices = TickStore.trade_prices

    def guarded_execute(*args, **kwargs):
        def python_price_read(self, start, end):
            raise AssertionError("TickStore.trade_prices must not be called during simulation")

        monkeypatch.setattr(TickStore, "trade_prices", python_price_read)
        try:
            return original_execute(*args, **kwargs)
        finally:
            monkeypatch.setattr(TickStore, "trade_prices", original_prices)

    monkeypatch.setattr(research_engine, "_execute_backtest", guarded_execute)
    result = _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)

    assert result.trades["exit_price"].tolist() == [94.0]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:12")]


def test_a_bar_whose_ticks_do_not_match_its_ohlc_names_the_bar_and_the_store(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)])
    frame = _m10_frame(store).copy()
    frame.iloc[1, frame.columns.get_loc("high")] = 150.0

    with pytest.raises(ValueError, match=r"Bar 1 at .* does not match its stored ticks.*TickStore\.bars") as exc_info:
        _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)}, store)
    assert not isinstance(exc_info.value, NoMarketDataError)


def test_a_missing_session_names_the_session_day_and_sync(root):
    store = _write_session(root, FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0)])
    frame = _m10_frame(store).copy()
    extra = frame.iloc[[1]].copy()
    extra.index = pd.DatetimeIndex([pd.Timestamp("2026-10-06 10:00", tz="America/Sao_Paulo")], name="time")
    extra.loc[:, ["open", "high", "low", "close"]] = [100.0, 100.0, 80.0, 85.0]
    frame = pd.concat([frame, extra])

    with pytest.raises(NoMarketDataError, match="2026-10-06") as exc_info:
        _run(frame, {0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0), 2: TradeOrder.buy(stop_loss=95.0)}, store)
    assert "TickStore.sync" in str(exc_info.value)
