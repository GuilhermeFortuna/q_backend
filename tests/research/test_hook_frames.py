"""Hook frames over the prepared history, and the forming candle a tick decision sees (Q-121)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.research import TradeOrder, backtest
from tests.research.test_intrabar_exit_hooks import BREAKOUT, ENTRY_BAR, NEXT_OPEN, Breakout
from tests.research.test_stop_target_backtest import SYMBOL, _m10_frame, _write_session

MARKET = ("open", "high", "low", "close", "tick_volume")


def _with_moving_average(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["ma"] = out["close"].rolling(2, min_periods=1).mean()
    return out


class Recorder(Breakout):
    """A phase-aware breakout that keeps every frame a hook is handed, as the hook received it."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[tuple[str, pd.DataFrame]] = []
        self.computed = 0

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        self.computed += 1
        return _with_moving_average(frame)

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        self.frames.append(("entry", frame))
        return super().entry_strategy(frame, positions)

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        self.frames.append((phase, frame))
        return super().exit_strategy(frame, positions, phase=phase)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


def test_compute_indicators_runs_once_with_a_phase_aware_exit_and_screened_candles(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    strategy = Recorder()

    result = backtest(_m10_frame(store), strategy=strategy, symbol=SYMBOL, ticks=store)

    assert result.trades["exit_tick_time"].notna().all()
    assert any(phase == "tick" for phase, _ in strategy.frames)
    assert strategy.computed == 1


def test_bar_screen_entry_and_tick_frames_share_memory_with_the_prepared_history(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    views: dict[str, list[np.ndarray]] = {"entry": [], "screen": [], "tick": []}

    class Viewing(Breakout):
        def compute_indicators(self, frame):
            return _with_moving_average(frame)

        def entry_strategy(self, frame, positions=()):
            views["entry"].append(frame["close"].to_numpy())
            return super().entry_strategy(frame, positions)

        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            if phase in views:
                views[phase].append(frame["close"].to_numpy())
            return super().exit_strategy(frame, positions, phase=phase)

    backtest(_m10_frame(store), strategy=Viewing(), symbol=SYMBOL, ticks=store)

    assert views["screen"] and views["tick"]
    first = views["entry"][0]
    for name, arrays in views.items():
        for array in arrays:
            assert np.shares_memory(first, array), f"{name} frame was copied from the prepared history"


def test_a_hook_that_writes_to_its_frames_changes_nothing_later(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    frame = _m10_frame(store)
    expected = backtest(frame, strategy=Breakout(), symbol=SYMBOL, ticks=store)

    class Writing(Breakout):
        def entry_strategy(self, frame, positions=()):
            frame.iloc[:, frame.columns.get_loc("close")] = -1.0
            return super().entry_strategy(frame, positions)

        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            frame.iloc[-1, frame.columns.get_loc("high")] = 1e9
            return super().exit_strategy(frame, positions, phase=phase)

    result = backtest(frame, strategy=Writing(), symbol=SYMBOL, ticks=store)

    pd.testing.assert_frame_equal(result.trades.drop(columns="trade_id"), expected.trades.drop(columns="trade_id"))
    pd.testing.assert_frame_equal(result.data, expected.data)


def test_frames_have_no_attributes(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    frame = _m10_frame(store)
    frame.attrs["leak"] = "history"
    strategy = Recorder()

    backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store)

    assert strategy.frames and all(view.attrs == {} for _, view in strategy.frames)


def test_a_forming_candle_shows_observed_prices_and_missing_indicators(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    strategy = Recorder()
    result = backtest(_m10_frame(store), strategy=strategy, symbol=SYMBOL, ticks=store)

    ticks = [frame for phase, frame in strategy.frames if phase == "tick"]
    assert len(ticks) == 3
    last = ticks[-1]
    row = last.iloc[-1]
    assert (row["open"], row["high"], row["low"], row["close"], row["tick_volume"]) == (101.0, 105.5, 101.0, 105.5, 3)
    other = [column for column in last.columns if column not in MARKET]
    assert other and last[other].iloc[-1].isna().all()
    # The closed candles before it keep the values the prepared history has for them.
    prepared = result.data.loc[last.index[:-1]]
    pd.testing.assert_series_equal(last["ma"].iloc[:-1], prepared["ma"], check_names=False)


def test_a_later_tick_never_reaches_an_earlier_decision(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    strategy = Recorder()
    backtest(_m10_frame(store), strategy=strategy, symbol=SYMBOL, ticks=store)

    decided = [frame.iloc[-1] for phase, frame in strategy.frames if phase == "tick"]
    assert [row["close"] for row in decided] == [101.0, 104.0, 105.5]
    assert [row["high"] for row in decided] == [101.0, 104.0, 105.5]
    assert [row["low"] for row in decided] == [101.0, 101.0, 101.0]
