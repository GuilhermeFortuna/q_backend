"""Hook frames are closed prefixes of the prepared history."""

from __future__ import annotations

import numpy as np
import pandas as pd

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from tests.research.test_intrabar_exit_hooks import ENTRY, MOVE, NEXT
from tests.research.test_stop_target_backtest import SYMBOL, _m10_frame, _write_session


def _with_moving_average(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["ma"] = out["close"].rolling(2, min_periods=1).mean()
    return out


class Recorder(ResearchStrategy):
    def __init__(self):
        self.frames = []
        self.computed = 0

    def compute_indicators(self, frame):
        self.computed += 1
        return _with_moving_average(frame)

    def entry_strategy(self, frame, positions=()):
        self.frames.append(("entry", frame))
        return TradeOrder.buy() if len(frame) == 1 else None

    def exit_strategy(self, frame, positions=(), *, ticks=None):
        self.frames.append(("exit", frame))
        return None


def test_compute_indicators_runs_once_and_hooks_run_once_for_each_closed_bar(tmp_path):
    store = _write_session(tmp_path / "ticks", ENTRY + MOVE + NEXT)
    strategy = Recorder()

    backtest(_m10_frame(store), strategy=strategy, symbol=SYMBOL, ticks=store)

    assert strategy.computed == 1
    assert len([kind for kind, _frame in strategy.frames if kind == "exit"]) == 4


def test_hook_frames_share_history_and_have_no_attributes(tmp_path):
    store = _write_session(tmp_path / "ticks", ENTRY + MOVE + NEXT)
    frame = _m10_frame(store)
    frame.attrs["leak"] = "history"
    strategy = Recorder()

    backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store)

    assert strategy.frames and all(view.attrs == {} for _kind, view in strategy.frames)
    first = strategy.frames[0][1]["close"].to_numpy()
    for _kind, view in strategy.frames:
        assert np.shares_memory(first, view["close"].to_numpy())


def test_a_hook_that_writes_to_its_frame_changes_nothing_later(tmp_path):
    store = _write_session(tmp_path / "ticks", ENTRY + MOVE + NEXT)
    frame = _m10_frame(store)
    expected = backtest(frame, strategy=Recorder(), symbol=SYMBOL, ticks=store)

    class Writing(Recorder):
        def entry_strategy(self, frame, positions=()):
            frame.iloc[:, frame.columns.get_loc("close")] = -1.0
            return super().entry_strategy(frame, positions)

        def exit_strategy(self, frame, positions=(), *, ticks=None):
            frame.iloc[-1, frame.columns.get_loc("high")] = 1e9
            return super().exit_strategy(frame, positions, ticks=ticks)

    actual = backtest(frame, strategy=Writing(), symbol=SYMBOL, ticks=store)
    pd.testing.assert_frame_equal(actual.data, expected.data)
