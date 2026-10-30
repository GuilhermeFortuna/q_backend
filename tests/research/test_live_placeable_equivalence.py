"""Live-placeable halves of the Q-139 migration fixtures."""

from datetime import time

import pandas as pd

from q_backend.research import ResearchStrategy, TradeOrder
from tests.research.test_resting_orders_backtest import _m10_frame, _run, _write_session


class Scripted(ResearchStrategy):
    def __init__(self, entries=None, exits=None, exit_fn=None):
        self.entries = entries or {}
        self.exits = exits or {}
        self.exit_fn = exit_fn

    def entry_strategy(self, frame, positions=()):
        return self.entries.get(len(frame) - 1)

    def exit_strategy(self, frame, positions=()):
        if self.exit_fn is not None:
            return self.exit_fn(frame, positions)
        return self.exits.get(len(frame) - 1) if positions else None


def _run_port(tmp_path, rows, port):
    store = _write_session(tmp_path, rows)
    return _run(_m10_frame(store), port, store)


def test_resting_stop_at_fixed_level_fills_on_next_bar(tmp_path):
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 106.0),
        ("10:14:00", 108.0),
        ("10:20:00", 107.0),
        ("10:22:00", 94.0),
    ]
    port = Scripted(
        entries={0: TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=120.0, expire_bars=1, reason="break")}
    )
    new = _run_port(tmp_path, rows, port)
    assert len(new.trades) == 1
    assert new.trades.iloc[0]["entry_price"] == 106.0


def test_oco_group_fills_first_reached_leg(tmp_path):
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:12:00", 106.0), ("10:14:00", 94.0), ("10:20:00", 100.0)]
    port = Scripted(
        entries={
            0: TradeOrder.oco(
                TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=120.0, expire_bars=1, reason="long"),
                TradeOrder.sell_stop(95.0, stop_loss=105.0, take_profit=80.0, expire_bars=1, reason="short"),
            )
        }
    )
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["entry_reason"] == "long"
    assert new.trades.iloc[0]["entry_price"] == 106.0


def test_amended_stop_fills_at_fixed_level(tmp_path):
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 100.0), ("10:22:00", 95.0), ("10:30:00", 99.0)]
    port = Scripted(
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.amend(stop_loss=95.0)},
    )
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["exit_price"] == 95.0
    assert new.trades.iloc[0]["exit_reason"] == "STOP_LOSS"


def test_timed_exit_at_bar_open(tmp_path):
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 101.0), ("10:24:00", 101.5), ("10:30:00", 102.0)]
    port = Scripted(entries={0: TradeOrder.buy()}, exits={1: TradeOrder.close(at=time(10, 20), reason="time up")})
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["exit_price"] == 101.0


def test_resting_stop_enters_on_exact_touch(tmp_path):
    rows = [("10:00:00", 100.0), ("10:00:30", 101.0), ("10:10:00", 100.0), ("10:12:00", 101.0), ("10:14:00", 99.0)]

    class PortBreakout(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
            if len(frame) == 1:
                return TradeOrder.buy_stop(float(frame.iloc[-1]["high"]), expire_bars=1, reason="breakout")
            return None

    new = _run_port(tmp_path, rows, PortBreakout())
    assert new.trades.iloc[0]["entry_price"] == 101.0
    assert new.trades.iloc[0]["entry_time"] == pd.Timestamp("2026-10-05 10:10", tz="America/Sao_Paulo")


def test_gap_entry_keeps_protective_levels_from_order(tmp_path):
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 107.0),
        ("10:12:00", 108.0),
        ("10:20:00", 104.0),
        ("10:21:00", 96.0),
        ("10:22:00", 94.0),
        ("10:24:00", 100.0),
        ("10:40:00", 100.0),
    ]
    port = Scripted(
        entries={
            0: TradeOrder.buy_stop(105.0, stop_loss=95.0, take_profit=115.0, expire_bars=1, reason="Bracket buy stop")
        },
        exits={4: TradeOrder.close(reason="x")},
    )
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["entry_price"] == 107.0
    assert new.trades.iloc[0]["exit_price"] == 94.0


def test_resting_reentry_fills_after_intrabar_exit(tmp_path):
    rows = [
        ("10:00:00", 108.0),
        ("10:10:00", 108.0),
        ("10:20:00", 103.0),
        ("10:22:00", 97.0),
        ("10:24:00", 102.0),
        ("10:40:00", 102.0),
    ]
    first = TradeOrder.buy(stop_loss=98.0, take_profit=120.0, reason="first")
    port = Scripted(
        entries={
            0: first,
            1: TradeOrder.buy_stop(101.0, stop_loss=91.0, take_profit=111.0, expire_bars=1, reason="again"),
        },
        exits={4: TradeOrder.close(reason="x")},
    )
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[1]["entry_price"] == 102.0
    assert new.trades.iloc[1]["entry_time"] == pd.Timestamp("2026-10-05 10:20", tz="America/Sao_Paulo")


def test_amended_stop_exits_on_exact_touch(tmp_path):
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 100.0),
        ("10:20:00", 100.0),
        ("10:22:00", 95.0),
        ("10:24:00", 99.0),
        ("10:30:00", 99.0),
        ("10:40:00", 99.0),
    ]
    port = Scripted(
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.amend(stop_loss=95.0), 3: TradeOrder.close(reason="time")},
    )
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["exit_price"] == 95.0


def test_amended_target_applies_after_entry_bar(tmp_path):
    rows = [
        ("10:00:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 104.0),
        ("10:20:00", 101.0),
        ("10:22:00", 101.0),
        ("10:30:00", 101.0),
        ("10:40:00", 101.0),
    ]
    port = Scripted(
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.amend(take_profit=103.0), 3: TradeOrder.close(reason="time")},
    )
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["exit_price"] == 101.0


def test_amended_stop_fills_when_reached(tmp_path):
    rows = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:20:00", 100.0), ("10:22:00", 97.0), ("10:24:00", 95.0)]
    port = Scripted(entries={0: TradeOrder.buy()}, exits={1: TradeOrder.amend(stop_loss=95.0)})
    new = _run_port(tmp_path, rows, port)
    assert new.trades.iloc[0]["exit_price"] == 95.0
