"""Closing a same-bar priced entry at the next open, ahead of that bar's stops and targets."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from tests.research.test_priced_entries_after_exit import SYMBOL, _bar, _m10_frame, _write_session

# Bar 0 (10:00) fills a priced long at 100.0. Bar 1 (10:10) opens at 100.0 and trades through the
# 106.0 target at 10:15.
ROWS = [
    ("10:00:00", 100.0),
    ("10:05:00", 101.0),
    ("10:09:59", 99.0),
    ("10:10:00", 100.0),
    ("10:12:00", 103.0),
    ("10:15:00", 107.0),
    ("10:19:59", 104.0),
]


class HoldOneBar(ResearchStrategy):
    """Buys at 100.0 on the first bar and exits with ``exit_order`` once it holds a position."""

    def __init__(self, exit_order: TradeOrder | None) -> None:
        self.exit_order = exit_order

    def entry_strategy(self, frame: pd.DataFrame, positions) -> TradeOrder | None:
        if len(frame) == 1 and not positions:
            return TradeOrder.buy(price=100.0, take_profit=106.0)
        return None

    def exit_strategy(self, frame: pd.DataFrame, positions) -> TradeOrder | None:
        return self.exit_order if positions else None


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


def test_a_close_returned_after_the_fill_exits_at_the_next_open_before_the_target(root) -> None:
    store = _write_session(root, ROWS)
    frame = _m10_frame(store)

    held = backtest(frame, strategy=HoldOneBar(None), symbol=SYMBOL, ticks=store)
    assert held.trades["exit_reason"].tolist() == ["TAKE_PROFIT"]

    closed = backtest(frame, strategy=HoldOneBar(TradeOrder.close(reason="Hold time")), symbol=SYMBOL, ticks=store)
    assert closed.trades["entry_time"].tolist() == [_bar("10:00")]
    assert closed.trades["exit_time"].tolist() == [_bar("10:10")]
    assert closed.trades["exit_price"].tolist() == [100.0]
    assert closed.trades["exit_reason"].tolist() == ["Hold time"]


def test_a_priced_close_returned_after_the_fill_is_refused(root) -> None:
    store = _write_session(root, ROWS)
    frame = _m10_frame(store)

    with pytest.raises(ValueError, match="only TradeOrder.close\\(\\) without a price"):
        backtest(frame, strategy=HoldOneBar(TradeOrder.close(price=100.0)), symbol=SYMBOL, ticks=store)
