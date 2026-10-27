"""Order lists: the bar's ticks decide which of a hook's priced orders fills."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from tests.research.test_priced_entries_after_exit import SYMBOL, _bar, _m10_frame, _write_session

# Bar 0 (10:00) is flat at 100.0. Bar 1 (10:10) opens at 100.0, trades down to 94.0, then up to
# 106.0 and closes at 103.0.
ROWS = [
    ("10:00:00", 100.0),
    ("10:05:00", 100.0),
    ("10:10:00", 100.0),
    ("10:12:00", 94.0),
    ("10:14:00", 106.0),
    ("10:19:59", 103.0),
]


class Scripted(ResearchStrategy):
    """Returns the scripted orders for each bar index, or calls them with the positions, and records
    the positions each hook saw."""

    def __init__(self, entries: dict, exits: dict) -> None:
        self.entries, self.exits = entries, exits
        self.seen: list[tuple[str, int, int]] = []

    def entry_strategy(self, frame: pd.DataFrame, positions):
        self.seen.append(("entry", len(frame) - 1, len(positions)))
        return self.entries.get(len(frame) - 1)

    def exit_strategy(self, frame: pd.DataFrame, positions):
        self.seen.append(("exit", len(frame) - 1, len(positions)))
        orders = self.exits.get(len(frame) - 1) if positions else None
        return orders(positions) if callable(orders) else orders


@pytest.fixture
def frame_and_store(tmp_path):
    store = _write_session(tmp_path / "ticks", ROWS)
    return _m10_frame(store), store


@pytest.mark.parametrize("reverse", [False, True])
def test_the_close_the_tape_reaches_first_wins_whatever_its_place_in_the_list(frame_and_store, reverse) -> None:
    frame, store = frame_and_store
    closes = [TradeOrder.close(price=95.0, reason="Stop"), TradeOrder.close(price=105.0, reason="Target")]
    strategy = Scripted({0: TradeOrder.buy()}, {1: closes[::-1] if reverse else closes})

    trades = backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store).trades

    # The stop at 95 is a jump to 94, its fill.
    assert trades["exit_price"].tolist() == [94.0]
    assert trades["exit_reason"].tolist() == ["Stop"]
    assert trades["exit_tick_time"].tolist() == [_bar("10:12")]


def test_the_entry_the_tape_touches_first_fills_and_cancels_the_other(frame_and_store) -> None:
    frame, store = frame_and_store
    bracket = [
        TradeOrder.buy(price=105.0, take_profit=106.0, reason="Buy stop"),
        TradeOrder.sell(price=95.0, stop_loss=110.0, reason="Sell stop"),
    ]
    strategy = Scripted({1: bracket}, {})

    trades = backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store).trades

    assert trades["side"].tolist() == ["short"]
    # The sell stop at 95 fills at the jump to 94.
    assert trades["entry_price"].tolist() == [94.0]
    assert trades["entry_reason"].tolist() == ["Sell stop"]
    assert trades["stop_loss"].tolist() == [110.0]


def test_an_entry_after_a_same_bar_close_sees_the_position_gone(frame_and_store) -> None:
    frame, store = frame_and_store
    strategy = Scripted(
        {0: TradeOrder.buy(reason="Open"), 1: [TradeOrder.sell(price=100.0, reason="Reverse")]},
        # Only the long is stopped; the engine asks again once the short fills, and it is kept.
        {1: lambda positions: [TradeOrder.close(price=95.0, reason="Stop")] if positions[0].side == "long" else None},
    )

    trades = backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store).trades

    assert ("exit", 1, 1) in strategy.seen and ("entry", 1, 0) in strategy.seen
    assert trades["side"].tolist() == ["long", "short"]
    assert trades["exit_reason"].tolist()[0] == "Stop"
    # The short at 100 is a limit above the 94 that stopped the long; the 106 trades beyond it.
    assert trades["entry_price"].tolist() == [100.0, 100.0]
    assert trades["entry_reason"].tolist() == ["Open", "Reverse"]


@pytest.mark.parametrize(
    "hook,orders,message",
    [
        ("exit_strategy", [TradeOrder.close(), TradeOrder.close()], "at most one close without a price"),
        ("exit_strategy", [TradeOrder.amend(stop_loss=90.0), TradeOrder.amend(stop_loss=91.0)], "at most one amend"),
        ("entry_strategy", [TradeOrder.buy(price=100.0), TradeOrder.buy_stop(105.0)], "only priced market entries"),
        ("entry_strategy", [TradeOrder.buy(), TradeOrder.sell()], "only priced market entries"),
    ],
)
def test_lists_the_engine_cannot_resolve_are_refused(frame_and_store, hook, orders, message) -> None:
    frame, store = frame_and_store
    entries = {0: TradeOrder.buy()} if hook == "exit_strategy" else {1: orders}
    exits = {1: orders} if hook == "exit_strategy" else {}

    with pytest.raises(ValueError, match=message):
        backtest(frame, strategy=Scripted(entries, exits), symbol=SYMBOL, ticks=store)
