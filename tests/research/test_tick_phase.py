"""Tick-phase decisions: skipped repeats, worker processes and progress output."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research import tick_phase
from q_backend.research.tick_phase import resolve_workers
from tests.research.test_intrabar_exit_hooks import ENTRY_BAR, NEXT_OPEN, Breakout
from tests.research.test_stop_target_backtest import SYMBOL, _bar, _m10_frame, _stamp, _write_session

REPEATS = [
    ("10:20:00", 101.0),
    ("10:20:10", 101.0),
    ("10:20:20", 104.0),
    ("10:20:30", 104.0),
    ("10:20:40", 104.0),
    ("10:20:50", 105.5),
    ("10:21:00", 105.5),
    ("10:21:10", 103.0),
]
COMPARED = ["side", "entry_time", "entry_price", "exit_time", "exit_tick_time", "exit_price", "exit_reason", "pnl"]


class FailingOnTicks(Breakout):
    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        if phase == "tick":
            raise KeyError("feed")
        return super().exit_strategy(frame, positions, phase=phase)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


def test_trade_prices_keep_positive_prices_inside_the_half_open_interval(root):
    rows = [
        ("10:19:59", 99.0),
        ("10:20:00", 101.0),
        ("10:24:00", 0.0),
        ("10:25:00", float("nan")),
        ("10:29:59", 103.0),
        ("10:30:00", 104.0),
    ]
    store = _write_session(root, rows)

    times, prices = store.trade_prices(_bar("10:20"), _bar("10:30"))

    assert prices.tolist() == [101.0, 103.0]
    assert times.dtype == "int64"
    assert prices.dtype == "float64"
    assert times.tolist() == [stamp.tz_localize(None).value // 1000 for stamp in (_stamp("10:20"), _stamp("10:29:59"))]


def test_ticks_that_repeat_the_forming_candle_are_not_decided_again(root):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)
    record: list = []

    result = backtest(_m10_frame(store), strategy=Breakout(record=record), symbol=SYMBOL, ticks=store)

    decided = [frame.iloc[-1] for phase, frame in record if phase == "tick"]
    assert [row["close"] for row in decided] == [101.0, 104.0, 105.5]
    assert [row["tick_volume"] for row in decided] == [1, 3, 6]
    assert result.trades["exit_price"].tolist() == [105.5]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:20:50")]


def test_worker_processes_reach_the_same_trades_as_one_process(root):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)
    frame = _m10_frame(store)

    single = backtest(frame, strategy=Breakout(), symbol=SYMBOL, ticks=store)
    pooled = backtest(frame, strategy=Breakout(), symbol=SYMBOL, ticks=store, workers=2)

    assert len(pooled.trades) == 1
    pd.testing.assert_frame_equal(pooled.trades[COMPARED], single.trades[COMPARED])


def test_a_tick_hook_failure_in_a_worker_names_the_phase_and_bar(root):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)

    with pytest.raises(RuntimeError, match="phase tick") as exc_info:
        backtest(_m10_frame(store), strategy=FailingOnTicks(), symbol=SYMBOL, ticks=store, workers=2)
    assert "2026-10-05 10:20" in str(exc_info.value)


def test_a_strategy_that_cannot_be_pickled_is_rejected_for_workers(root):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)

    class Local(Breakout):
        pass

    with pytest.raises(ValueError, match="workers=1") as exc_info:
        backtest(_m10_frame(store), strategy=Local(), symbol=SYMBOL, ticks=store, workers=2)
    assert "Local" in str(exc_info.value)


@pytest.mark.parametrize("workers", [0, -1, True, 2.0, "many"])
def test_invalid_workers_are_rejected(root, workers):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)
    with pytest.raises(ValueError, match="workers"):
        backtest(_m10_frame(store), strategy=Breakout(), symbol=SYMBOL, ticks=store, workers=workers)


def test_auto_workers_are_limited_by_cpus_and_free_memory(monkeypatch):
    monkeypatch.setattr(tick_phase.os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False)

    monkeypatch.setattr(tick_phase, "_available_memory", lambda: 3 << 30)
    assert resolve_workers("auto") == 3
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: 64 << 30)
    assert resolve_workers("auto") == 8
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: 1 << 20)
    assert resolve_workers("auto") == 1
    monkeypatch.setattr(tick_phase, "_available_memory", lambda: None)
    assert resolve_workers("auto") == 8
    assert resolve_workers(5) == 5


def test_progress_is_silent_off_a_terminal_and_reports_when_requested(root, capsys):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)
    frame = _m10_frame(store)

    quiet = backtest(frame, strategy=Breakout(), symbol=SYMBOL, ticks=store)
    assert capsys.readouterr().err == ""

    shown = backtest(frame, strategy=Breakout(), symbol=SYMBOL, ticks=store, progress=True)
    summary = capsys.readouterr().err
    assert "WDO$N M10" in summary
    assert "4 bars, 1 trade, 1 candle replayed from ticks" in summary
    pd.testing.assert_frame_equal(shown.trades[COMPARED], quiet.trades[COMPARED])


def test_progress_covers_strategies_compiled_to_signals(root, capsys):
    store = _write_session(root, ENTRY_BAR + REPEATS + NEXT_OPEN)

    class EntersOnce(ResearchStrategy):
        def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
            return TradeOrder.buy() if len(frame) == 1 else None

    backtest(_m10_frame(store), strategy=EntersOnce(), symbol=SYMBOL, progress=True)

    assert "4 bars, 1 trade, 0 candles replayed from ticks" in capsys.readouterr().err
