"""Phase-aware exit screening and causal tick replay in research backtests (Q-104)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research.errors import NoMarketDataError
from tests.research.test_stop_target_backtest import (
    CountingStore,
    _bar,
    _m10_frame,
    _stamp,
    _write_session,
)

SYMBOL = "WDO$N"
MARGIN = 5.0

ENTRY_BAR = [("10:00:00", 100.0), ("10:05:00", 100.0), ("10:10:00", 100.0), ("10:15:00", 100.0)]
BREAKOUT = [("10:20:00", 101.0), ("10:21:00", 104.0), ("10:22:00", 105.5), ("10:23:00", 103.0)]
NEXT_OPEN = [("10:30:00", 120.0), ("10:31:00", 121.0)]
NEXT_FLAT = [("10:30:00", 102.0), ("10:31:00", 103.0)]


class Breakout(ResearchStrategy):
    """Enters on bar 0 and exits a long or short once price reaches entry +/- 5 inside a candle."""

    def __init__(self, *, tick_margin: float = 0.0, record: list | None = None) -> None:
        self.tick_margin = tick_margin
        self.record = record
        self.entries = 0

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        self.entries += 1
        return TradeOrder.buy() if len(frame) == 1 else None

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        if self.record is not None:
            self.record.append((phase, frame.copy()))
        if phase == "bar" or not positions:
            return None
        level = positions[0].entry_price + MARGIN
        if phase == "screen":
            reached = frame["high"].iloc[-1] >= level
        else:
            reached = frame["close"].iloc[-1] >= level + self.tick_margin
        return TradeOrder.close() if reached else None


def _run(frame, store, strategy, **kwargs):
    return backtest(frame, strategy=strategy, symbol=SYMBOL, ticks=store, **kwargs)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ticks"


def test_a_custom_exit_fills_at_the_first_confirmed_price_not_the_next_open(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    frame = _m10_frame(store)
    result = _run(frame, store, Breakout())

    trades = result.trades
    assert trades["exit_price"].tolist() == [105.5]
    assert trades["exit_reason"].tolist() == ["SIGNAL"]
    assert trades["exit_time"].tolist() == [_bar("10:20")]
    assert trades["exit_tick_time"].tolist() == [_stamp("10:22")]
    assert trades["exit_price"].tolist() != [frame["open"].iloc[3]]


def test_a_short_custom_exit_mirrors_the_long(root):
    short_breakout = [("10:20:00", 99.0), ("10:21:00", 96.0), ("10:22:00", 94.5), ("10:23:00", 97.0)]
    store = _write_session(root, ENTRY_BAR + short_breakout + NEXT_OPEN)

    class Short(Breakout):
        def entry_strategy(self, frame, positions=()):
            return TradeOrder.sell() if len(frame) == 1 else None

        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            if phase == "bar" or not positions:
                return None
            level = positions[0].entry_price - MARGIN
            if phase == "screen":
                reached = frame["low"].iloc[-1] <= level
            else:
                reached = frame["close"].iloc[-1] <= level
            return TradeOrder.close() if reached else None

    result = _run(_m10_frame(store), store, Short())
    assert result.trades["side"].tolist() == ["short"]
    assert result.trades["exit_price"].tolist() == [94.5]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:22")]


def test_a_screen_without_a_confirming_tick_does_not_fill(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_FLAT)
    result = _run(_m10_frame(store), store, Breakout(tick_margin=1.0))

    assert result.trades["status"].tolist() == ["open"]
    assert result.trades["exit_reason"].isna().all()


def test_a_crossing_that_reverses_before_the_close_still_screens(root):
    reversal = [("10:20:00", 104.0), ("10:21:00", 105.5), ("10:22:00", 103.0)]
    store = _write_session(root, ENTRY_BAR + reversal + NEXT_OPEN)
    counting = CountingStore(SYMBOL, root=root)
    result = _run(_m10_frame(counting), counting, Breakout())

    assert result.trades["exit_price"].tolist() == [105.5]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:21")]
    assert counting.reads == [_bar("10:20")]


def test_flat_candles_are_not_screened_and_unqualified_candles_read_no_ticks(root):
    store = _write_session(root, ENTRY_BAR + NEXT_FLAT)
    counting = CountingStore(SYMBOL, root=root)
    record: list = []
    strategy = Breakout(record=record)
    result = _run(_m10_frame(counting), counting, strategy)

    screened = [frame.index[-1] for phase, frame in record if phase == "screen"]
    assert screened == [_bar("10:10"), _bar("10:30")]
    assert counting.reads == []
    assert result.trades["status"].tolist() == ["open"]


def test_a_protective_stop_and_a_custom_exit_on_one_candle_share_one_read(root):
    breakout = [("10:20:00", 101.0), ("10:21:00", 104.0), ("10:22:00", 105.5), ("10:23:00", 94.0)]
    store = _write_session(root, ENTRY_BAR + breakout + NEXT_OPEN)
    counting = CountingStore(SYMBOL, root=root)

    class Both(Breakout):
        def entry_strategy(self, frame, positions=()):
            if len(frame) == 1:
                return TradeOrder.buy(stop_loss=95.0, take_profit=130.0)
            return None

    result = _run(_m10_frame(counting), counting, Both())

    assert counting.reads == [_bar("10:20")]
    assert result.trades["exit_price"].tolist() == [105.5]


def test_tick_frames_are_causal_and_recompute_indicators_on_the_partial_candle(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    record: list = []

    class Averaged(Breakout):
        def compute_indicators(self, frame):
            out = frame.copy()
            out["ma"] = out["close"].rolling(2, min_periods=1).mean()
            return out

    _run(_m10_frame(store), store, Averaged(record=record))

    ticks = [frame for phase, frame in record if phase == "tick"]
    assert len(ticks) == 3
    last = ticks[-1]
    assert len(last) == 3
    assert last.index[-1] == _bar("10:20")
    assert last["open"].iloc[-1] == 101.0
    assert last["high"].iloc[-1] == 105.5
    assert last["low"].iloc[-1] == 101.0
    assert last["close"].iloc[-1] == 105.5
    assert last["tick_volume"].iloc[-1] == 3
    assert last["ma"].iloc[-1] == pytest.approx((100.0 + 105.5) / 2)
    assert np.isnan(last["real_volume"].iloc[-1])
    assert ticks[0]["close"].iloc[-1] == 101.0


def test_a_later_tick_cannot_justify_an_earlier_fill(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    result = _run(_m10_frame(store), store, Breakout())

    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:22")]
    assert result.trades["exit_price"].tolist() == [105.5]


def test_entry_hooks_are_not_invoked_per_tick(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    strategy = Breakout()
    _run(_m10_frame(store), store, strategy)

    assert strategy.entries == 4


def test_a_phase_aware_exit_without_ticks_names_the_strategy_and_argument(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT)
    with pytest.raises(ValueError, match="ticks=") as exc_info:
        backtest(_m10_frame(store), strategy=Breakout(), symbol=SYMBOL)
    assert "Breakout" in str(exc_info.value)


def test_a_legacy_exit_keeps_the_next_open_fill(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)

    class Legacy(ResearchStrategy):
        def entry_strategy(self, frame):
            return TradeOrder.buy() if len(frame) == 1 else None

        def exit_strategy(self, frame, **kwargs):
            return TradeOrder.close() if len(frame) == 3 else None

    result = backtest(_m10_frame(store), strategy=Legacy(), symbol=SYMBOL, ticks=store)
    assert result.trades["exit_price"].tolist() == [120.0]
    assert result.trades["exit_tick_time"].isna().all()


def test_a_tick_hook_failure_names_the_phase_and_bar(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)

    class Failing(Breakout):
        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            if phase == "tick":
                raise KeyError("feed")
            return super().exit_strategy(frame, positions, phase=phase)

    with pytest.raises(RuntimeError, match="phase tick") as exc_info:
        _run(_m10_frame(store), store, Failing())
    assert "2026-10-05 10:20" in str(exc_info.value)


def test_an_invalid_tick_decision_is_rejected_with_its_phase(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)

    class Wrong(Breakout):
        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            if phase == "tick":
                return TradeOrder.buy()
            return super().exit_strategy(frame, positions, phase=phase)

    with pytest.raises(ValueError, match="phase tick"):
        _run(_m10_frame(store), store, Wrong())


def test_a_missing_session_for_a_candidate_names_the_day_and_sync(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT)
    frame = _m10_frame(store).copy()
    extra = frame.iloc[[2]].copy()
    extra.index = pd.DatetimeIndex([pd.Timestamp("2026-10-06 10:00", tz=frame.index.tz)], name="time")
    extra.loc[:, "low"] = 90.0
    extra.loc[:, "high"] = 120.0
    frame = pd.concat([frame, extra])

    with pytest.raises(NoMarketDataError, match="2026-10-06") as exc_info:
        _run(frame, store, Breakout(tick_margin=1.0))
    assert "TickStore.sync" in str(exc_info.value)


def test_screen_positions_are_observed_after_queued_fills(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)
    record: list = []
    _run(_m10_frame(store), store, Breakout(record=record))
    screens = [(frame.index[-1], len(frame)) for phase, frame in record if phase == "screen"]
    assert screens[0] == (_bar("10:10"), 2)


def test_a_phase_exit_without_positions_still_screens_open_trades(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)

    class FrameOnlyPhased(ResearchStrategy):
        def entry_strategy(self, frame):
            return TradeOrder.buy() if len(frame) == 1 else None

        def exit_strategy(self, frame, *, phase="bar"):
            if phase == "screen":
                return TradeOrder.close() if frame["high"].iloc[-1] >= 105.0 else None
            if phase == "tick":
                return TradeOrder.close() if frame["close"].iloc[-1] >= 105.0 else None
            return None

    result = _run(_m10_frame(store), store, FrameOnlyPhased())
    assert result.trades["exit_price"].tolist() == [105.5]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:22")]


def test_a_bar_phase_close_still_fills_at_the_next_open(root):
    store = _write_session(root, ENTRY_BAR + BREAKOUT + NEXT_OPEN)

    class BarClose(ResearchStrategy):
        def entry_strategy(self, frame):
            return TradeOrder.buy() if len(frame) == 1 else None

        def exit_strategy(self, frame, *, phase="bar"):
            if phase == "bar" and len(frame) == 3:
                return TradeOrder.close()
            return None

    result = _run(_m10_frame(store), store, BarClose())
    assert result.trades["exit_price"].tolist() == [120.0]
    assert result.trades["exit_tick_time"].isna().all()


def test_position_aware_entry_levels_travel_through_the_runtime_callback(root):
    store = _write_session(
        root,
        ENTRY_BAR + [("10:20:00", 100.0), ("10:21:00", 96.0), ("10:22:00", 94.0)] + NEXT_OPEN,
    )

    class RuntimeLevels(ResearchStrategy):
        def entry_strategy(self, frame, positions=()):
            if len(frame) == 1 and not positions:
                return TradeOrder.buy(stop_loss=95.0, take_profit=130.0)
            return None

        def exit_strategy(self, frame, positions=(), *, phase="bar"):
            return None

    result = _run(_m10_frame(store), store, RuntimeLevels())
    assert result.trades["exit_reason"].tolist() == ["STOP_LOSS"]
    assert result.trades["exit_price"].tolist() == [94.0]
    assert result.trades["stop_loss"].tolist() == [95.0]
    assert result.trades["exit_tick_time"].tolist() == [_stamp("10:22")]
