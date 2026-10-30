"""Invalid order lists are refused."""

from __future__ import annotations

import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from tests.research.test_priced_entries_after_exit import SYMBOL, _m10_frame, _write_session

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


@pytest.mark.parametrize(
    "hook,orders,message",
    [
        ("exit_strategy", [TradeOrder.close(), TradeOrder.close()], "at most one close without a price"),
        ("exit_strategy", [TradeOrder.amend(stop_loss=90.0), TradeOrder.amend(stop_loss=91.0)], "at most one amend"),
        ("entry_strategy", [TradeOrder.buy(), TradeOrder.sell()], "use TradeOrder.oco"),
    ],
)
def test_lists_the_engine_cannot_resolve_are_refused(frame_and_store, hook, orders, message) -> None:
    frame, store = frame_and_store
    entries = {0: TradeOrder.buy()} if hook == "exit_strategy" else {1: orders}
    exits = {1: orders} if hook == "exit_strategy" else {}

    with pytest.raises(ValueError, match=message):
        backtest(frame, strategy=Scripted(entries, exits), symbol=SYMBOL, ticks=store)
