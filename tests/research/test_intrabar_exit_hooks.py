"""Priced close decisions execute against the current candle's stored trades (Q-128)."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research.hooks import detect_hook_call
from tests.research.test_stop_target_backtest import (
    _bar,
    _m10_frame,
    _stamp,
    _write_session,
)

SYMBOL = "WDO$N"
ENTRY = [("10:00:00", 100.0), ("10:05:00", 100.0), ("10:10:00", 100.0), ("10:15:00", 100.0)]
MOVE = [("10:20:00", 101.0), ("10:21:00", 104.0), ("10:22:00", 105.5), ("10:23:00", 103.0)]
NEXT = [("10:30:00", 120.0), ("10:31:00", 121.0)]


class EnterOnce(ResearchStrategy):
    def entry_strategy(self, frame, positions=()):
        return TradeOrder.buy() if len(frame) == 1 else None


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


def _run(root, strategy, rows=ENTRY + MOVE + NEXT, **kwargs):
    store = _write_session(root, rows)
    return backtest(_m10_frame(store), strategy=strategy, symbol=SYMBOL, ticks=store, **kwargs)


def test_phase_parameter_is_rejected_with_its_name():
    class OldExit(EnterOnce):
        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            return None

    with pytest.raises(TypeError, match="phase.*removed"):
        detect_hook_call(OldExit(), "exit_strategy")


def test_exit_with_ticks_runs_once_per_closed_bar_at_bar_end(root):
    class Recorded(EnterOnce):
        def __init__(self):
            self.calls = []

        def exit_strategy(self, frame, positions=(), *, ticks=None):
            self.calls.append((frame.index[-1], ticks.cutoff))
            return None

    strategy = Recorded()
    _run(root, strategy)
    assert [moment for moment, _ in strategy.calls] == [_bar("10:00"), _bar("10:10"), _bar("10:20"), _bar("10:30")]
    assert all(cutoff > moment for moment, cutoff in strategy.calls)


def test_a_short_close_above_the_market_is_a_stop_that_fills_at_the_trade_reaching_it(root):
    class Priced(EnterOnce):
        def entry_strategy(self, frame, positions=()):
            return TradeOrder.sell() if len(frame) == 1 else None

        def exit_strategy(self, frame, positions=(), *, ticks=None):
            if positions and frame.index[-1] == _bar("10:20"):
                return TradeOrder.close(price=105.0)
            return None

    trade = _run(root, Priced()).trades.iloc[0]
    # The tape jumps from 104 to 105.5 through the 105 stop.
    assert trade["exit_price"] == 105.5
    assert trade["exit_tick_time"] == _stamp("10:22")


def test_a_long_close_above_the_market_is_a_limit_that_fills_at_its_price_once_traded_beyond(root):
    class Target(EnterOnce):
        def exit_strategy(self, frame, positions=(), *, ticks=None):
            if positions and frame.index[-1] == _bar("10:20"):
                return TradeOrder.close(price=104.0)
            return None

    trade = _run(root, Target()).trades.iloc[0]
    # 104 trades at 10:21, but only the 105.5 at 10:22 goes beyond it.
    assert trade["exit_price"] == 104.0
    assert trade["exit_tick_time"] == _stamp("10:22")


def test_priced_close_outside_bar_range_names_bar_and_price(root):
    class Invalid(EnterOnce):
        def exit_strategy(self, frame, positions=(), *, ticks=None):
            if positions and frame.index[-1] == _bar("10:20"):
                return TradeOrder.close(price=110.0)
            return None

    with pytest.raises(ValueError, match=r"exit price 110 at bar 2 \(.*10:20"):
        _run(root, Invalid())


def test_priced_close_requires_ticks(root):
    class Priced(EnterOnce):
        def exit_strategy(self, frame, positions=()):
            return TradeOrder.close(price=105.0) if positions else None

    store = _write_session(root, ENTRY + MOVE + NEXT)
    with pytest.raises(ValueError, match=r"Priced.exit_strategy.*priced close.*ticks="):
        backtest(_m10_frame(store), strategy=Priced(), symbol=SYMBOL)


def test_protective_stop_that_fills_first_wins_over_priced_close(root):
    class Protected(EnterOnce):
        def entry_strategy(self, frame, positions=()):
            return TradeOrder.buy(stop_loss=95.0) if len(frame) == 1 else None

        def exit_strategy(self, frame, positions=(), *, ticks=None):
            return TradeOrder.close(price=105.0) if frame.index[-1] == _bar("10:20") else None

    rows = ENTRY + [("10:20:00", 101.0), ("10:21:00", 94.0), ("10:22:00", 105.5), ("10:23:00", 103.0)] + NEXT
    trade = _run(root, Protected(), rows=rows).trades.iloc[0]
    assert trade["exit_price"] == 94.0
    assert trade["exit_reason"] == "STOP_LOSS"


def test_unreached_priced_close_keeps_position_open(root):
    class Unreached(ResearchStrategy):
        def entry_strategy(self, frame, positions=()):
            return TradeOrder.buy_stop(105.5) if len(frame) == 2 else None

        def exit_strategy(self, frame, positions=(), *, ticks=None):
            return TradeOrder.close(price=104.0) if positions and frame.index[-1] == _bar("10:20") else None

    rows = ENTRY + [("10:20:00", 101.0), ("10:21:00", 105.0), ("10:22:00", 106.0)] + NEXT
    trade = _run(root, Unreached(), rows=rows).trades.iloc[0]
    assert trade["status"] == "open"


def test_unpriced_close_still_fills_at_the_next_open(root):
    class Plain(EnterOnce):
        def exit_strategy(self, frame, positions=()):
            return TradeOrder.close() if positions and frame.index[-1] == _bar("10:20") else None

    trade = _run(root, Plain()).trades.iloc[0]
    assert trade["exit_price"] == 120.0
    assert pd.isna(trade["exit_tick_time"])
