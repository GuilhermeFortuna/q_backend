"""Day-trade sessions run in worker processes and join to the sequential result (Q-121)."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

import q_backend.research.engine as research_engine
from examples.research.tick_backtest_benchmark import FIRST_DAY, SYMBOL, write_sessions
from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research.session_blocks import session_blocks

SESSIONS = 6
COMPARED = [
    "side",
    "entry_time",
    "entry_price",
    "order_kind",
    "stop_loss",
    "take_profit",
    "final_stop_loss",
    "exit_time",
    "exit_tick_time",
    "exit_price",
    "exit_reason",
    "pnl",
]


class Mixed(ResearchStrategy):
    """Market and resting buy stops, stops amended to each closed bar's low, and a phase-aware exit."""

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if positions:
            return None
        last = frame.iloc[-1]
        step = len(frame) % 5
        if step == 0:
            return TradeOrder.buy(stop_loss=float(last["close"]) - 4.0)
        if step == 2:
            return TradeOrder.buy_stop(float(last["high"]) + 0.5, stop_loss=float(last["low"]) - 2.0, expire_bars=2)
        return None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        if not positions:
            return None
        if phase == "bar":
            low = float(frame["low"].iloc[-1])
            current = positions[0].stop_loss
            return TradeOrder.amend(stop_loss=low) if current is None or low > current else None
        floor = frame["low"].iloc[-6:-1].min()
        reached = frame["low"].iloc[-1] <= floor if phase == "screen" else frame["close"].iloc[-1] <= floor
        return TradeOrder.close() if reached else None


class FailingOnTicks(Mixed):
    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        if phase == "tick":
            raise KeyError("feed")
        return super().exit_strategy(frame, positions, phase=phase)


@pytest.fixture
def bars_and_store(tmp_path):
    store = write_sessions(tmp_path / "ticks", SESSIONS)
    end = (FIRST_DAY + timedelta(days=SESSIONS - 1)).isoformat()
    return store.bars("M10", start=FIRST_DAY.isoformat(), end=end), store


def _run(bars, store, strategy, **kwargs):
    return backtest(bars, strategy=strategy, symbol=SYMBOL, ticks=store, day_trade=True, **kwargs)


def test_sessions_joined_from_worker_blocks_match_the_sequential_run(bars_and_store):
    bars, store = bars_and_store
    sequential = _run(bars, store, Mixed())

    kinds = set(sequential.trades["order_kind"])
    assert {"market", "stop"} <= kinds
    assert sequential.trades["exit_tick_time"].notna().any()
    assert (sequential.trades["final_stop_loss"] != sequential.trades["stop_loss"]).any()
    for workers in (2, 4):
        pooled = _run(bars, store, Mixed(), workers=workers)
        pd.testing.assert_frame_equal(pooled.trades[COMPARED], sequential.trades[COMPARED])
        pd.testing.assert_frame_equal(pooled.rejected_entries, sequential.rejected_entries)
        pd.testing.assert_frame_equal(pooled.equity, sequential.equity)
        pd.testing.assert_frame_equal(pooled.data, sequential.data)
        assert pooled.metrics == sequential.metrics


def test_without_day_trade_the_run_is_not_split(bars_and_store, monkeypatch):
    bars, store = bars_and_store

    def no_split(*args, **kwargs):
        raise AssertionError("a run that is not day-trade must stay in one process")

    monkeypatch.setattr(research_engine, "run_session_blocks", no_split)
    sequential = backtest(bars, strategy=Mixed(), symbol=SYMBOL, ticks=store)
    requested = backtest(bars, strategy=Mixed(), symbol=SYMBOL, ticks=store, workers=4)

    pd.testing.assert_frame_equal(requested.trades[COMPARED], sequential.trades[COMPARED])


def test_a_tick_hook_failure_in_a_session_names_the_phase_and_bar(bars_and_store):
    bars, store = bars_and_store

    with pytest.raises(RuntimeError, match="phase tick") as exc_info:
        _run(bars, store, FailingOnTicks(), workers=2)
    assert "2026-01-05" in str(exc_info.value)


def test_a_strategy_that_cannot_be_pickled_is_rejected_for_session_workers(bars_and_store):
    bars, store = bars_and_store

    class Local(Mixed):
        pass

    with pytest.raises(ValueError, match="workers=1") as exc_info:
        _run(bars, store, Local(), workers=2)
    assert "Local" in str(exc_info.value)


def test_progress_reports_the_bars_of_every_session(bars_and_store, capsys):
    bars, store = bars_and_store

    _run(bars, store, Mixed(), workers=2, progress=True)

    assert f"{len(bars)} bars" in capsys.readouterr().err


def test_session_blocks_are_contiguous_whole_sessions():
    index = pd.DatetimeIndex(
        [f"2026-01-{day:02d} {hour:02d}:00" for day in (5, 6, 7, 8, 9) for hour in (9, 10, 11)],
        tz="America/Sao_Paulo",
    )

    blocks = session_blocks(index, 4)

    assert blocks == [(0, 6), (6, 9), (9, 12), (12, 15)]
    assert session_blocks(index, 1) is None
    assert session_blocks(index[:3], 4) is None
    assert session_blocks(index, 9) == [(0, 3), (3, 6), (6, 9), (9, 12), (12, 15)]
