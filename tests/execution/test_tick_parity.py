"""Tick-replay parity between research backtests and the forward path (Q-137 acceptance 7).

Every case is the spec's parity list except the trailing stop, which is out of scope.
"""

from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder
from tests.execution.tick_parity import (
    research_fills,
    run_forward,
    run_research,
    tape_from_clocks,
    write_store,
)


class Scripted(ResearchStrategy):
    """Places each named order on the bar at its index, once; holds no other logic."""

    def __init__(self, entries=None, exits=None):
        self.entries = dict(entries or {})
        self.exits = dict(exits or {})

    def entry_strategy(self, frame, positions=()):
        return self.entries.get(len(frame) - 1)

    def exit_strategy(self, frame, positions=()):
        return self.exits.get(len(frame) - 1)


def _rounded(fills):
    return [(pd.Timestamp(when), side, round(price, 6)) for when, side, price in fills]


def _assert_parity(tmp_path, db_engine, rows, entries=None, exits=None, expected=None):
    tape = tape_from_clocks(rows)
    store = write_store(tmp_path / "ticks", tape)
    frame = store.bars("M10", start=tape[0][0].date().isoformat())
    research = _rounded(research_fills(run_research(store, Scripted(entries, exits), frame)))
    forward = _rounded(run_forward(db_engine, tape, frame, Scripted(entries, exits)))
    assert forward == research
    if expected is not None:
        assert research == [(pd.Timestamp(when), side, round(price, 6)) for when, side, price in expected]


def _at(clock: str, day: str = "2026-10-05"):
    return pd.Timestamp(f"{day} {clock}", tz="America/Sao_Paulo")


def test_entry_with_a_stop_and_a_target(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.5),
        ("10:12:00", 101.0),
        ("10:18:00", 96.0),
        ("10:20:00", 104.0),
        ("10:24:00", 106.0),
        ("10:28:00", 104.0),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy(stop_loss=95.0, take_profit=105.0)},
        expected=[(_at("10:10"), "buy", 100.5), (_at("10:24"), "sell", 105.0)],
    )


def test_an_amend_moves_the_stop_and_the_exit_follows_it(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.5),
        ("10:15:00", 101.0),
        ("10:19:00", 102.0),
        ("10:21:00", 99.5),
        ("10:23:00", 98.5),
        ("10:26:00", 103.0),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy(stop_loss=95.0, take_profit=110.0)},
        exits={1: TradeOrder.amend(stop_loss=99.0)},
        expected=[(_at("10:10"), "buy", 100.5), (_at("10:23"), "sell", 98.5)],
    )


def test_a_resting_stop_entry_fills_and_its_target_exits(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 101.0),
        ("10:15:00", 101.5),
        ("10:21:00", 101.0),
        ("10:24:00", 102.5),
        ("10:28:00", 116.0),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy_stop(102.0, stop_loss=95.0, take_profit=115.0, expire_bars=2)},
        expected=[(_at("10:24"), "buy", 102.5), (_at("10:28"), "sell", 115.0)],
    )


def test_a_resting_limit_entry_expires_without_a_fill(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 99.0),
        ("10:12:00", 99.5),
        ("10:18:00", 98.2),
        ("10:25:00", 97.0),
        ("10:28:00", 96.5),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy_limit(98.0, expire_bars=1)},
        expected=[],
    )


def test_a_cancel_removes_the_resting_entry(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 101.0),
        ("10:12:00", 101.5),
        ("10:21:00", 101.8),
        ("10:24:00", 103.0),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy_stop(102.0, expire_bars=None), 1: TradeOrder.cancel()},
        expected=[],
    )


def test_an_oco_bracket_where_the_second_member_fills_first(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:12:00", 99.0),
        ("10:15:00", 96.5),
        ("10:20:00", 95.0),
        ("10:25:00", 89.5),
        ("10:26:00", 102.5),
    ]
    oco = TradeOrder.oco(
        TradeOrder.buy_stop(102.0, stop_loss=98.0, take_profit=110.0, expire_bars=2),
        TradeOrder.sell_stop(97.0, stop_loss=101.0, take_profit=90.0, expire_bars=2),
    )
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: oco},
        expected=[(_at("10:15"), "sell", 96.5), (_at("10:25"), "buy", 90.0)],
    )


def test_a_resting_entry_expiring_by_expire_at(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.0),
        ("10:18:00", 103.0),
        ("10:21:00", 101.0),
        ("10:27:00", 106.0),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy_stop(105.0, expire_bars=None, expire_at=time(10, 25))},
        expected=[],
    )


def test_a_timed_exit_closes_at_the_first_trade_after_its_instant(tmp_path, db_engine):
    rows = [
        ("10:00:00", 100.0),
        ("10:05:00", 100.0),
        ("10:10:00", 100.5),
        ("10:15:00", 101.0),
        ("10:22:00", 102.0),
        ("10:29:00", 102.5),
        ("10:31:00", 103.5),
        ("10:35:00", 104.0),
    ]
    _assert_parity(
        tmp_path,
        db_engine,
        rows,
        entries={0: TradeOrder.buy()},
        exits={1: TradeOrder.close(at=time(10, 30))},
        expected=[(_at("10:10"), "buy", 100.5), (_at("10:31"), "sell", 103.5)],
    )
